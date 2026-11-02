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
import stat
import subprocess
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


def source_signature(workspace, *, details=None, max_seconds=5):
    """Observe Git changes plus non-Git source, not every immutable baseline file.

    No index refresh writes, hooks, filters, lazy fetch or cloud clients are
    permitted. An inaccessible repository or exceeded bound remains unknown.
    """
    root=Path(workspace).resolve(strict=True);deadline=time.monotonic()+max_seconds
    entries=[];visited=set();counts={'files':0,'directories':0,'repositories':0,'git_commands':0}
    def budget():
        remaining=deadline-time.monotonic()
        if remaining<=0 or counts['files']>6000 or counts['directories']>2000:
            raise ValueError('Workspace observation budget reached; manual inspection required')
        return remaining
    def relative(path):
        if not path.is_relative_to(root) or '..' in path.relative_to(root).parts:raise ValueError('Source path escaped workspace')
        return str(path.relative_to(root))
    def ignored(path):
        return any(part in IGNORED for part in path.relative_to(root).parts)
    def git(repo,gitdir,args,options=(),allow_empty=False):
        budget();counts['git_commands']+=1
        env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_OPTIONAL_LOCKS='0',
                   GIT_NO_LAZY_FETCH='1',GIT_TERMINAL_PROMPT='0',LC_ALL='C')
        command=['/usr/bin/git','-c','safe.directory='+str(repo),'-c','core.fsmonitor=false',
                 '-c','core.untrackedCache=false','-c','core.hooksPath=/dev/null',
                 '-c','core.attributesFile=/dev/null','-c','core.excludesFile=/dev/null',
                 '-c','submodule.recurse=false','-c','protocol.allow=never',*options,
                 '--git-dir='+str(gitdir),'--work-tree='+str(repo),*args]
        try:result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,env=env,timeout=budget())
        except subprocess.TimeoutExpired:
            raise ValueError('Git observation budget reached; manual inspection required') from None
        if len(result.stdout)+len(result.stderr)>2*1024*1024:
            raise ValueError('Git observation output bound reached')
        if result.returncode and not (allow_empty and result.returncode==1):
            raise ValueError('Git source inspection failed; manual inspection required')
        return result.stdout
    def repository(repo):
        counts['repositories']+=1;marker=repo/'.git'
        if marker.is_symlink():raise ValueError('Linked Git metadata refused')
        if marker.is_dir():gitdir=marker
        else:
            if marker.stat().st_size>4096:raise ValueError('Oversized Git worktree reference')
            value=marker.read_text().strip()
            if not value.startswith('gitdir: '):raise ValueError('Invalid Git worktree reference')
            value=value[8:];pointer=Path(value)
            # A role sees its own workspace as /workspace. Translate that
            # reference read-only for linked worktrees; never edit .git files.
            gitdir=root/pointer.relative_to('/workspace') if pointer.is_relative_to('/workspace') else pointer if pointer.is_absolute() else repo/pointer
        gitdir=gitdir.resolve(strict=True)
        if not gitdir.is_relative_to(root):raise ValueError('Git metadata escaped workspace')
        common=gitdir/'commondir'
        if common.exists():
            if common.is_symlink() or common.stat().st_size>4096:raise ValueError('Unsafe common Git directory')
            if not (gitdir/common.read_text().strip()).resolve(strict=True).is_relative_to(root):
                raise ValueError('Common Git metadata escaped workspace')
        # git status may invoke clean/process filters while refreshing its
        # in-memory index. Override every configured driver before observation.
        keys=git(repo,gitdir,['config','--null','--name-only','--get-regexp',r'^filter\..*\.(clean|process|required)$'],allow_empty=True)
        options=[]
        for raw in keys.split(b'\0'):
            if not raw:continue
            key=raw.decode()
            if not re.fullmatch(r'filter\..+\.(clean|process|required)',key):raise ValueError('Invalid Git filter setting')
            options.extend(['-c',key+('=false' if key.endswith('.required') else '=')])
        head=git(repo,gitdir,['rev-parse','--verify','--quiet','HEAD'],options,allow_empty=True).strip()
        index_path=gitdir/'index'
        def index_stamp():
            if not index_path.exists():return None
            s=index_path.stat();return (s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        before=index_stamp()
        index=git(repo,gitdir,['ls-files','-v','--stage','-z'],options)
        submodules=[]
        for line in index.split(b'\0'):
            if not line:continue
            meta,name=line.split(b'\t',1);fields=meta.split()
            # Git deliberately omits worktree changes behind these index flags.
            # Do not call the author stalled when those bytes were unobserved.
            if fields[0].islower() or fields[0]==b'S':raise ValueError('Git index hides worktree changes; manual inspection required')
            if fields[1]==b'160000':submodules.append(os.fsdecode(name))
        status=git(repo,gitdir,['status','--porcelain=v1','-z','--untracked-files=all',
                              '--ignored=matching','--ignore-submodules=all','--no-renames'],options)
        entries.append(('git',relative(repo),head.decode(),hashlib.sha256(index).hexdigest(),hashlib.sha256(status).hexdigest()))
        for line in status.split(b'\0'):
            if not line:continue
            if len(line)<4 or line[2:3]!=b' ':raise ValueError('Unrecognized Git status entry')
            changed=repo/os.fsdecode(line[3:]).rstrip('/')
            if not changed.is_relative_to(repo) or '..' in changed.relative_to(repo).parts:
                raise ValueError('Unsafe Git status path')
            walk(changed)
        for name in submodules:walk(repo/name)
        if before!=index_stamp() or git(repo,gitdir,['rev-parse','--verify','--quiet','HEAD'],options,allow_empty=True).strip()!=head:
            raise ValueError('Repository changed during source observation')
    def walk(path):
        budget();relative(path)
        if path in visited or ignored(path):return
        visited.add(path)
        try:before=path.lstat()
        except FileNotFoundError:
            entries.append(('missing',relative(path)));return
        if stat.S_ISDIR(before.st_mode):
            counts['directories']+=1;budget()
            if (path/'.git').exists() or (path/'.git').is_symlink():
                repository(path);return
            for child in sorted(path.iterdir()):walk(child)
            return
        counts['files']+=1;budget()
        if stat.S_ISLNK(before.st_mode):value=('link',os.readlink(path))
        elif stat.S_ISREG(before.st_mode):
            # Stat/ctime covers large downloaded tools and data without reading
            # their gigabytes; small active sources also receive a content hash.
            value=('file',hashlib.sha256(path.read_bytes()).hexdigest() if before.st_size<=65536 else None)
        else:raise ValueError('Special source file cannot be observed safely')
        after=path.lstat()
        stamp=lambda s:(s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if stamp(before)!=stamp(after):raise ValueError('Source changed during observation')
        entries.append((relative(path),*stamp(after),value))
    walk(root);budget()
    if details is not None:details.update(counts)
    return hashlib.sha256(json.dumps(['git-aware-v1',sorted(entries,key=lambda x:json.dumps(x))]).encode()).hexdigest()


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
            save(path, dict(existing, action='inspection_required', error_type=type(error).__name__,
                            observed_at=now, stable_since=now, source_signature=None))
