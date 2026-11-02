#!/usr/bin/env python3
"""Operator-owned evaluator dispatch. Requests contain IDs, never commands/keys.

Only immutable operator-registered snapshots/configurations can run. Results
contain aggregate solver evidence, never an acceptance or raw trajectory.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

W = Path('/mnt/data/zhanglinfeng/workspace')
ID = re.compile(r'^[a-f0-9]{32}$')
REQUEST_KEYS = {'job_id', 'evaluation_id', 'snapshot_sha256'}


def digest_tree(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Snapshot must be a real directory')
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('Snapshot links/special files are forbidden')
        if path.is_file():
            name = path.relative_to(root).as_posix().encode()
            digest.update(len(name).to_bytes(8, 'big') + name)
            digest.update(info.st_size.to_bytes(8, 'big'))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def root_file(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Operator file must not be a symlink')
    path = path.resolve(strict=True)
    info = path.lstat()
    local_runtime=str(path).startswith('/tmp/gewu-terminal-bench-science-runtime-')
    if (not path.is_relative_to(W) and not local_runtime) or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Operator file ownership/type/mode refused')
    return path

def queue_dir(path, uid=None):
    path=Path(path).resolve(strict=True); info=path.lstat()
    local_runtime=str(path).startswith('/tmp/gewu-terminal-bench-science-runtime-')
    allowed_owner={0} if not local_runtime else {0, uid or 0}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed_owner or info.st_mode & 0o022:
        raise ValueError('Queue directory ownership/type/mode refused')
    return path


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    temporary = path.with_suffix('.writing')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def evaluation_catalog(registry):
    purposes = {'solver': 'registered solver evidence; not acceptance',
                'quality_diagnostic': 'quality diagnostic; not acceptance',
                'semantic_diagnostic': 'full-corpus instruction comparison; not acceptance'}
    return {'evaluations': [{'evaluation_id': key, 'snapshot_sha256': entry['snapshot_sha256'],
                             'purpose': purposes[entry.get('dispatch', 'solver')]}
                            for key, entry in sorted(registry.items()) if entry.get('enabled', True) is True]}


def read_request(path, uid):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > 16384:
            raise ValueError('Request ownership/type/size refused')
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('Request grew beyond limit')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != REQUEST_KEYS or not all(isinstance(v, str) for v in value.values()):
        raise ValueError('Request schema refused')
    if not ID.fullmatch(value['job_id']) or path.name != value['job_id'] + '.json':
        raise ValueError('Request identity refused')
    return value


def validated_template(entry, request):
    task = root_file(entry['template'])
    raw = task.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['template_sha256']:
        raise ValueError('Registered template changed')
    snapshot = Path(entry['snapshot'])
    local_snapshot=str(snapshot.resolve()).startswith('/tmp/gewu-terminal-bench-science-runtime-')
    workspace_snapshot=snapshot.resolve().is_relative_to(W.resolve())
    if (not workspace_snapshot and not local_snapshot) or digest_tree(snapshot) != entry['snapshot_sha256'] or request['snapshot_sha256'] != entry['snapshot_sha256']:
        raise ValueError('Registered snapshot changed or request is stale')
    if 'mechanical_check_sha256' in entry:
        from check_harbor_task import validate_mechanical_check
        validate_mechanical_check(snapshot.parent / 'mechanical-check.json', snapshot, entry['mechanical_check_sha256'])
    config = json.loads(raw)
    if config['n_attempts'] != 1 or config['n_concurrent_trials'] != 1 or config['retry']['max_retries'] != 0:
        raise ValueError('Require one trial and zero retries')
    if config['environment']['type'] != 'ack' or len(config['agents']) != 1:
        raise ValueError('Environment/agent refused')
    environment = config['environment']['kwargs']
    image_pattern = r'dptech-sh-pai-acr-registry\.cn-shanghai\.cr\.aliyuncs\.com/terminal-bench-universe/[a-z0-9._/-]+@sha256:[a-f0-9]{64}'
    if environment.get('use_sandbox_claim') is not False or any(not re.fullmatch(image_pattern, environment.get(key, '')) for key in ['runtime_image', 'verifier_image']):
        raise ValueError('Require direct Pods and digest-pinned project ACR images')
    agent = config['agents'][0]
    codex = agent.get('name') == 'codex-preinstall' and not agent.get('import_path')
    legacy = agent.get('import_path') == 'universe_metrics:MetadataOnlyMiniSwe'
    if agent.get('model_name') != 'openai/deepseek-v4.1-flash-1017' or not (codex or legacy):
        raise ValueError('Model/harness refused')
    if codex:
        if agent.get('override_timeout_sec') != 14400 or agent.get('kwargs',{}).get('max_steps') is not None:
            raise ValueError('Codex requires four-hour wall-clock budget and no step bound')
        env = agent.get('env',{})
        if env.get('CODEX_MODEL_PROVIDER') != 'dawnfm' or env.get('CODEX_WIRE_API') != 'responses':
            raise ValueError('Explicit Codex provider and Responses protocol required')
    if codex:
        from acs_helpers.run_guard import check_config
        check_config(config)
    options = agent.get('kwargs', {})
    if 'private_trajectory_dir' in options or type(options.get('retain_private_trajectory', False)) is not bool:
        raise ValueError('Private trajectory destinations are assigned only by the worker')
    agent_env = agent.get('env', {})
    if agent_env.get('OPENAI_API_KEY') != '${OPENAI_API_KEY}' or agent_env.get('OPENAI_BASE_URL') != 'https://dawnfm.gravarc.cn/v1' or agent_env.get('OPENAI_API_BASE') != 'https://dawnfm.gravarc.cn/v1':
        raise ValueError('Explicit designated provider and key placeholder required')
    datasets=[{'path': str(snapshot.parent), 'task_names': [snapshot.name]}]
    if config['datasets'] != datasets:
        # Local runtime migration preserves the signed template and task bytes.
        # Accept its former path only when the root-owned provenance binds that
        # exact template and task digest; execute against the verified local copy.
        provenance=json.loads(root_file(snapshot.parent/'provenance.json').read_text())
        original=Path(provenance.get('snapshot',''))
        if (not (workspace_snapshot or local_snapshot) or not original.is_absolute()
                or provenance.get('template_sha256')!=entry['template_sha256']
                or provenance.get('snapshot_sha256')!=entry['snapshot_sha256']
                or not re.fullmatch('[a-f0-9]{40}',str(entry.get('source_commit','')))
                or provenance.get('source_commit')!=entry.get('source_commit')
                or config['datasets']!=[{'path':str(original.parent),'task_names':[original.name]}]):
            raise ValueError('Dataset is not the registered snapshot')
        config['datasets']=datasets
    if any(value for key, value in config.items() if key.startswith('export')):
        raise ValueError('Raw export refused')
    return config


def aggregate(report, result_path, config=None, snapshot_sha256=None, snapshot_unchanged=False):
    from acs_result_diagnostics import classify_trial
    stats = report.get('stats') or {}
    complete = stats.get('n_completed_trials') == 1 and stats.get('n_errored_trials') == 0 and report.get('harbor_exit_code') == 0
    cleanup = report.get('cleanup', {}).get('confirmed') is True
    rewards = []
    diagnostics = []
    errors = 0
    for evaluation in stats.get('evals', {}).values():
        errors += evaluation.get('n_errors', 0)
        rewards.extend(float(key) for key in evaluation.get('reward_stats', {}).get('reward', {}))
    metadata = []
    if result_path.exists():
        for path in result_path.parent.glob('*/result.json'):
            trial = json.loads(path.read_text())
            diagnostics.append(classify_trial(trial))
            data = ((trial.get('agent_result') or {}).get('metadata') or {}).get('aggregate_metrics')
            if isinstance(data, dict):
                metadata.append({key: value for key, value in data.items() if key in {'input_tokens', 'output_tokens', 'model_messages', 'tool_result_messages', 'native_tool_calls'} and (value is None or type(value) is int and value >= 0)})
    unscored = [item for item in diagnostics if not item['scored']]
    timeout_review = any(item['diagnostic'] == 'agent_timeout_requires_cause_review' for item in unscored)
    infrastructure = any(item['diagnostic'] != 'agent_timeout_requires_cause_review' for item in unscored)
    status = ('infrastructure_failed' if infrastructure else
              'agent_timeout_requires_cause_review' if timeout_review else
              'solver_complete' if complete and cleanup and errors == 0 else 'failed')
    from execution_evidence import inspect, public_status
    evidence = inspect(report, result_path, config, snapshot_sha256, snapshot_unchanged)
    if status == 'solver_complete' and not evidence['complete']:
        status = 'incomplete_evidence'
    return {'status': status, **public_status(evidence),
            'reward': rewards[0] if len(rewards) == 1 and complete and not unscored else None,
            'cleanup_confirmed': cleanup, 'n_errors': errors if stats.get('evals') else None,
            'input_tokens': stats.get('n_input_tokens'), 'output_tokens': stats.get('n_output_tokens'),
            'aggregate_metrics': metadata, 'diagnostics': diagnostics,
            'quality_acceptance': False, 'raw_trajectory_exported': evidence['checks']['trajectory_retained'],
            'raw_trajectory_exported_to_roles': False}


def process(config, request_path, mock=False):
    request = read_request(request_path, config['evaluator_uid'])
    state = Path(config['state'])
    job = state / request['job_id']
    if job.exists():
        claim = json.loads(root_file(job / 'claim.json').read_text())
        if any(claim.get(key) != request[key] for key in REQUEST_KEYS):
            raise ValueError('Job ID already belongs to a different immutable request')
        retained = job / 'result.json'
        answer = json.loads(retained.read_text()) if retained.exists() else {**request, 'status': 'interrupted', 'cleanup_confirmed': False, 'quality_acceptance': False}
        if any(answer.get(key) != request[key] for key in REQUEST_KEYS):
            raise ValueError('Retained job result identity differs from immutable request')
        atomic_json(Path(config['results']) / (request['job_id'] + '.json'), answer, 0o644)
        return answer
    registry = json.loads(root_file(config['registry']).read_text())
    if request['evaluation_id'] not in registry:
        raise ValueError('Evaluation ID has not been registered by operator')
    entry = registry[request['evaluation_id']]
    if entry.get('enabled', True) is not True:
        raise ValueError('Evaluation ID retired by operator')
    dispatch = entry.get('dispatch', 'solver')
    standalone_acs = dispatch == 'quality_diagnostic' and entry.get('quality_backend') == 'acs' and entry.get('registration_kind') == 'source_bound_quality_v1'
    if standalone_acs:
        from register_acs_quality import validate_registration
        validate_registration(entry, request)
        template = {}
    else:
        template = validated_template(entry, request)
    if dispatch not in {'solver', 'quality_diagnostic', 'semantic_diagnostic'}:
        raise ValueError('Unknown registered dispatch')
    if dispatch == 'quality_diagnostic':
        quality_backend = entry.get('quality_backend', 'chroot')
        if quality_backend == 'acs':
            from run_acs_quality import load_settings
            quality_settings = root_file(entry['acs_quality_settings'])
            if hashlib.sha256(quality_settings.read_bytes()).hexdigest() != entry['acs_quality_settings_sha256']:
                raise ValueError('Registered ACS quality settings changed')
            settings, quality_task = load_settings(quality_settings)
            if (settings['task_sha256'] != entry['snapshot_sha256'] or quality_task.resolve() != Path(entry['snapshot']).resolve()
                    or settings['runtime_image'] != (entry['runtime_image'] if standalone_acs else template['environment']['kwargs']['runtime_image'])):
                raise ValueError('ACS quality task differs from registered snapshot')
        elif quality_backend == 'chroot':
            from role_exec import trusted
            review_root = trusted(entry['review_root'], directory=True)
            if not review_root.is_relative_to(W / '.state/science-review-tools') or digest_tree(review_root / 'task') != entry['snapshot_sha256']:
                raise ValueError('Registered review root/task mismatch')
        else:
            raise ValueError('Unknown quality backend')
    if dispatch == 'semantic_diagnostic':
        from register_semantic_diagnostic import registered_corpus
        corpus = registered_corpus(entry)
    # Serialize reservations, not execution. Active sibling workers are allowed;
    # abandoned or completed-but-unclean jobs still block further cloud work.
    with (state / 'reservation.lock').open('a') as reservation:
        fcntl.flock(reservation, fcntl.LOCK_EX)
        for claim_path in state.glob('*/claim.json'):
            retained = claim_path.parent / 'result.json'
            if retained.exists():
                if json.loads(retained.read_text()).get('cleanup_confirmed') is True:
                    continue
                raise ValueError('Prior job cleanup is not confirmed')
            claim = json.loads(root_file(claim_path).read_text())
            if not active_queue_worker(claim, claim_path.parent.name):
                raise ValueError('Prior job cleanup is not confirmed')
        day = datetime.now(timezone.utc).date().isoformat()
        count = sum(json.loads(path.read_text()).get('day') == day for path in state.glob('*/claim.json'))
        if config['max_jobs_per_day'] and count >= config['max_jobs_per_day']:
            raise ValueError('Daily dispatch budget exhausted')
        job.mkdir(mode=0o700)
        atomic_json(job / 'claim.json', {'day': day, **request, 'mock': mock, 'queue_worker_pid': os.getpid()})
    answer = {**request, 'provider': 'dawnfm', 'model': 'deepseek-v4.1-flash-1017'}
    stop_requested = False
    private = None
    if mock:
        answer.update(status='mock_complete', quality_acceptance=False, live_model_calls=0, cleanup_confirmed=True)
    else:
        template.update(job_name='tbs-universe-linfeng-queue-' + request['job_id'], jobs_dir=str(job / 'runs'))
        def relabel(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'evaluation-run':
                        value[key] = template['job_name']
                    else:
                        relabel(item)
            elif isinstance(value, list):
                for item in value:
                    relabel(item)
        relabel(template)
        options = {} if standalone_acs else template['agents'][0].setdefault('kwargs', {})
        # Codex already retains native logs and ATIF in the root-private job directory.
        # The mini-swe adapter needs an explicit extra retention destination.
        retain_private = options.pop('retain_private_trajectory', True)
        if dispatch == 'solver' and retain_private is not True:
            raise ValueError('Solver trajectory retention is required')
        legacy_retention = not standalone_acs and template['agents'][0].get('import_path') == 'universe_metrics:MetadataOnlyMiniSwe'
        if dispatch == 'solver' and retain_private and legacy_retention:
            private_root = W / '.state/science-private-trajectories'
            private_root.mkdir(mode=0o700, exist_ok=True)
            info = private_root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Private trajectory storage ownership/mode refused')
            private = private_root / request['job_id']
            private.mkdir(mode=0o700)
            atomic_json(private / 'identity.json', {'evaluation_id': request['evaluation_id'], 'snapshot_sha256': entry['snapshot_sha256'], 'snapshot_path': entry['snapshot'], 'provider': 'dawnfm', 'model': 'deepseek-v4.1-flash-1017', 'runtime_image': template['environment']['kwargs']['runtime_image'], 'verifier_image': template['environment']['kwargs']['verifier_image']})
            options['private_trajectory_dir'] = str(private)
        atomic_json(job / 'config.json', template)
        env = os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(Path(__file__).parent / 'harbor-extensions') + ':' + str(Path(__file__).parent) + ':' + str(W / '.local/harbor-extensions'))
        if dispatch == 'quality_diagnostic':
            if quality_backend == 'acs':
                command = [str(W / 'gewu-multiverse/deploy/science/run-acs-quality'), '--settings', str(quality_settings), '--summary-output', str(job / 'quality-summary.json')]
            else:
                command = [str(W / 'gewu-multiverse/deploy/science/run-quality-diagnostic'), '--evaluation-id', request['evaluation_id'], '--root', str(review_root), '--job', str(job / 'quality')]
        elif dispatch == 'semantic_diagnostic':
            command = [str(W / 'gewu-multiverse/deploy/science/run-semantic-diagnostic'), '--evaluation-id', request['evaluation_id'], '--corpus', str(corpus), '--output', str(job / 'semantic')]
        else:
            command = [str(W / 'gewu-multiverse/deploy/science/run-acs-evaluation'), str(job / 'config.json'), str(job / 'report.json')]
        with (job / 'controller.log').open('w') as log:
            child = None
            def stop(_number, _frame):
                nonlocal stop_requested
                stop_requested = True
                if child is not None and child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
            previous = signal.signal(signal.SIGTERM, stop)
            try:
                child = subprocess.Popen(command, stdout=log, stderr=log, env=env, start_new_session=True)
                atomic_json(job / 'claim.json', {'day': day, **request, 'mock': False, 'controller_pid': child.pid, 'queue_worker_pid': os.getpid()})
                if stop_requested:
                    stop(None, None)
                try:
                    child.wait(timeout=75600 if dispatch == 'quality_diagnostic' and quality_backend == 'acs' else 15600 if dispatch == 'solver' else 1800)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=180)
            finally:
                signal.signal(signal.SIGTERM, previous)
        if dispatch == 'quality_diagnostic':
            summary = job / ('quality-summary.json' if quality_backend == 'acs' else 'quality/summary.json')
            if summary.exists():
                answer.update(json.loads(root_file(summary).read_text()))
            else:
                from role_exec import role_processes
                import time
                deadline = time.monotonic() + 10
                while role_processes(21012) and time.monotonic() < deadline:
                    time.sleep(.1)
                answer.update(status='quality_diagnostic_failed', cleanup_confirmed=quality_backend != 'acs' and not role_processes(21012), quality_acceptance=False, scientific_acceptance=False, release_eligible=False, factual_verification_required=True)
        elif dispatch == 'semantic_diagnostic':
            report_path = job / 'semantic/report.json'
            if report_path.exists():
                report = json.loads(root_file(report_path).read_text())
                from semantic_comparison import load_corpus, validate_report
                records, manifest = load_corpus(corpus)
                validate_report(report, records, manifest, entry['snapshot_sha256'], (Path(entry['snapshot']) / 'instruction.md').read_text())
                answer.update(status='semantic_diagnostic_complete', comparison=report)
            else:
                answer.update(status='semantic_diagnostic_failed')
            answer.update(cleanup_confirmed=True, quality_acceptance=False, scientific_acceptance=False, release_eligible=False, factual_verification_required=True, acs_launched=False, raw_trajectory_exported=False)
        elif not (job / 'report.json').exists():
            answer.update(status='failed', cleanup_confirmed=False, quality_acceptance=False)
        else:
            report = json.loads((job / 'report.json').read_text())
            result_path = job / 'runs' / template['job_name'] / 'result.json'
            unchanged = digest_tree(Path(entry['snapshot'])) == entry['snapshot_sha256']
            from execution_evidence import inspect, materialize_private_trajectory
            if private is not None and (private/'retention.json').exists():
                try:materialize_private_trajectory(private,result_path,entry,template)
                except (OSError,ValueError,KeyError,TypeError) as error:
                    answer['private_trajectory_bridge_error']=type(error).__name__
            evidence = inspect(report, result_path, template, entry['snapshot_sha256'], unchanged)
            atomic_json(job / 'evidence-manifest.json', evidence)
            answer.update(aggregate(report, result_path, template, entry['snapshot_sha256'], unchanged))
            # Failed runs also need a diagnostic packet when the verifier never starts.
            if evidence['checks']['trajectory_retained'] or evidence['checks']['verifier_retained']:
                from publish_evidence import publish
                from selftest_logs import credential_values
                packet_root = Path(config['results']).parent / 'evidence'
                packet_root.mkdir(mode=0o750, exist_ok=True)
                os.chown(packet_root, 0, config['evaluator_uid'])
                if packet_root.is_symlink() or packet_root.stat().st_mode & 0o022:
                    raise ValueError('Unsafe evaluator evidence directory')
                packet = packet_root / request['job_id']
                trial = next(result_path.parent.glob('*/result.json')).parent
                receipt = publish(trial, packet, credential_values(Path(config['results']).parents[1]), config['evaluator_uid'])
                # Only the role-visible relative reference is returned; raw paths stay private.
                answer['evidence_packet'] = '/evaluation/evidence/' + request['job_id']
                answer['sanitized_trajectory_exported'] = receipt['trajectory_available']
                binding = {'run_id': report['run_id'], 'snapshot_sha256': entry['snapshot_sha256'],
                           'evidence_manifest_sha256': answer['evidence_manifest_sha256'],
                           'pod_evidence': report.get('pod_observations', {}),
                           'execution_identity': report.get('execution_identity', {}),
                           'cleanup_confirmed': answer.get('cleanup_confirmed') is True}
                atomic_json(packet / 'execution-binding.json', binding, 0o640)
                os.chown(packet / 'execution-binding.json', 0, config['evaluator_uid'])

        if private is not None:
            # A retained trace is not reviewable until execution identity and
            # cleanup are checked again after the guard exits.
            unchanged = digest_tree(Path(entry['snapshot'])) == entry['snapshot_sha256']
            retention = private / 'retention.json'
            ready = False
            if retention.exists():
                metadata = json.loads(root_file(retention).read_text())
                ready = metadata.get('review_ready') is True and metadata.get('snapshot_sha256') == entry['snapshot_sha256']
                for filename, key in [('identity.json', 'identity_sha256'), ('mini-swe-agent.trajectory.json', 'raw_sha256'), ('trajectory.json', 'atif_sha256')]:
                    if ready:
                        ready = hashlib.sha256(root_file(private / filename).read_bytes()).hexdigest() == metadata.get(key)
            atomic_json(private / 'execution-binding.json', {'snapshot_sha256': entry['snapshot_sha256'], 'snapshot_unchanged_after_run': unchanged, 'solver_status': 'cancelled' if stop_requested else answer.get('status'), 'cleanup_confirmed': answer.get('cleanup_confirmed') is True, 'trajectory_retained': ready, 'review_input_eligible': not stop_requested and unchanged and ready and answer.get('status') == 'solver_complete' and answer.get('cleanup_confirmed') is True, 'scientific_acceptance': False})
    if stop_requested:
        answer['status'] = 'cancelled'
    atomic_json(job / 'result.json', answer)
    atomic_json(Path(config['results']) / (request['job_id'] + '.json'), answer, 0o644)
    if stop_requested:
        raise SystemExit(0)
    return answer


def active_queue_worker(claim, job_id):
    pid = claim.get('queue_worker_pid')
    if type(pid) is not int or pid <= 1:
        return False
    try:
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        script = os.fsencode(str(Path(__file__).resolve()))
        flag = argv.index(b'--single-request')
        request = Path(os.fsdecode(argv[flag + 1]))
        return script in argv and request.stem == job_id and '--queue-index'.encode() in argv
    except (OSError, ValueError, IndexError):
        return False


def handle_request(config, path, mock):
    try:
        process(config, path, mock)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError):
        atomic_json(Path(config['results']) / path.name,
                    {'job_id': path.stem, 'status': 'rejected', 'quality_acceptance': False}, 0o644)


def parallel_limit(config):
    if not isinstance(config, dict):
        raise ValueError('Queue config must be an object')
    broker_path = config.get('broker_config')
    if not isinstance(broker_path, str) or not broker_path:
        raise ValueError('Queue requires the operator-owned author roster in broker_config')
    broker = json.loads(root_file(broker_path).read_text())
    roles = broker.get('roles') if isinstance(broker, dict) else None
    if not isinstance(roles, dict) or any(not isinstance(role, dict) for role in roles.values()):
        raise ValueError('Author roster requires a roles object with role entries')
    # Waiting/paused authors still count: their submissions may be in review.
    authors = sum(name == 'author' or name.startswith('author-') for name in roles)
    if not authors:
        raise ValueError('Evaluation capacity requires at least one configured author')
    ceiling = 5 * authors
    value = config.get('max_parallel_jobs', ceiling)
    if type(value) is not int or not 1 <= value <= ceiling:
        raise ValueError(f'max_parallel_jobs must be between 1 and {ceiling} (author count x 5)')
    return value


def dispatch_paused(config):
    value = config.get('dispatch_paused', False)
    if type(value) is not bool:
        raise ValueError('dispatch_paused must be a boolean')
    return value


def adopt_workers(config, queues, active):
    """Recover active reservations after replacing only the queue supervisor."""
    from queue_parallel import RetainedWorker
    adopted = []
    for queue in queues:
        for path in Path(queue['inbox']).glob('*.json'):
            if not ID.fullmatch(path.stem) or str(path) in active.children:
                continue
            claim_path = Path(config['state']) / path.stem / 'claim.json'
            if not claim_path.exists():
                continue
            claim = json.loads(root_file(claim_path).read_text())
            if not active_queue_worker(claim, path.stem):
                continue
            try:
                child = RetainedWorker(claim['queue_worker_pid'])
            except ProcessLookupError:
                continue
            # Revalidate after opening the pidfd, then compare the actual
            # request bytes and UID before binding the reservation to an inbox.
            if not active_queue_worker(claim, path.stem) or child.poll() is not None:
                child.close()
                continue
            try:
                request = read_request(path, queue['evaluator_uid'])
                if any(claim.get(key) != request[key] for key in REQUEST_KEYS):
                    raise ValueError('Active claim differs from immutable request')
            except BaseException:
                child.close()
                raise
            active.children[str(path)] = child
            adopted.append({'job_id': path.stem, 'pid': child.pid})
    return adopted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--mock', action='store_true', help='No LLM/container; report explicitly mock')
    parser.add_argument('--owner-pid', type=int, help='Stop with the owning universe launcher')
    parser.add_argument('--single-request', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--queue-index', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = json.loads(root_file(args.config).read_text())
    registry = json.loads(root_file(config['registry']).read_text())
    if not isinstance(registry, dict) or not registry:
        raise SystemExit('An operator-registered evaluation catalog is required')
    queues = config.get('role_queues') or [{key:config[key] for key in ('inbox','results','evaluator_uid')}]
    budget = config.get('max_jobs_per_day', 0)
    if type(budget) is not int or budget < 0:
        raise SystemExit('Dispatch budget must be nonnegative; zero means unlimited')
    config['max_jobs_per_day'] = budget
    identities = set()
    for queue in queues:
        uid = queue.get('evaluator_uid')
        if type(uid) is not int or uid < 20000 or uid in identities:
            raise SystemExit('Evaluator requires a distinct dedicated nonroot UID')
        identities.add(uid)
        for key in ('inbox', 'results', 'state'):
            path = Path(config['state'] if key == 'state' else queue[key])
            info = path.lstat()
            expected = uid if key == 'inbox' else 0
            local_runtime=str(path).startswith('/tmp/gewu-terminal-bench-science-runtime-')
            if path.is_symlink() or (not path.is_relative_to(W) and not local_runtime) or not path.is_dir() or info.st_uid not in ({expected,0} if local_runtime else {expected}) or info.st_mode & 0o022:
                raise SystemExit('Queue directory ownership/type/mode refused')
    if args.single_request is not None:
        if not 0 <= args.queue_index < len(queues):
            raise SystemExit('Invalid role queue')
        selected = {**config, **queues[args.queue_index]}
        path = args.single_request
        if path.parent.resolve() != Path(selected['inbox']).resolve() or not ID.fullmatch(path.stem):
            raise SystemExit('Request outside selected inbox')
        handle_request(selected, path, args.mock)
        return
    from queue_parallel import ActiveJobs
    parallel = parallel_limit(config)
    paused = dispatch_paused(config)
    from command_watchdog import process_info, owner_alive
    try:
        owner = process_info(args.owner_pid) if args.owner_pid is not None else None
    except (OSError, ValueError, IndexError):
        raise SystemExit('Universe launcher is not alive') from None
    with (Path(config['state']) / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        active = ActiveJobs(parallel)
        stopping = False
        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True
        previous = signal.signal(signal.SIGTERM, stop)
        supervising = False
        try:
            adopted = adopt_workers(config, queues, active)
            def publish_ready():
                if args.ready_file is not None:
                    atomic_json(args.ready_file, {'pid': os.getpid(), 'evaluator_uids': sorted(identities),
                                'max_parallel_jobs': active.limit, 'adopted_workers': adopted,
                                'owner_pid': args.owner_pid, 'dispatch_paused': paused})
            if args.ready_file is not None:
                if args.ready_file.parent.resolve() != Path(config['state']).resolve():
                    raise SystemExit('Readiness file must be in the private worker state directory')
            publish_ready()
            supervising = True
            import time
            next_reload = 0
            queue_offset = 0
            while not stopping and owner_alive(owner):
                if time.monotonic() >= next_reload:
                    # Refresh capacity (including author roster changes) and
                    # admission pause. Evaluator identities/paths stay fixed.
                    try:
                        updated = json.loads(root_file(args.config).read_text())
                        if not isinstance(updated, dict):
                            raise ValueError('Queue config must be an object')
                        if updated.get('broker_config') != config.get('broker_config'):
                            raise ValueError('Author roster path needs a validated restart')
                        limit = parallel_limit(updated)
                        updated_pause = dispatch_paused(updated)
                    except (OSError, ValueError, TypeError):
                        pass  # Keep the last valid capacity on partial/bad updates.
                    else:
                        if limit != active.limit or updated_pause != paused:
                            active.limit = limit
                            paused = updated_pause
                            publish_ready()
                    next_reload = time.monotonic() + 1
                active.reap()
                for delta in range(0 if paused else len(queues)):
                    index = (queue_offset + delta) % len(queues)
                    queue = queues[index]
                    for path in sorted(Path(queue['inbox']).glob('*.json')):
                        if not ID.fullmatch(path.stem) or (Path(queue['results']) / path.name).exists():
                            continue
                        command = [sys.executable, str(Path(__file__).resolve()), '--config', str(args.config.resolve()),
                                   '--single-request', str(path.resolve()), '--queue-index', str(index)]
                        if args.mock:
                            command.append('--mock')
                        active.start(str(path), command)
                queue_offset = (queue_offset + 1) % len(queues)
                if args.once:
                    # One scan, then await its active children. Remaining requests
                    # stay queued for the next invocation when capacity is full.
                    while active.children and not stopping and owner_alive(owner):
                        active.reap()
                        import time
                        time.sleep(.1)
                    return
                import time
                time.sleep(.2)
        finally:
            if supervising:
                active.stop()
            else:
                # Startup failure must leave existing executions intact; no
                # new jobs are started before the readiness record is written.
                for child in active.children.values():
                    child.close()
            signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    main()
