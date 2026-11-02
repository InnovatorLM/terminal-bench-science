"""Bounded text-safe transfers for ACS evidence; no tar websocket retry loop."""
import asyncio
import base64
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile

MAX_BYTES = 100 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


async def remote_json(environment, script):
    result = await asyncio.wait_for(environment.exec(
        command='python3 -I -c ' + shlex.quote(script), timeout_sec=30), timeout=40)
    if result.return_code != 0:
        # Remote stderr may contain secrets or user data. Keep it out of the
        # controller exception; retain the original task logs separately.
        raise OSError('ACS evidence transfer command failed')
    return json.loads(result.stdout)


def safe_destination(target):
    target = Path(target)
    if target.is_symlink() or any(p.is_symlink() for p in target.parents):
        raise ValueError('Symlink in evidence destination')
    if target.exists() and not target.is_file():
        raise ValueError('Evidence destination must be a regular file')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


async def download_file(environment, source, target, *, size=None):
    target = safe_destination(target)
    source = str(source)
    if size is None:
        meta = await remote_json(environment, f'''import pathlib,json,stat
p=pathlib.Path({source!r})
s=p.lstat() if p.exists() or p.is_symlink() else None
print(json.dumps({{"size":s.st_size if s else None,"regular":bool(s and stat.S_ISREG(s.st_mode))}}))''')
        if meta.get('size') is None:
            raise FileNotFoundError('ACS source file is missing')
        if meta.get('regular') is not True:
            raise ValueError('ACS source must be a regular file')
        size = meta['size']
    if type(size) is not int or not 0 <= size <= MAX_BYTES:
        raise ValueError('ACS evidence exceeds transfer limit')
    fd, name = tempfile.mkstemp(prefix='.acs-download-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            for offset in range(0, size, CHUNK_BYTES):
                count = min(CHUNK_BYTES, size - offset)
                data = await remote_json(environment, f'''import os,stat,json,base64
fd=os.open({source!r},os.O_RDONLY|os.O_NOFOLLOW)
with os.fdopen(fd,'rb') as f:
 if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise ValueError('regular file required')
 f.seek({offset});data=f.read({count})
print(json.dumps(base64.b64encode(data).decode('ascii')))''')
                if not isinstance(data, str) or len(data) > 4 * ((count + 2) // 3):
                    raise ValueError('Invalid ACS evidence chunk')
                raw = base64.b64decode(data, validate=True)
                if len(raw) != count:
                    raise ValueError('ACS evidence changed during collection')
                output.write(raw)
        safe_destination(target)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


async def download_agent(environment, target):
    """Capture growing logs and sessions, including before Codex's finally runs."""
    files = await remote_json(environment, '''import pathlib,json,stat
roots=[('/logs/agent',''),('/tmp/codex-home/sessions','sessions/')]
files={};total=0
for directory,prefix in roots:
 root=pathlib.Path(directory)
 if root.is_symlink(): raise ValueError('linked evidence root')
 for p in sorted(root.rglob('*')):
  s=p.lstat()
  if stat.S_ISDIR(s.st_mode): continue
  if not stat.S_ISREG(s.st_mode): raise ValueError('nonregular evidence')
  name=prefix+p.relative_to(root).as_posix()
  if name in files: continue
  total+=s.st_size
  if total>100*1024*1024 or len(files)>=10000: raise ValueError('evidence limit')
  files[name]={'source':str(p),'size':s.st_size}
print(json.dumps(files))''')
    if not isinstance(files, dict) or len(files) > 10000:
        raise ValueError('Invalid ACS evidence manifest')
    total = 0
    for name, item in files.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts or str(rel) != name:
            raise ValueError('Unsafe ACS evidence path')
        expected = '/logs/agent/' + name
        alternate = '/tmp/codex-home/' + name
        if item.get('source') not in (expected, alternate if name.startswith('sessions/') else expected):
            raise ValueError('ACS evidence source mismatch')
        size = item.get('size')
        if type(size) is not int or size < 0:
            raise ValueError('Invalid ACS evidence size')
        total += size
    if total > MAX_BYTES:
        raise ValueError('ACS evidence exceeds transfer limit')
    for name, item in files.items():
        await download_file(environment, item['source'], Path(target) / name, size=item['size'])
    return {'files': len(files), 'bytes': total}
