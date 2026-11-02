"""Collect complete verifier snapshots using resumable, hash-verified chunks."""
import fnmatch
from pathlib import Path

from acs_evidence_transport import MAX_FILES, download_file, remote_json


async def manifest(environment):
    files = await remote_json(environment, '''# verifier inventory
import pathlib,json,stat
root=pathlib.Path('/logs/verifier');files={}
if root.is_symlink() or any(p.is_symlink() for p in root.parents): raise ValueError('linked evidence root')
for p in sorted(root.rglob('*')):
 s=p.lstat()
 if stat.S_ISDIR(s.st_mode): continue
 if not stat.S_ISREG(s.st_mode): raise ValueError('nonregular evidence')
 if len(files)>=10000: raise ValueError('evidence file count limit')
 files[p.relative_to(root).as_posix()]={'source':str(p),'size':s.st_size,'mtime_ns':s.st_mtime_ns,'inode':s.st_ino}
print(json.dumps(files))''')
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError('Invalid verifier evidence manifest')
    for name, item in files.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise ValueError('Invalid verifier evidence entry')
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts or str(rel) != name:
            raise ValueError('Unsafe verifier evidence path')
        if name == 'acs-collection.json' or '.acs-transfer' in rel.parts:
            raise ValueError('Reserved verifier evidence path')
        if item.get('source') != '/logs/verifier/' + name:
            raise ValueError('Verifier evidence source mismatch')
        if any(type(item.get(field)) is not int or item[field] < 0 for field in ('size', 'mtime_ns', 'inode')):
            raise ValueError('Invalid verifier evidence metadata')
    if any(str(parent) in files for name in files for parent in Path(name).parents):
        raise ValueError('Conflicting verifier evidence paths')
    return files


async def download(environment, target, *, include=None, exclude=None, protect=None):
    target = Path(target)
    files = await manifest(environment)
    # Preflight the entire namespace before any file is installed. Invalid
    # paths or a symlink late in the inventory cannot leave an accepted bundle.
    for name in [*files, 'acs-collection.json']:
        dest = target / name
        if dest.is_symlink() or any(p.is_symlink() for p in dest.parents):
            raise ValueError('Symlink in evidence destination')
        if ((dest.exists() and not dest.is_file())
                or any(p.exists() and not p.is_dir() for p in dest.parents)):
            raise ValueError('Conflicting verifier evidence path')
    retained = {}
    excluded = []
    for name, item in files.items():
        selected = (not include or any(fnmatch.fnmatch(name, pattern) for pattern in include))
        selected = selected and not any(fnmatch.fnmatch(name, pattern) for pattern in (exclude or []))
        if not selected and name not in (protect or []) and name not in ('reward.txt', 'reward.json'):
            excluded.append(name)
            continue
        retained[name] = await download_file(environment, item['source'], target / name,
            size=item['size'], staging=target.parent / '.acs-transfer')
    stable = files == await manifest(environment)
    return dict(files=len(retained), bytes=sum(row['bytes'] for row in retained.values()),
                stable=stable, complete=stable and not excluded, excluded_files=excluded,
                manifest=retained, downloaded_bytes=sum(row['downloaded_bytes'] for row in retained.values()),
                resumed_bytes=sum(row['resumed_bytes'] for row in retained.values()))
