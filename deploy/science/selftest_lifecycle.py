"""Durable selftest lifecycle; interrupted work is never silently skipped."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from evaluation_queue import atomic_json

TERMINAL = {'selftest_passed', 'selftest_failed', 'failed', 'interrupted'}

def owned_run(command, **kwargs):
    timeout = kwargs.pop('timeout', None)
    # The ACS guard needs time to terminate Harbor and confirm deletion of its
    # own resources.  Build commands retain a short termination grace.
    cleanup_grace = kwargs.pop('cleanup_grace', 3)
    with subprocess.Popen(command, start_new_session=True, **kwargs) as child:
        try:
            child.wait(timeout=timeout)
            # Wrappers may exit while descendants still mutate retained evidence.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # The leader may already be reaped while a child ignores SIGTERM.
            # Kill only the group created for this invocation, never other jobs.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return subprocess.CompletedProcess(command, child.returncode)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=cleanup_grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise

def recover_interrupted(work, result, publish):
    work, result = Path(work), Path(result)
    prior = {}
    malformed = None
    if result.exists():
        try:
            prior = json.loads(result.read_text())
            if not isinstance(prior,dict):raise ValueError('Non-object receipt')
        except (ValueError,UnicodeError):
            malformed = result.read_bytes()
            prior = {'receipt_recovery':'Malformed prior result retained; operator inspection required'}
        if prior.get('status') in TERMINAL:
            return True
    if not work.exists() and not prior:
        return False
    work.mkdir(mode=0o700,parents=True,exist_ok=True)
    if malformed is not None:
        backup=work/'malformed-prior-result.json'
        if not backup.exists():
            backup.write_bytes(malformed);backup.chmod(0o600)
    receipt = dict(prior, status='interrupted', quality_acceptance=False, recovered_at=time.time(),
        error='Previous worker stopped without terminal receipt. Preserved artifacts require inspection and a new submission; cloud cleanup must be verified.')
    atomic_json(work / 'recovery.json', receipt)
    publish(receipt)
    return True

def install_stop_handlers():
    def stop(signum, frame):
        raise KeyboardInterrupt('Worker stop requested')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
