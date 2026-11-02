"""Operator-only role reset; retains archives/workspaces and defers restart to controller."""
import argparse, contextlib, fcntl, json, os, signal, time, uuid
from pathlib import Path
from command_watchdog import process_info

W = Path('/mnt/data/zhanglinfeng/workspace')
from deployment_paths import universe_root
U = universe_root(W)
DEPLOYMENT_STATE=U/'.state/gewu-multiverse'
BEGIN = '<!-- operator-reset:start -->'
END = '<!-- operator-reset:end -->'

@contextlib.contextmanager
def maintenance_lock(directory):
    """Launcher holds a shared lifetime lock; mutations require exclusive access."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / 'lifecycle.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Deployment is active; stop its launcher and supervisor before reset') from None
        yield
    finally:
        os.close(fd)

def directive(text, reset_id, reason, guidance):
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            raise ValueError('Malformed reset block')
        a, rest = text.split(BEGIN, 1)
        _, b = rest.split(END, 1)
        text = a.rstrip() + b
    block = '\n'.join([BEGIN, '## Operator reset: ' + reset_id,
        'Previous session was interrupted by the operator; this is not a model-answer failure.',
        'Reason: ' + reason, 'Next action: ' + guidance,
        'Preserve existing files, branches and trajectories. Inspect the prior handoff briefly; do not repeat an abandoned search unless the operator requests it.',
        'Keep the existing per-idea active-time ledger; reset does not grant a fresh 48-hour budget for the same idea.', END])
    return text.rstrip() + '\n\n' + block + '\n'

def identity(pid):
    info = process_info(pid)
    info['argv'] = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(bytes([0]))
    return info

def runner_matches(info, run, controller_uid):
    return (info['uid'] == controller_uid and info['state'] != 'Z'
        and b'--run-agent' in info['argv'] and str(run).encode() in info['argv'])

def signal_identity(info, sig):
    try:
        fd = os.pidfd_open(info['pid'])
        try:
            now = process_info(info['pid'])
            if (now['uid'], now['start_ticks']) != (info['uid'], info['start_ticks']):
                raise RuntimeError('Process identity changed')
            signal.pidfd_send_signal(fd, sig)
        finally:
            os.close(fd)
    except (FileNotFoundError, ProcessLookupError):
        pass

def alive(info):
    try:
        now = process_info(info['pid'])
        return now['state'] != 'Z' and (now['uid'], now['start_ticks']) == (info['uid'], info['start_ticks'])
    except (FileNotFoundError, ProcessLookupError):
        return False

def instruction_paths(root, universe, role):
    """The model controller and chroot may read different instruction copies."""
    candidates=[Path(root)/'instruction.md',Path(universe)/'.config/gewu-multiverse/agents'/role/'instruction.md']
    result=[]
    for path in candidates:
        if path.is_symlink():raise ValueError('Symlink instruction forbidden')
        if path.exists() and path.resolve() not in [p.resolve() for p in result]:
            if not path.is_file():raise ValueError('Invalid role instruction')
            result.append(path)
    if not result or not candidates[0].is_file():raise ValueError('Missing isolated role instruction')
    return result


def update_instructions(root, universe, role, directory, receipt, reason, guidance):
    # Read and validate every copy before mutating any. Keep distinct detailed
    # policies while replacing the same bounded operator directive in both.
    versions=[]
    for instruction in instruction_paths(root,universe,role):
        original=instruction.read_text()
        versions.append((instruction,original,directive(original,receipt['reset_id'],reason,guidance)))
    for index,(instruction,original,updated) in enumerate(versions):
        suffix='.instruction.before' if index==0 else '.controller-instruction.before'
        backup=directory/(receipt['reset_id']+suffix)
        backup.write_text(original);backup.chmod(0o600)
    for instruction,original,updated in versions:
        instruction.write_text(updated)
    receipt['instruction_copies_updated']=[str(path) for path,_,_ in versions]


def reset(role, reason, guidance, apply=False, grace=5, _online_nonce=None):
    if os.geteuid() != 0:
        raise PermissionError('Operator required')
    if not reason.strip() or not guidance.strip() or any(x in reason + guidance for x in (BEGIN, END)):
        raise ValueError('Explicit reason and next action required; marker text forbidden')
    config = json.loads((DEPLOYMENT_STATE / 'role-broker/config.json').read_text())
    if role not in config['roles']:
        raise ValueError('Unknown role')
    entry = config['roles'][role]
    root = Path(entry['root']).resolve()
    if not root.is_relative_to(W.resolve()):
        raise ValueError('Role root outside workspace')
    runs = sorted((U / '.state/gewu-multiverse' / role).glob('run-*'))
    targets = []
    for run in runs:
        marker = run / 'session.pid'
        if not marker.exists():
            continue
        try:
            info = identity(int(marker.read_text().strip()))
        except (FileNotFoundError, ProcessLookupError):
            continue
        if not runner_matches(info, run, config['controller_uid']):
            raise RuntimeError('PID marker identity mismatch; refusing reset')
        targets.append(info)
    # Role commands are broker children, often outside the model runner process group.
    # Only the dedicated configured UID is eligible; no host/controller/cloud jobs.
    uid = entry['uid']
    if uid < 1000 or sum(e['uid'] == uid for e in config['roles'].values()) != 1:
        raise ValueError('Dedicated non-root role UID required')
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            info = process_info(p.name)
        except (OSError, ValueError, IndexError):
            continue
        if info['uid'] == uid and info['state'] != 'Z':
            targets.append(info)
    targets = list({(i['pid'], i['start_ticks']): i for i in targets}.values())
    receipt = dict(role=role, reset_id=uuid.uuid4().hex, at=time.time(), reason=reason, guidance=guidance,
        prior_run=runs[-1].name if runs else None, applied=apply,
        processes=[{k:i[k] for k in ('pid','uid','start_ticks')} for i in targets],
        files_preserved=True, restart='controller-managed; new session not yet verified')
    if not apply:
        return receipt
    directory = DEPLOYMENT_STATE / 'role-reset'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lifecycle=contextlib.nullcontext() if _online_nonce else maintenance_lock(directory)
    role_lock=contextlib.nullcontext() if _online_nonce else (directory / (role + '.lock')).open('a')
    with lifecycle, role_lock as lock:
        if _online_nonce:
            verify_pause(role,_online_nonce,config)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / (receipt['reset_id'] + '.json')
        def save():
            path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2)); path.chmod(0o600)
        receipt['status'] = 'prepared'; save()
        # Preserve instruction ownership/mode for the unprivileged controller.
        update_instructions(root,U,role,directory,receipt,reason,guidance)
        for info in targets:
            signal_identity(info, signal.SIGTERM)
        end = time.monotonic() + grace
        while time.monotonic() < end and any(alive(i) for i in targets):
            time.sleep(.1)
        for info in targets:
            if alive(info):
                signal_identity(info, signal.SIGKILL)
        end = time.monotonic() + 2
        while time.monotonic() < end and any(alive(i) for i in targets):
            time.sleep(.1)
        receipt['survivors'] = [i['pid'] for i in targets if alive(i)]
        receipt['status'] = 'cleanup_failed' if receipt['survivors'] else 'reset_ready'
        save()
        if receipt['survivors']:
            raise RuntimeError('Processes survived; inspect reset receipt')
        return receipt

def verify_pause(role, nonce, config):
    marker=DEPLOYMENT_STATE/'operator-role-holds'/role
    if marker.is_symlink() or marker.read_text()!=nonce:
        raise RuntimeError('Operator pause marker changed; role remains held')
    path=DEPLOYMENT_STATE/role/'operator-hold-ack.json'
    if path.is_symlink() or path.stat().st_uid!=config['controller_uid']:
        raise RuntimeError('Pause acknowledgement ownership/type refused')
    ack=json.loads(path.read_text())
    pid=int((DEPLOYMENT_STATE/'controller.pid').read_text())
    info=identity(pid)
    if (ack.get('version')!=1 or ack.get('nonce')!=nonce or ack.get('role')!=role
        or ack.get('quiescent') is not True or ack.get('controller_pid')!=pid
        or info['uid']!=config['controller_uid'] or info['state']=='Z'
        or b'--run-agent' in info['argv']
        or b'gewu-multiverse' not in Path(os.fsdecode(info['argv'][0])).name.encode()):
        raise RuntimeError('Pause acknowledgement/controller identity mismatch')
    return info


def online_reset(role, reason, guidance, apply=False, grace=5, wait_seconds=120):
    if not apply:return reset(role,reason,guidance,False,grace)
    # Validate the request before publishing a hold; no files or processes change.
    reset(role,reason,guidance,False,grace)
    config=json.loads((DEPLOYMENT_STATE/'role-broker/config.json').read_text())
    directory=DEPLOYMENT_STATE/'role-reset';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(directory/'lifecycle.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        # Shares the live launcher's lock, but excludes maintenance mutations.
        fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        with (directory/(role+'.lock')).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            holds=DEPLOYMENT_STATE/'operator-role-holds'
            if holds.is_symlink():raise ValueError('Symlink hold directory forbidden')
            holds.mkdir(mode=0o755,exist_ok=True)
            if holds.stat().st_uid!=0 or holds.stat().st_mode&0o022:
                raise ValueError('Hold directory must be operator-owned and not writable by others')
            marker=holds/role;nonce=uuid.uuid4().hex
            hold_fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o644)
            with os.fdopen(hold_fd,'w') as stream:stream.write(nonce)
            deadline=time.monotonic()+wait_seconds
            while True:
                try:
                    controller=verify_pause(role,nonce,config);break
                except (OSError,ValueError,RuntimeError,IndexError):
                    if time.monotonic()>=deadline:
                        raise RuntimeError('Controller did not acknowledge pause; hold retained for operator recovery') from None
                    time.sleep(.1)
            # No automatic finally-release: failure must keep the role paused.
            receipt=reset(role,reason,guidance,True,grace,_online_nonce=nonce)
            if not alive(controller):raise RuntimeError('Controller changed during reset; hold retained')
            verify_pause(role,nonce,config)
            marker.unlink()
            receipt.update(mode='online',pause_nonce=nonce,pause_controller_pid=controller['pid'],
                hold_released=True,restart='controller-managed; new session not yet verified')
            path=directory/(receipt['reset_id']+'.json')
            path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2));path.chmod(0o600)
            return receipt
    finally:os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role'); parser.add_argument('--reason', required=True)
    parser.add_argument('--next-action', required=True); parser.add_argument('--apply', action='store_true')
    parser.add_argument('--online',action='store_true',help='Pause one role through controller acknowledgement; retain hold on failure')
    args = parser.parse_args()
    action=online_reset if args.online else reset
    print(json.dumps(action(args.role, args.reason, args.next_action, args.apply), ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
