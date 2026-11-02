"""Resumable, hash-verified ACS evidence snapshots with bounded transfer chunks.

There is no total-byte cutoff. Interrupted chunks stay private, and only a
complete file matching the remote snapshot digest replaces a prior checkpoint.
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat

CHUNK_BYTES = 1024 * 1024
MAX_FILES = 10000


async def remote_json(environment, script):
    result = await asyncio.wait_for(environment.exec(
        command='python3 -I -c ' + shlex.quote(script), timeout_sec=30), timeout=40)
    if result.return_code != 0:
        # Never return remote stderr, which can contain credentials/task data.
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


def file_hash(path):
    with safe_destination(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


async def describe(environment, source, size, prefix):
    # Hash once remotely, also checking the retained prefix. Resuming a rotated,
    # rewritten or corrupted file must never splice two different generations.
    meta = await remote_json(environment, f'''import os,pathlib,stat,json,hashlib
p=pathlib.Path({source!r});limit={size!r};prefix={prefix}
if p.is_symlink() or any(x.is_symlink() for x in p.parents): raise ValueError('linked evidence')
if not p.exists():
 print(json.dumps({{'missing':True}}))
else:
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as f:
  s=os.fstat(f.fileno())
  if not stat.S_ISREG(s.st_mode): raise ValueError('regular file required')
  if limit is None: limit=s.st_size
  if limit>s.st_size: raise ValueError('source shrank')
  full=hashlib.sha256();head=hashlib.sha256();offset=0
  while offset<limit:
   data=f.read(min(1024*1024,limit-offset))
   if not data: raise ValueError('source shrank')
   full.update(data)
   if offset<prefix: head.update(data[:prefix-offset])
   offset+=len(data)
  print(json.dumps({{'size':limit,'sha256':full.hexdigest(),'prefix_sha256':head.hexdigest(),
                    'device':s.st_dev,'inode':s.st_ino}}))''')
    if meta.get('missing'):
        raise FileNotFoundError('ACS source file is missing')
    if type(meta.get('size')) is not int or meta['size'] < 0:
        raise ValueError('Invalid ACS source size')
    if any(type(meta.get(name)) is not int or meta[name] < 0 for name in ('device', 'inode')):
        raise ValueError('Invalid ACS source identity')
    for name in ('sha256', 'prefix_sha256'):
        value = meta.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('Invalid ACS evidence checksum')
    return meta


async def download_file(environment, source, target, *, size=None, staging=None):
    target = safe_destination(target)
    source = str(source)
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError('Invalid ACS evidence size')
    staging = Path(staging) if staging is not None else target.parent / '.acs-transfer'
    key = hashlib.sha256((source + '\0' + str(target.absolute())).encode()).hexdigest()
    partial = safe_destination(staging / (key + '.part'))
    existing = partial if partial.exists() else target if target.exists() else None
    offset = existing.stat().st_size if existing else 0
    meta = await describe(environment, source, size, offset)
    size = meta['size']
    if offset > size or (existing and file_hash(existing) != meta['prefix_sha256']):
        existing, offset = None, 0
    if existing == target and offset == size:
        target.chmod(0o600)
        return {'bytes': size, 'sha256': meta['sha256'], 'downloaded_bytes': 0, 'resumed_bytes': size}
    # Keep old complete checkpoints intact during failed or cancelled transfers.
    # Free-space failures remain explicit and never turn partial logs into success.
    required = size - (offset if existing == partial else 0)
    if shutil.disk_usage(staging).free < required + CHUNK_BYTES:
        raise OSError('Insufficient space for complete ACS evidence snapshot')
    if existing == target:
        with target.open('rb') as inp, partial.open('wb') as out:
            os.fchmod(out.fileno(), 0o600)
            shutil.copyfileobj(inp, out, CHUNK_BYTES)
    elif existing is None:
        with partial.open('wb') as out:
            os.fchmod(out.fileno(), 0o600)
    downloaded = 0
    # Unbuffered writes retain every complete verified transport chunk even on
    # cancellation. Next invocation checks the entire prefix against the source.
    with partial.open('ab', buffering=0) as output:
        for position in range(offset, size, CHUNK_BYTES):
            count = min(CHUNK_BYTES, size - position)
            data = await remote_json(environment, f'''import os,stat,json,base64
fd=os.open({source!r},os.O_RDONLY|os.O_NOFOLLOW)
with os.fdopen(fd,'rb') as f:
 s=os.fstat(f.fileno())
 if not stat.S_ISREG(s.st_mode) or (s.st_dev,s.st_ino)!=({meta['device']},{meta['inode']}): raise ValueError('source replaced')
 f.seek({position});data=f.read({count})
print(json.dumps(base64.b64encode(data).decode('ascii')))''')
            if not isinstance(data, str) or len(data) > 4 * ((count + 2) // 3):
                raise ValueError('Invalid ACS evidence chunk')
            raw = base64.b64decode(data, validate=True)
            if len(raw) != count:
                raise ValueError('ACS evidence changed during collection')
            output.write(raw)
            downloaded += len(raw)
    if file_hash(partial) != meta['sha256']:
        raise ValueError('ACS evidence snapshot checksum mismatch')
    safe_destination(target)
    os.replace(partial, target)
    return {'bytes': size, 'sha256': meta['sha256'], 'downloaded_bytes': downloaded, 'resumed_bytes': offset}


async def agent_manifest(environment):
    files = await remote_json(environment, '''import pathlib,json,stat
roots=[('/tmp/codex-home/sessions','sessions/'),('/logs/agent','')]
files={}
for directory,prefix in roots:
 root=pathlib.Path(directory)
 if root.is_symlink(): raise ValueError('linked evidence root')
 for p in sorted(root.rglob('*')):
  s=p.lstat()
  if stat.S_ISDIR(s.st_mode): continue
  if not stat.S_ISREG(s.st_mode): raise ValueError('nonregular evidence')
  name=prefix+p.relative_to(root).as_posix()
  if name in files: continue
  if len(files)>=10000: raise ValueError('evidence file count limit')
  files[name]={'source':str(p),'size':s.st_size,'mtime_ns':s.st_mtime_ns,'inode':s.st_ino}
print(json.dumps(files))''')
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError('Invalid ACS evidence manifest')
    for name, item in files.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts or str(rel) != name:
            raise ValueError('Unsafe ACS evidence path')
        if name == 'acs-collection.json' or '.acs-transfer' in rel.parts:
            raise ValueError('Reserved ACS evidence path')
        expected = '/logs/agent/' + name
        alternate = '/tmp/codex-home/' + name
        if item.get('source') not in (expected, alternate if name.startswith('sessions/') else expected):
            raise ValueError('ACS evidence source mismatch')
        size = item.get('size')
        if type(size) is not int or size < 0:
            raise ValueError('Invalid ACS evidence size')
    return files


async def download_agent(environment, target):
    """Checkpoint growing sessions. Completeness requires a stable final scan."""
    target = Path(target)
    files = await agent_manifest(environment)
    retained = {}
    for name, item in files.items():
        retained[name] = await download_file(environment, item['source'], target / name,
            size=item['size'], staging=target.parent / '.acs-transfer')
    after = await agent_manifest(environment)
    # Concurrent writers are expected at checkpoints. A final collector retries
    # until stable; it must never label a stale checkpoint as full retention.
    return {'files': len(files), 'bytes': sum(v['size'] for v in files.values()),
            'complete': files == after, 'manifest': retained,
            'downloaded_bytes': sum(v['downloaded_bytes'] for v in retained.values()),
            'resumed_bytes': sum(v['resumed_bytes'] for v in retained.values())}
