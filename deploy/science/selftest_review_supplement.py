"""Append-only review requests for completed selftests; never builds or runs ACS.

The periodic consumer also retries explicit historical handoffs with bounded
backoff. Author requests are snapshotted into operator evidence before use.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import time

from author_submission_gate import read_operator_file, validate_identity
from evaluation_queue import atomic_json
from selftest_upload import snapshot_upload

IDENTITY = ('job_id', 'idea_id', 'source_repository', 'source_task_path',
            'source_commit', 'snapshot_sha256', 'execution_commit', 'execution_task_path')
FIELDS = {'schema_version', *IDENTITY, 'iteration'}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def request_id(value):
    return hashlib.sha256(encoded(value)).hexdigest()


@contextmanager
def operator_lock(path, blocking=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError('Invalid operator lock')
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def immutable_json(path, value):
    """Publish a complete root-owned record once, including across crashes."""
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.review-capture-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded(value))
            output.flush()
            os.fsync(output.fileno())
        os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate_request(request, role, status, original):
    if not isinstance(request, dict) or set(request) != FIELDS or request['schema_version'] != 1:
        raise ValueError('Invalid review supplement fields')
    if (status.get('schema_version') != 2 or status.get('role') != role
            or status.get('status') != 'selftest_passed'
            or status.get('source_git_tree_verified') is not True
            or status.get('quality_acceptance') is not False):
        raise ValueError('Require this author successful source-bound selftest')
    if any(request.get(key) != status.get(key) for key in IDENTITY):
        raise ValueError('Stale review source identity; use the original successful job')
    for key in ('job_id', 'idea_id', 'source_repository', 'source_task_path', 'source_commit'):
        if original.get(key) != status.get(key):
            raise ValueError('Original request does not bind this source and idea')
    if (not isinstance(request['idea_id'], str)
            or not re.fullmatch('[a-z0-9][a-z0-9-]{0,95}', request['idea_id'])):
        raise ValueError('Cannot introduce an idea missing from the original request')
    validate_identity(request['source_commit'], request['source_task_path'])
    if not re.fullmatch('[a-f0-9]{64}', str(request['snapshot_sha256'])):
        raise ValueError('Invalid frozen snapshot identity')
    if original.get('initial_review'):
        raise ValueError('Legacy initial-review bypass is not supported')
    iteration = request['iteration']
    if (not isinstance(iteration, dict) or set(iteration) != {'previous_job', 'note'}
            or not isinstance(iteration.get('previous_job'), str)
            or not re.fullmatch('[a-f0-9]{32}', iteration['previous_job'])
            or iteration['previous_job'] == request['job_id']
            or not isinstance(iteration.get('note'), str)
            or not 20 <= len(iteration['note'].strip()) <= 4000):
        raise ValueError('Require a different prior job and genuine iteration explanation')


def effective_request(work):
    """Return merged intent plus immutable evidence paths; do not rewrite it."""
    work = Path(work)
    original_raw = read_operator_file(work/'request.json', 16384)
    original = json.loads(original_raw)
    path = work/'review-supplement.json'
    if not path.exists():
        return original, []
    evidence = json.loads(read_operator_file(path, 32768))
    status_raw = read_operator_file(work/'status.json', 1024*1024)
    status = json.loads(status_raw)
    if (evidence.get('schema_version') != 1
            or evidence.get('original_request_sha256') != hashlib.sha256(original_raw).hexdigest()
            or evidence.get('original_status_sha256') != hashlib.sha256(status_raw).hexdigest()
            or evidence.get('build_receipt_sha256') != hashlib.sha256(read_operator_file(work/'build/receipt.json')).hexdigest()
            or evidence.get('job_id') != work.name
            or evidence.get('role') != status.get('role')
            or evidence.get('request_id') != request_id(evidence.get('request'))):
        raise ValueError('Review supplement no longer matches immutable execution evidence')
    request = evidence['request']
    validate_request(request, evidence.get('role'), status, original)
    return dict(original, request_review=True, iteration=request['iteration']), [path]


def capture(role, entry, source, state):
    """Validate an author's file and append a single immutable effective intent."""
    source, state = Path(source), Path(state)
    expected = Path(entry['root'])/'selftest/inbox'
    if source.parent.resolve() != expected.resolve() or not re.fullmatch('review-[a-f0-9]{64}\\.json', source.name):
        raise ValueError('Review request outside author inbox')
    raw = snapshot_upload(source, entry['uid'], 16384)
    request = json.loads(raw)
    if source.stem != 'review-'+request_id(request):
        raise ValueError('Review request content does not match its ID')
    job = request.get('job_id') if isinstance(request, dict) else None
    if not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job):
        raise ValueError('Invalid target selftest job')
    work = state/job
    # Check ownership before taking a lock in a target job or exposing evidence.
    status_raw = read_operator_file(work/'status.json', 1024*1024)
    status = json.loads(status_raw)
    if status.get('role') != role:
        raise ValueError('Require this author successful source-bound selftest')
    with operator_lock(work/'review-supplement.lock'):
        original_raw = read_operator_file(work/'request.json', 16384)
        original = json.loads(original_raw)
        validate_request(request, role, status, original)
        path = work/'review-supplement.json'
        if path.exists():
            prior = json.loads(read_operator_file(path, 32768))
            if prior.get('request_id') != request_id(request):
                raise ValueError('This job already has another immutable review request')
            effective_request(work)
            return work
        if original.get('request_review'):
            if original.get('iteration') != request['iteration']:
                raise ValueError('Original review request already binds another iteration')
            return work
        from selftest_iteration import validate
        validate(request['iteration'], status, state, role)
        receipt_raw = read_operator_file(work/'build/receipt.json')
        evidence = dict(schema_version=1, role=role, job_id=job,
                        request_id=request_id(request), captured_at=time.time(), request=request,
                        original_request_sha256=hashlib.sha256(original_raw).hexdigest(),
                        original_status_sha256=hashlib.sha256(status_raw).hexdigest(),
                        build_receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
                        cloud_execution_started=False, scientific_acceptance=False)
        immutable_json(path, evidence)
        return work


