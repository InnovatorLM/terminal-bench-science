"""Install current credential-free author clients before role capability checks."""
import os
from pathlib import Path
import re
import tempfile
from evaluation_queue import W


def install_clock(root):
    # Install the shared policy before either executable can import it. Both
    # command names remain supported by existing author workspaces.
    directory = Path(root)/'usr/local/bin'
    for source, name, mode in [
        ('iteration_budget.py', 'iteration_budget.py', 0o644),
        ('author_iteration_clock.py', 'author-iteration-clock', 0o755),
        ('author_iteration_clock.py', 'role-iteration', 0o755),
    ]:
        _install_file(directory, source, name, mode)


def _install_file(directory, source, name, mode=0o755):
    fd, pending = tempfile.mkstemp(prefix='.selftest-client-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(Path(__file__).with_name(source).read_bytes())
        os.chmod(pending, mode)
        os.replace(pending, directory/name)
    finally:
        Path(pending).unlink(missing_ok=True)


def install(config):
    if os.geteuid()!=0:raise PermissionError('Operator required')
    for role,entry in config['roles'].items():
        if not re.fullmatch(r'author-[0-9]+',role):continue
        root=Path(entry['root']).resolve(strict=True);directory=root/'usr/local/bin'
        local_runtime=str(root).startswith('/tmp/gewu-terminal-bench-science-runtime-')
        if (not root.is_relative_to(W) and not local_runtime) or directory.resolve()!=directory:raise ValueError('Unsafe client destination')
        _install_file(directory, 'role_acs_selftest.py', 'role-acs-selftest')
        install_clock(root)
