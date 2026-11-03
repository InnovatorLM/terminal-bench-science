"""Operator-side reconciliation of author clocks with verified model runners."""
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from author_iteration_clock import reconcile_session, transition
from command_watchdog import process_info


def operator_json(path):
    from author_submission_gate import read_operator_file
    try:
        value = json.loads(read_operator_file(path, 1024*1024))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError('Invalid operator lifecycle evidence')
    return value


def review_waiting(root):
    """Match the controller's exact author hold, not an old review state alone."""
    root = Path(root)
    feedback = operator_json(root/'review-feedback.json')
    current = operator_json(root/'review-current-assignment.json')
    if (feedback.get('author_must_pause') is not True
            or feedback.get('state') not in ('reviewing', 'waiting_platform')
            or type(feedback.get('generation')) is not int or feedback['generation'] < 0
            or any(not isinstance(feedback.get(k), str) or not feedback[k]
                   for k in ('key', 'commit', 'task'))
            or any(current.get(k) != feedback.get(k)
                   for k in ('state', 'key', 'generation', 'commit', 'task'))):
        return False
    if feedback['state'] == 'reviewing' and feedback.get('job_id') is None:
        return True  # Legacy PR assignments have no self-test identity.
    request = operator_json(root/'selftest/current-request.json')
    if feedback['state'] == 'reviewing':
        if not request:
            return feedback.get('idea_id') is None and feedback.get('received_at') is None
        return all(feedback.get(k) is not None and request.get(k) == feedback[k]
                   for k in ('job_id', 'idea_id', 'commit', 'task', 'received_at'))
    if feedback.get('stage') in ('five_rollouts', 'formal_acceptance_evidence'):
        if (current.get('stage') != feedback['stage']
                or any(not isinstance(feedback.get(k), str) or not feedback[k]
                       or current.get(k) != feedback[k]
                       for k in ('evaluation_id', 'snapshot_sha256'))):
            return False
    elif (feedback.get('feedback_source') != 'selftest_handoff'
            or current.get('feedback_source') != 'selftest_handoff'):
        return False
    return all(feedback.get(k) is not None
               and current.get(k) == feedback[k] and request.get(k) == feedback[k]
               for k in ('job_id', 'idea_id', 'commit', 'task', 'received_at'))


def terminal_selftest(sessions, role, idea, job, now):
    """A terminal operator receipt with no unresolved cloud cleanup."""
    if not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job):
        return None
    status = operator_json(Path(sessions)/'author-selftest'/job/'status.json')
    if (status.get('schema_version') != 2
            or status.get('role') != role or status.get('idea_id') != idea
            or status.get('job_id') != job
            or status.get('status') not in ('selftest_passed', 'selftest_failed', 'failed')):
        return None
    start, end = status.get('received_at'), status.get('completed_at')
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end))
            or not 0 < start <= end <= now or status.get('cleanup_confirmed') is False):
        return None
    trials = status.get('trials', [])
    if not isinstance(trials, list) or any(not isinstance(t, dict)
            or t.get('cleanup_confirmed') is not True for t in trials):
        return None
    if not trials and (status['status'] != 'failed'
            or status.get('cloud_execution_started') is True
            or status.get('cloud_execution_attempted') is True):
        return None
    source_fields = ('source_repository', 'source_task_path', 'source_commit')
    if any(not isinstance(status.get(k), str) or not status[k] for k in source_fields):
        # A preflight rejection can finish before the worker verifies source
        # bytes. Its immutable captured request binds the failed attempt, not
        # an executed/qualified task; explicit no-cloud evidence is required.
        if (status['status'] != 'failed' or trials
                or status.get('cloud_execution_started') is not False
                or status.get('cloud_execution_attempted') is not False):
            return None
        request = operator_json(Path(sessions)/'author-selftest'/job/'request.json')
        if (request.get('job_id') != job or request.get('idea_id') != idea
                or any(not isinstance(request.get(k), str) or not request[k]
                       or (status.get(k) is not None and status[k] != request[k]) for k in source_fields)):
            return None
        status = dict(status, **{k:request[k] for k in source_fields})
    return status