def publish_request_result(entry, identifier, result):
    path = Path(entry['root'])/'selftest/results'/('review-'+identifier+'.json')
    atomic_json(path, dict(result, request_id=identifier,
                cloud_execution_started=False, selftest_rerun_required=False,
                scientific_acceptance=False), 0o640)
    os.chown(path, 0, entry['gid'])


def process_request(role, entry, source, state):
    identifier = source.stem.removeprefix('review-')
    try:
        work = capture(role, entry, source, state)
    except (OSError, ValueError, KeyError, TypeError) as error:
        # All accepted fields are credential-free identifiers; avoid exception
        # details from arbitrary paths or Python internals in author feedback.
        reason = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'Invalid, incomplete or unavailable review request/evidence'
        publish_request_result(entry, identifier, dict(state='rejected', stage='review_supplement',
                               reason=reason, error_type=type(error).__name__))
        return None
    publish_request_result(entry, identifier, dict(state='review_request_recorded', job_id=work.name,
                           stage='review_supplement', reason='Existing execution retained; only review qualification and registration will run.'))
    return work


def file_signature(path):
    try:
        info = Path(path).lstat()
        return [info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    except OSError:
        return None


def observation_dependency(path, idea):
    """Watch facts for this idea, excluding the observer's periodic heartbeat."""
    try:
        observed=json.loads(read_operator_file(path))
        row=observed.get('ideas',{}).get(idea)
        if row is not None and not isinstance(row,dict):
            return dict(state='invalid_idea')
        return dict(state='available',role=observed.get('role'),idea_id=idea,
                    idea=None if row is None else {
                        key:row.get(key) for key in ('events','status','first_observed_at')},
                    recovery=observed.get('recovery_evidence',{}).get(idea))
    except FileNotFoundError:
        return dict(state='missing')
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
        return dict(state='unavailable',error_type=type(error).__name__)


def retry_signature(work, state, role, universe):
    paths = [work/name for name in ('request.json','status.json','build/receipt.json','mechanical.json','review-supplement.json','legacy-budget-admission.json')]
    paths += [state.parent/'maintenance/verified-pauses.json',
              Path(universe)/'.config/science-evaluation/queue.json',
              Path(__file__), Path(__file__).with_name('selftest_handoff.py'),
              Path(__file__).with_name('selftest_budget_admission.py'),
              Path(__file__).with_name('selftest_budget.py'),
              Path(__file__).with_name('completed_review_reuse.py'),
              Path(__file__).with_name('selftest_registration.py'),
              Path(__file__).with_name('selftest_iteration.py')]
    # A newly formalized decision may resolve a legacy wait for this exact
    # version without author edits or another cloud execution.
    try:
        status=json.loads(read_operator_file(work/'status.json'))
        from formal_review_decision import name_for
        key=status['source_commit']+':'+status['source_task_path']
        paths.append(Path(universe)/'.state/gewu-multiverse/review-dispatcher/formal-decisions'/name_for(key))
    except (OSError,ValueError,KeyError,TypeError):pass
    idea=None
    try:
        request, _ = effective_request(work)
        idea=request.get('idea_id')
        prior = request.get('iteration', {}).get('previous_job')
        if isinstance(prior, str) and re.fullmatch('[a-f0-9]{32}', prior):
            paths.append(state/prior/'status.json')
    except (OSError, ValueError, TypeError, KeyError):
        pass
    observation=state.parent/'command-watchdog/iteration-observations'/(role+'.json')
    dependencies=dict(files={str(p):file_signature(p) for p in paths},
                      observation=observation_dependency(observation,idea))
    return hashlib.sha256(encoded(dependencies)).hexdigest()


def consume_once(config, state, universe, now=None):
    """Process metadata and retry due handoffs. No cloud queue is touched."""
    state, universe = Path(state), Path(universe)
    now = time.time() if now is None else now
    service = state/'review-supplement-consumer'
    service.mkdir(mode=0o700, parents=True, exist_ok=True)
    intake = service/'intake'; intake.mkdir(mode=0o700, exist_ok=True)
    attempts = service/'attempts'; attempts.mkdir(mode=0o700, exist_ok=True)
    accepted = {}
    for role, entry in sorted(config['roles'].items()):
        if not re.fullmatch('author-[0-9]+', role):
            continue
        inbox = Path(entry['root'])/'selftest/inbox'
        for source in sorted(inbox.glob('review-*.json')):
            if not re.fullmatch('review-[a-f0-9]{64}\\.json', source.name):
                continue
            record = intake/(role+'-'+source.name)
            signature = file_signature(source)
            try:
                if record.exists():
                    previous = json.loads(read_operator_file(record))
                    if previous.get('source_signature') == signature:
                        continue
                work = process_request(role, entry, source, state)
                atomic_json(record, dict(source_signature=signature, processed_at=now,
                            job_id=work.name if work else None))
                if work:
                    accepted[work.name] = source.stem.removeprefix('review-')
            except (OSError, ValueError, TypeError, KeyError) as error:
                atomic_json(service/(role+'-'+source.stem+'-error.json'),
                            dict(error_type=type(error).__name__, observed_at=now))
    # This also recovers requests captured before a crash and older explicit
    # review requests that completed before a periodic reconciler existed.
    counts = dict(captured=len(accepted), attempted=0, deferred=0)
    for path in sorted(state.glob('*/status.json')):
        work = path.parent
        if not re.fullmatch('[a-f0-9]{32}', work.name):
            continue
        role=None;entry=None
        try:
            status = json.loads(read_operator_file(path, 1024*1024))
            role = status.get('role'); entry = config['roles'].get(role)
            if not entry or status.get('status') != 'selftest_passed':
                continue
            request, supplement = effective_request(work)
            if not request.get('request_review'):
                continue
            result_path = work/'handoff.json'
            current_result = json.loads(read_operator_file(result_path)) if result_path.exists() else {}
            # Completed immutable registration is not periodic work.
            if current_result.get('state') == 'ready_for_independent_review':
                from selftest_budget_admission import assert_current_idea_active
                try:
                    assert_current_idea_active(work,status)
                except ValueError:
                    pass  # Reconcile records an actionable blocked disposition.
                else:
                    continue
            signature = retry_signature(work, state, role, universe)
            marker = attempts/(work.name+'.json')
            previous = json.loads(read_operator_file(marker)) if marker.exists() else {}
            changed = previous.get('input_signature') != signature
            if previous and ((not changed and (previous.get('state') != 'waiting_platform'
                                              or previous.get('next_retry_at', 0) > now))
                             or previous.get('last_attempt_at', 0)+5 > now):
                counts['deferred'] += 1
                continue
            from selftest_handoff import reconcile
            reconcile(work, state, entry, universe)
            result = json.loads(read_operator_file(result_path))
            number = previous.get('attempts', 0)+1
            delay = min(900, 60 * (2 ** min(number-1, 4)))
            # Author repairs require changed evidence. Platform failures get a
            # bounded retry as an operator may restore an external dependency.
            next_retry = now+delay if result.get('state') == 'waiting_platform' else None
            atomic_json(marker, dict(attempts=number, last_attempt_at=now, next_retry_at=next_retry,
                        input_signature=signature, state=result.get('state')))
            if supplement:
                identifier = json.loads(read_operator_file(supplement[0]))['request_id']
                publish_request_result(entry, identifier, dict(result, job_id=work.name))
            counts['attempted'] += 1
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Preserve a per-job operator diagnostic and keep other authors
            # moving; do not overwrite original selftest results or retry ACS.
            atomic_json(service/(work.name+'-error.json'), dict(job_id=work.name,
                        error_type=type(error).__name__, observed_at=now,
                        cloud_execution_started=False))
            # Accepted metadata must not leave the author indefinitely seeing
            # 'recorded' when operator evidence later becomes unavailable.
            try:
                accepted_file=work/'review-supplement.json'
                accepted_record=json.loads(read_operator_file(accepted_file,32768))
                identifier=accepted_record['request_id']
                if (re.fullmatch('[a-f0-9]{64}',identifier)
                        and accepted_record.get('role')==role and entry):
                    publish_request_result(entry,identifier,dict(state='waiting_platform',
                        stage='review_evidence_validation',job_id=work.name,
                        reason='Platform must validate current immutable review evidence; retain this execution and do not rerun ACS.',
                        error_type=type(error).__name__))
            except (OSError,ValueError,KeyError,TypeError):
                pass
    atomic_json(service/'heartbeat.json', dict(updated_at=time.time(), **counts))
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--broker-config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--universe', type=Path, required=True)
    parser.add_argument('--poll-seconds', type=int, default=30)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--owner-pid', type=int)
    args = parser.parse_args()
    from command_watchdog import process_info, owner_alive
    owner = process_info(args.owner_pid) if args.owner_pid is not None else None
    if os.geteuid() != 0 or not 5 <= args.poll_seconds <= 300:
        raise SystemExit('Require operator/root and polling interval 5..300 seconds')
    directory = args.state/'review-supplement-consumer'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    with operator_lock(directory/'consumer.lock', blocking=False):
        from iteration_budget import LIMIT
        print(json.dumps(dict(event='review_consumer_started',pid=os.getpid(),budget_seconds=LIMIT,owner_pid=args.owner_pid)),flush=True)
        while not stopping and owner_alive(owner):
            try:
                consume_once(json.loads(read_operator_file(args.broker_config)), args.state, args.universe)
            except Exception as error:
                print(json.dumps(dict(event='review_consumer_error', error_type=type(error).__name__)), flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            deadline = time.monotonic()+args.poll_seconds
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline-time.monotonic())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
