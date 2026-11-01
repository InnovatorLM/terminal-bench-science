"""Bind uploaded regular files to an actual committed Git tree."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import os

def verify_source(workspace, repository, task_path, commit, uploaded):
    workspace = Path(workspace).resolve(strict=True)
    relative = PurePosixPath(repository)
    task_relative = PurePosixPath(task_path)
    if relative.is_absolute() or '..' in relative.parts or task_relative.is_absolute() or '..' in task_relative.parts or not task_relative.parts:
        raise ValueError('Source paths must be relative workspace paths')
    repo = workspace.joinpath(*relative.parts).resolve(strict=True)
    if not repo.is_relative_to(workspace) or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Invalid committed source identity')
    command = ['git', '-c', 'safe.directory=' + str(repo), '-C', str(repo)]
    env = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1'}
    # Older patched Git ignores safe.directory supplied via -c. Supply exactly
    # this repository in a private operator-owned protected config, never '*'.
    with tempfile.NamedTemporaryFile(mode='w',prefix='selftest-git-',dir=Path(os.environ.get('TMPDIR','/mnt/data/zhanglinfeng/workspace/.tmp'))) as protected:
        protected.write('[safe]\n\tdirectory = '+__import__('json').dumps(str(repo))+'\n')
        protected.flush()
        env['GIT_CONFIG_GLOBAL']=protected.name
        raw = subprocess.check_output(command + ['ls-tree', '-r', '-z', commit + ':' + task_relative.as_posix()], env=env, timeout=30)
    expected = {}
    for row in raw.split(bytes([0])):
        if not row:
            continue
        meta, name = row.split(b'	', 1)
        mode, kind, oid = meta.decode().split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('Committed task links and submodules are refused')
        expected[name.decode()] = (mode, oid)
    actual = {}
    for path in Path(uploaded).rglob('*'):
        if path.is_symlink():
            raise ValueError('Upload symlink refused')
        if path.is_file():
            data = path.read_bytes()
            oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + bytes([0]) + data).hexdigest()
            mode = '100755' if path.stat().st_mode & 0o111 else '100644'
            actual[path.relative_to(uploaded).as_posix()] = (mode, oid)
    if not expected or actual != expected:
        raise ValueError('Uploaded task differs from claimed committed source; commit all intended changes before submitting')
    return {'source_commit': commit, 'source_task_path': task_relative.as_posix(), 'source_repository': relative.as_posix(), 'source_git_tree_verified': True, 'file_count': len(actual)}
