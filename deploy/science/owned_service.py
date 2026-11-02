"""Run one replacement service only while its original launcher is alive."""
import argparse
import os
import select
import signal
import subprocess


def run(owner_pid, command, grace=10):
    # A pidfd follows the original process even if its numeric PID is reused.
    owner = os.pidfd_open(owner_pid)
    child = None
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if select.select([owner], [], [], 0)[0]:
            return 0
        child = subprocess.Popen(command, start_new_session=True)
        while child.poll() is None:
            if stopping or select.select([owner], [], [], 1)[0]:
                break
        return child.returncode if child.returncode is not None else 0
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        os.close(owner)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-pid', type=int, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error('A service command is required')
    raise SystemExit(run(args.owner_pid, args.command))