def resume_completed_acs_wait(state, root, role, sessions, live, now):
    """Resume at observation time; never rewrite or refund historical intervals.

    Both the clock's job and the latest accepted job must be terminal, belong to
    this exact idea/task and have confirmed cleanup. A newer running job, an
    unknown receipt or a current review/platform hold keeps the pause intact.
    """
    if not live or review_waiting(root):
        return
    request = operator_json(Path(root)/'selftest/current-request.json')
    if request.get('role') != role:
        return
    for idea, row in state.get('ideas', {}).items():
        if row.get('status') != 'acs_wait' or request.get('idea_id') != idea:
            continue
        paused = terminal_selftest(sessions, role, idea, row.get('acs_run_id'), now)
        latest = terminal_selftest(sessions, role, idea, request.get('job_id'), now)
        if paused is None or latest is None:
            continue
        if (any(latest.get(k) != paused.get(k) for k in ('source_repository', 'source_task_path'))
                or any(not request.get(k) or latest.get(v) != request[k] for k, v in (
                    ('commit', 'source_commit'), ('task', 'source_task_path'), ('received_at', 'received_at')))
                or request != operator_json(Path(root)/'selftest/current-request.json')
                or review_waiting(root)):
            continue
        transition(state, 'resume', idea, now,
                   reason='operator observed current selftest terminal with confirmed cleanup and a live author')


def runner_state(run,uid):
    try:
        pid=int((run/'session.pid').read_text())
        info=process_info(pid)
        argv=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(bytes([0]))
        return info['uid']==uid and info['state']!='Z' and b'--run-agent' in argv and str(run).encode() in argv
    except (OSError,ValueError):return False


def terminal_time(run):
    try:
        with (run/'transcript.jsonl').open('rb') as stream:
            stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-16384));data=stream.read()
        for line in reversed(data.splitlines()):
            try:event=json.loads(line)
            except (ValueError,UnicodeError):continue
            if event.get('event')=='run_finished':
                return datetime.datetime.fromisoformat(event['ts'].replace('Z','+00:00')).timestamp()
    except (OSError,ValueError,KeyError):pass
    return None


def scan_clocks(config, state_root=None, observation_root=None):
    errors=[]
    for name,entry in config['roles'].items():
        try:
            scoped=dict(controller_uid=config['controller_uid'],roles={name:entry})
            if state_root is not None:scoped['session_state_root']=str(state_root)
            if observation_root is not None:scoped['observation_root']=str(observation_root)
            _scan_clocks(scoped)
        except (OSError,ValueError,KeyError,TypeError) as error:
            # No ledger contents or arbitrary role-controlled error text in logs.
            errors.append(dict(role=name,error_type=type(error).__name__))
    if errors:print(json.dumps(dict(event='iteration_clock_reconciliation_failed',roles=errors)),flush=True)
    return errors


def _scan_clocks(config):
    for name,entry in config['roles'].items():
        if not name.startswith('author-'):continue
        root=Path(entry['root']);directory=root/'home/role/.state/author-iteration'
        if not directory.exists():continue
        # Refuse role-created path escapes before privileged file operations.
        if directory.resolve()!=directory or not directory.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe author clock directory')
        path=directory/'ledger.json'
        if not path.exists():continue
        lockfd=os.open(directory/'clock.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            lockinfo=os.fstat(lockfd)
            if not stat.S_ISREG(lockinfo.st_mode) or lockinfo.st_nlink!=1:
                raise ValueError('Invalid author clock lock')
            try:fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:continue
            os.fchown(lockfd,entry['uid'],entry['gid'])
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'r') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024:raise ValueError('Invalid author ledger')
                state=json.load(stream)
            # A controller may reuse isolated workspaces from a stopped donor.
            # Its model runners live in the selected deployment, not the donor.
            sessions=Path(config['session_state_root']) if config.get('session_state_root') else root.parents[3]/'.state/gewu-multiverse'
            runs=list((sessions/name).glob('run-*'))
            live=any(runner_state(run,config['controller_uid']) for run in runs)
            end=terminal_time(max(runs,key=lambda p:p.stat().st_mtime)) if runs and not live else None
            before=json.dumps(state,sort_keys=True)
            now=time.time()
            resume_completed_acs_wait(state,root,name,sessions,live,now)
            reconcile_session(state,live,now,end)
            if config.get('observation_root'):
                from iteration_observation import observe
                observe(Path(config['observation_root']),name,state,live,now)
            if json.dumps(state,sort_keys=True)!=before:
                pending=directory/('lifecycle-'+str(os.getpid())+'.pending')
                out=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(out,'w') as stream:json.dump(state,stream,ensure_ascii=False,indent=2)
                os.chown(pending,entry['uid'],entry['gid']);pending.replace(path)
        finally:os.close(lockfd)
