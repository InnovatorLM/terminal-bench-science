"""Conservative author reset from repeated failures and independently observed stasis.

The hourly external observer remains advisory. This operator watchdog can request
one controller-coordinated reset per unchanged workspace signature, retaining all
work and iteration clocks. Waiting or insufficient evidence never triggers reset.
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import time

WAIT_STATES = {'reviewing', 'waiting_platform', 'validating', 'waiting_builder',
               'building', 'running_oracle', 'running_nop', 'queued', 'running'}
IGNORED = {'.git', '.cache', '.venv', 'node_modules', '__pycache__', '.mypy_cache'}


def read_json(path):
    if path.is_symlink():
        raise ValueError('Linked control evidence refused')
    return json.loads(path.read_text())


def save(path, payload):
    temporary = path.with_suffix('.pending')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream, indent=2)
    temporary.replace(path)


def source_signature(workspace):
    """Bound the scan; unknown/oversized trees are never deemed stalled."""
    entries = [];started=time.monotonic();directories=0
    def onerror(error):raise error
    for directory, names, files in os.walk(workspace, followlinks=False, onerror=onerror):
        directories+=1
        if directories>2000 or time.monotonic()-started>5:
            raise ValueError('Workspace observation budget reached')
        names[:] = sorted(n for n in names if n not in IGNORED and not (Path(directory)/n).is_symlink())
        for name in sorted(files):
            p = Path(directory)/name
            if p.is_symlink():
                continue
            s = p.stat()
            entries.append((str(p.relative_to(workspace)), s.st_size, s.st_mtime_ns))
            if len(entries) > 6000 or time.monotonic()-started>5:
                raise ValueError('Workspace scan bound reached; manual inspection required')
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def failures(run, minimum=3):
    """Hash repeated failing outputs; never place arbitrary outputs in receipts."""
    path = run/'transcript.jsonl'
    if path.is_symlink():
        raise ValueError('Linked transcript refused')
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        offset = max(0, size-2*1024*1024)
        stream.seek(offset)
        if offset:
            stream.readline()
        lines = stream.read().splitlines()
    counts = collections.Counter()
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('event') != 'tool_result':
            continue
        output = event.get('output', '')
        if not isinstance(output, str):
            continue
        # Expected nop/negative-control failure is not a repeated defect.
        if any(word in output.lower() for word in ('negative control', 'negative_control', 'reward=0', 'reward: 0')):
            continue
        if event.get('exit_code') not in (None, 0) and output.strip():
            # Tracebacks include changing call-site lines and unrelated stdout.
            # Preserve the exception's type and message, rather than requiring
            # the full tool output to be byte-identical to an earlier failure.
            exceptions=re.findall(r'(?m)^([A-Za-z_][\w.]*(?:Error|Exception):[^\r\n]+)$',output)
            signature=exceptions[-1] if exceptions else output.strip()
            counts[hashlib.sha256(signature.encode()).hexdigest()] += 1
    return {digest for digest, count in counts.items() if count >= minimum}


def decision(previous, observation, now):
    same = previous.get('source_signature') == observation['source_signature']
    stable_since = previous.get('stable_since', now) if same else now
    result = dict(previous, **observation)
    result.update(stable_since=stable_since, observed_at=now,
                  action='observe', last_attempt=previous.get('last_attempt', 0),
                  attempted_signature=previous.get('attempted_signature'))
    # Waiting is not iteration time. Restart the stasis window after any wait,
    # command execution, missing runner or insufficient transcript evidence.
    if observation.get('waiting') or not observation.get('runner_live'):
        result.update(action='waiting', stable_since=now)
    elif not observation.get('shared_failure') or (
            len(observation.get('sessions', [])) < 3 and not observation.get('sustained_session_failure')):
        result.update(action='insufficient_evidence')
    elif result['attempted_signature'] == observation['source_signature']:
        result['action'] = 'already_attempted'
    elif now - stable_since >= 3600 and now - result['last_attempt'] >= 21600:
        result['action'] = 'reset'
    return result


def observe(role, entry, config, session_state):
    from iteration_lifecycle import runner_state
    from command_watchdog import process_info
    root = Path(entry['root']).resolve()
    workspace = root/'workspace'
    if workspace.is_symlink():
        raise ValueError('Linked workspace refused')
    runs = sorted((session_state/role).glob('run-*'), key=lambda p:p.stat().st_mtime_ns)[-3:]
    waiting = []
    feedback = root/'review-feedback.json'
    if feedback.exists() and read_json(feedback).get('state') in WAIT_STATES:
        waiting.append('review_or_platform')
    requests = sorted((root/'selftest/inbox').glob('*.json'), key=lambda p:p.stat().st_mtime_ns)
    successes=[]
    for request in requests:
        result = root/'selftest/results'/request.name
        if not result.exists() or read_json(result).get('status') in WAIT_STATES:
            waiting.append('selftest');break
        result_data=read_json(result)
        if result_data.get('status')=='selftest_passed':
            successes.append(request.stem)
        if request == requests[-1] and str(read_json(result).get('error_class', '')).startswith(('platform_', 'infrastructure')):
            # An unresolved platform error is not the author's dead end.
            waiting.append('selftest_platform');break
    uptime=float(Path('/proc/uptime').read_text().split()[0]);hz=os.sysconf('SC_CLK_TCK')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try: info = process_info(proc.name)
        except (OSError, ValueError, IndexError):continue
        # Short diagnostic tools are normal during a loop. Only a sustained
        # local process protects an ongoing computation from intervention.
        if (info['uid'] == entry['uid'] and info['state'] != 'Z'
                and uptime-info['start_ticks']/hz>=300):
            waiting.append('local_command');break
    if waiting:
        return dict(source_signature=None,sessions=[run.name for run in runs],
                    shared_failure=[],runner_live=False,waiting=sorted(set(waiting)))
    sets = [failures(run) for run in runs]
    shared = sorted(set.intersection(*sets)) if len(sets) == 3 else []
    # Sessions have no step cap and may last hours. Nine repeated failures in
    # one live session must not require two artificial restarts to be noticed.
    sustained=sorted(failures(runs[-1],minimum=9)) if runs else []
    evidence=shared or sustained
    live=bool(runs and runner_state(runs[-1], config['controller_uid']))
    source=source_signature(workspace) if evidence and live else None
    if source is not None:
        # A newly successful self-test is progress even when its source bytes
        # were already frozen. It restarts the stasis window once, not per poll.
        source=hashlib.sha256(json.dumps([source,sorted(successes)]).encode()).hexdigest()
    return dict(source_signature=source,
                sessions=[run.name for run in runs], shared_failure=evidence,
                sustained_session_failure=bool(sustained),
                runner_live=live,
                waiting=sorted(set(waiting)))


def scan(config, session_state, state, now=None, reset_action=None):
    """Called by the existing watchdog; failures become receipts, never retries."""
    import role_reset
    session_state, state = Path(session_state), Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time() if now is None else now
    for role, entry in config['roles'].items():
        if not role.startswith('author-'):continue
        path = state/(role+'.json')
        try:
            previous = read_json(path) if path.exists() else {}
            # Expensive transcript/workspace inspection need not run with the
            # 30-second command timeout watchdog. Five minutes is ample for a
            # one-hour stasis policy and avoids repeated CPFS metadata walks.
            if now-previous.get('observed_at',0)<300:continue
            current = decision(previous, observe(role, entry, config, session_state), now)
            save(path, current)
            if current['action'] != 'reset':continue
            current.update(last_attempt=now, attempted_signature=current['source_signature'], action='reset_requested')
            # Persist attempt before mutation: a crash must not repeatedly reset.
            save(path, current)
            reason = 'Repeated identical failures across three sessions or nine times in one session; workspace unchanged for at least one non-waiting hour.'
            guidance = 'Read the preserved failure and task state once. Change the approach or abandon the idea; do not repeat unchanged failing commands. Preserve the existing five-hour idea clock.'
            if reset_action is None:
                old_u, old_state = role_reset.U, role_reset.DEPLOYMENT_STATE
                try:
                    role_reset.U = session_state.parent.parent
                    role_reset.DEPLOYMENT_STATE = session_state
                    receipt = role_reset.online_reset(role, reason, guidance, apply=True)
                finally:
                    role_reset.U, role_reset.DEPLOYMENT_STATE = old_u, old_state
            else:
                receipt = reset_action(role, reason, guidance, apply=True)
            current.update(action='reset_ready', reset_id=receipt['reset_id'],
                           restart_verified=False, work_and_trajectories_preserved=True)
            save(path, current)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            # Preserve attempt/cooldown metadata and avoid publishing role data.
            try:existing = read_json(path) if path.exists() else {}
            except (OSError,ValueError):existing={'last_attempt':now}
            save(path, dict(existing, action='inspection_required', error_type=type(error).__name__, observed_at=now))
