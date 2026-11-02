"""Bounded process workers; each owns its signal handlers and cloud cleanup."""
import subprocess
import os
import select
import signal


class RetainedWorker:
    """Track an orphaned worker without owning/restarting its cloud execution.

    A pidfd binds operations to this process even if its numeric PID is reused.
    The caller must validate the worker's claim after opening this handle.
    """
    def __init__(self, pid):
        self.pid = pid
        self.fd = os.pidfd_open(pid)

    def poll(self):
        return 0 if self.fd is None or select.select([self.fd], [], [], 0)[0] else None

    def wait(self):
        if self.fd is not None:
            select.select([self.fd], [], [])
            self.close()
        return 0

    def terminate(self):
        if self.poll() is None:
            try:
                signal.pidfd_send_signal(self.fd, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ActiveJobs:
    def __init__(self, limit):
        self.limit = limit
        self.children = {}

    def reap(self):
        for key, child in list(self.children.items()):
            if child.poll() is not None:
                child.wait()
                del self.children[key]

    def start(self, key, command):
        self.reap()
        if key in self.children or len(self.children) >= self.limit:
            return False
        self.children[key] = subprocess.Popen(command)
        return True

    def stop(self):
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()
        # Workers forward SIGTERM to their guard and await cloud cleanup.
        for child in self.children.values():
            child.wait()
        self.children.clear()
