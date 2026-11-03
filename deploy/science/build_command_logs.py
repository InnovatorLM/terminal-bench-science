"""Persist complete private subprocess output and invocation metadata."""
import os
from pathlib import Path
import shutil
import subprocess
import time

from evaluation_queue import atomic_json


def run_logged(command, prefix, *, timeout, env=None, cwd=None, details=None):
    prefix = Path(prefix)
    stdout = prefix.with_name(prefix.name + '.stdout.log')
    stderr = prefix.with_name(prefix.name + '.stderr.log')
    combined = prefix.with_name(prefix.name + '.log')
    metadata = prefix.with_name(prefix.name + '.json')
    command = [str(item) for item in command]
    record = dict(details or {}, command=command, cwd=str(Path(cwd or Path.cwd()).resolve()),
                  started_at=time.time(), timeout_seconds=timeout, returncode=None,
                  status='running', stdout=stdout.name, stderr=stderr.name,
                  combined=combined.name, combined_order='stdout_then_stderr')
    atomic_json(metadata, record)
    try:
        with os.fdopen(os.open(stdout, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as out, \
             os.fdopen(os.open(stderr, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as err:
            result = subprocess.run(command, stdout=out, stderr=err, env=env, cwd=cwd, timeout=timeout)
        record.update(status='exited', returncode=result.returncode)
        return result
    except BaseException as error:
        record.update(status='timed_out' if isinstance(error, subprocess.TimeoutExpired) else 'exception',
                      exception_type=type(error).__name__)
        raise
    finally:
        record['finished_at'] = time.time()
        atomic_json(metadata, record)
        with os.fdopen(os.open(combined, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as log:
            for name, path in [('stdout', stdout), ('stderr', stderr)]:
                log.write(('[' + name + ']\n').encode())
                if path.is_file():
                    with path.open('rb') as stream:
                        shutil.copyfileobj(stream, log)
                log.write(b'\n')
