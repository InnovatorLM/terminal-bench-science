"""Incremental recovery backup: task work, Git, trajectories and control state.

A completed cycle covers its explicit inventory, not a transactional live-world
snapshot. Credentials, installed root filesystems and rebuildable caches are
excluded. A bounded cycle resumes its pending inventory on the next invocation.
"""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import time

WORKSPACE = Path('/mnt/data/zhanglinfeng/workspace')
SENSITIVE = ('*.env', '*token*', '*secret*', '*credential*', 'app.ini',
             'kubeconfig*', '*.key', 'id_rsa*', 'id_ed25519*', 'jwt')
CACHES = {'.cache', '.tmp', '__pycache__', '.venv', 'node_modules', '.mypy_cache'}
ROLE_PAYLOADS = {'workspace', 'selftest', 'evaluation', 'skills', 'home', 'runs',
                 'instruction.md', 'capabilities.json', 'review-feedback.json',
                 'review-current-assignment.json', 'review-dispatch.json'}


class BudgetExpired(Exception):
    pass


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_name(path.name + '.pending')
    fd = os.open(pending, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    pending.chmod(0o600)
    pending.replace(path)


def excluded(rel):
    parts = rel.parts
    if rel.name in {'runtime-backup.json','runtime-backup.jsonl'}:return True
    if any(part in CACHES for part in parts):
        return True
    if any(any(fnmatch.fnmatch(part.lower(), pat) for pat in SENSITIVE) for part in parts):
        return True
    if rel.name.endswith(('.lock', '.sock', '.pid', '.pending', '.backup-pending', '-wal', '-shm')):
        return True
    if '.git' in parts and (rel.name.startswith(('tmp_', 'tmp_obj_')) or rel.name in {'FETCH_HEAD', 'config'}):
        return True
    # Role roots contain full package environments. Retain work and instruction
    # state without walking installed /usr, /lib, /tmp, /proc or old rootfs copies.
    prefix = ('.config', 'gewu-multiverse', 'agents')
    if parts[:3] == prefix and len(parts) >= 5:
        if parts[4] not in ROLE_PAYLOADS:
            return True
        if parts[4] == 'home' and len(parts) >= 7:
            if parts[6] not in {'.state', 'review-assignment.json', 'review-status.json',
                                'activity-receipt.json', 'reset-instruction.md'}:
                return True
        if parts[4] == 'workspace' and len(parts) >= 6 and parts[5] == 'nodesrc':
            return True
    if 'author-selftest' in parts:
        ix = parts.index('author-selftest')
        if len(parts) > ix+2 and parts[ix+2] in {'tmp', 'home'}:
            return True
        if len(parts) > ix+3 and parts[ix+2] == 'build':
            return parts[ix+3] not in {'receipt.json', 'task', 'build-manifest.json'}
    # Forgejo repository objects and a consistent SQLite snapshot are retained;
    # package blobs, indexers and downloaded archives are reconstructible.
    marker = ('.local', 'share', 'gewu-multiverse', 'forgejo', 'instance')
    if parts[:4] == marker[:4] and len(parts) > 4 and parts[4] != 'instance':
        return True
    if parts[:5] == marker and len(parts) > 5:
        if parts[5] != 'data':
            return True
        if len(parts) > 6 and parts[6] not in {'repositories', 'forgejo.db', 'attachments'}:
            return True
    return False


def inspect_source(path):
    value = path.lstat()
    if not (stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)):
        raise ValueError('Expected ordinary source file')
    result={'size': value.st_size, 'mtime_ns': value.st_mtime_ns,
            'mode': stat.S_IMODE(value.st_mode), 'inode': value.st_ino,
            'ctime_ns': value.st_ctime_ns}
    if stat.S_ISLNK(value.st_mode):result.update(kind='symlink',target=os.readlink(path))
    if path.name=='forgejo.db':
        try:
            wal=path.with_name(path.name+'-wal').stat()
            result['wal']=[wal.st_size,wal.st_mtime_ns,wal.st_ctime_ns]
        except FileNotFoundError:result['wal']=None
    return result


def inventory(source, includes, deadline):
    """Prune heavyweight trees before walking; never follow external symlinks."""
    selected = []
    warnings = []
    roots = [source / name for name in includes] if includes else [source]
    for root in roots:
        if not root.exists():
            warnings.append({'path': str(root.relative_to(source)), 'kind': 'missing_scope'})
            continue
        paths = [(str(root.parent), [], [root.name])] if root.is_file() else os.walk(root, followlinks=False)
        for folder, dirs, names in paths:
            if time.monotonic() > deadline:
                raise BudgetExpired('Inventory not finished')
            parent = Path(folder)
            names=list(names)+[name for name in dirs if (parent/name).is_symlink()]
            dirs[:] = sorted(name for name in dirs if not excluded((parent/name).relative_to(source))
                             and not (parent/name).is_symlink())
            for name in sorted(names):
                path = parent/name
                rel = path.relative_to(source)
                if excluded(rel):
                    continue
                if path.is_symlink():
                    link=Path(os.readlink(path))
                    resolved=path.resolve()
                    if link.is_absolute() or not resolved.is_relative_to(source) or excluded(resolved.relative_to(source)):
                        continue
                try:
                    info = inspect_source(path)
                except FileNotFoundError:
                    warnings.append({'path': str(rel), 'kind': 'source_disappeared'})
                    continue
                except ValueError:
                    continue
                selected.append({'path': rel.as_posix(), 'source': info})
    # Most recent trajectories/control state are useful even while a large task
    # repository is still copying. Stable sorting makes the inventory auditable.
    selected.sort(key=lambda item: (0 if item['path'].startswith('.state/') else 1,
                                    -item['source']['mtime_ns'], item['path']))
    return selected, warnings


def digest(path, deadline, limit=None):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        remaining=limit
        while remaining is None or remaining>0:
            chunk=stream.read(1024*1024 if remaining is None else min(1024*1024,remaining))
            if not chunk:
                if remaining:raise ValueError('File shortened during verification')
                break
            if time.monotonic() > deadline:
                raise BudgetExpired('File verification not finished')
            value.update(chunk)
            if remaining is not None:remaining-=len(chunk)
    return value.hexdigest()


def copy_file(source, target, expected, deadline, checked_parents=None):
    if inspect_source(source) != expected:
        raise ValueError('Source changed since inventory')
    is_link=expected.get('kind')=='symlink'
    if target.is_symlink() and not is_link:raise ValueError('Linked backup destination refused')
    # This private tree has one writer. Rechecking its entire ancestor chain
    # for every small file creates severe metadata overhead on network storage.
    if checked_parents is None or target.parent not in checked_parents:
        if any(p.is_symlink() for p in target.parents):raise ValueError('Linked backup destination refused')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if checked_parents is not None:checked_parents.add(target.parent)
    pending = target.with_name(target.name + '.backup-pending')
    try:
        if pending.is_symlink():raise ValueError('Linked pending destination refused')
        if is_link:
            pending.symlink_to(expected['target'])
            checksum=hashlib.sha256(expected['target'].encode()).hexdigest()
        elif source.name == 'forgejo.db':
            # SQLite backups include committed WAL contents and omit transient
            # locks. The resulting file is internally consistent while live.
            with closing(sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(pending)) as dst:
                    def progress(*_):
                        if time.monotonic() > deadline:raise BudgetExpired()
                    src.backup(dst, pages=128, progress=progress, sleep=0.05)
                    dst.execute('pragma journal_mode=delete')
            pending.chmod(0o600)
            checksum = digest(pending, deadline)
        else:
            fd = os.open(pending, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            checksum = hashlib.sha256()
            append_log=source.suffix in {'.jsonl','.log'}
            with os.fdopen(os.open(source,os.O_RDONLY|os.O_NOFOLLOW),'rb') as src, os.fdopen(fd, 'wb') as dst:
                remaining=expected['size']
                while remaining>0:
                    chunk=src.read(min(1024*1024,remaining))
                    if not chunk:raise ValueError('Source shortened during copy')
                    if time.monotonic() > deadline:raise BudgetExpired()
                    dst.write(chunk);checksum.update(chunk);remaining-=len(chunk)
                dst.flush();os.fsync(dst.fileno())
            after=inspect_source(source)
            if after!=expected:
                # A live transcript can keep growing. Preserve a verified
                # prefix rather than requiring the author to stop generating.
                if (not append_log or after['inode']!=expected['inode'] or after['size']<expected['size']
                        or digest(source,deadline,expected['size'])!=checksum.hexdigest()):
                    raise ValueError('Source changed during copy')
            checksum = checksum.hexdigest()
            if digest(pending, deadline) != checksum:
                raise ValueError('Backup checksum differs')
        if not is_link:pending.chmod(0o600)
        pending.replace(target)
        result = target.lstat()
        return dict(source=expected, sha256=checksum, size=result.st_size,
                    backup_mtime_ns=result.st_mtime_ns, backup_ctime_ns=result.st_ctime_ns)
    finally:
        pending.unlink(missing_ok=True)


def unchanged(record, item, destination):
    if not record or record.get('source') != item['source']:return False
    if item['source'].get('kind')=='symlink':
        if not destination.is_symlink() or os.readlink(destination)!=item['source']['target']:return False
    elif destination.is_symlink():return False
    try:info=destination.lstat()
    except FileNotFoundError:return False
    return (record.get('size')==info.st_size and record.get('backup_mtime_ns')==info.st_mtime_ns
            and record.get('backup_ctime_ns')==info.st_ctime_ns)


def cycle(source, target, budget=300, includes=None, receipt=None, workers=4):
    source = Path(source).resolve(strict=True);target=Path(target)
    if workers < 1 or workers > 16:raise ValueError('Backup workers must be between 1 and 16')
    if target.is_symlink():raise ValueError('Linked backup root refused')
    target=target.resolve()
    if not target.is_relative_to(WORKSPACE) or target==source or target.is_relative_to(source):
        raise ValueError('Backup target must be outside source and inside workspace')
    includes = list(includes or [])
    for name in includes:
        p=Path(name)
        if p.is_absolute() or '..' in p.parts or not p.parts:raise ValueError('Unsafe scope')
        if not (source/p).resolve().is_relative_to(source):raise ValueError('Linked scope refused')
    target.mkdir(parents=True, exist_ok=True, mode=0o700);target.chmod(0o700)
    with (target/'backup.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'already_running'}
        started=time.time();deadline=time.monotonic()+budget
        manifest_path=target/'manifest.json';progress_path=target/'progress.json'
        old=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        identity={'source':str(source),'includes':includes}
        if old and old.get('identity')!=identity:raise ValueError('Target belongs to another backup scope')
        manifest=old or {'identity':identity,'files':{},'last_complete_inventory_at':None}
        progress=json.loads(progress_path.read_text()) if progress_path.exists() else {}
        row=dict(identity,started_at=started,status='running',copied=0,unchanged=0,warnings=[],
                 coverage='selected_scope' if includes else 'recovery_payload',
                 consistency='per-file-verified-live-inventory',last_success_at=manifest.get('last_success_at'))
        latest=Path(receipt) if receipt else target/'latest.json'
        if not latest.resolve().is_relative_to(WORKSPACE):raise ValueError('Receipt must be workspace-local')
        atomic(latest,row)
        try:
            if progress.get('identity')!=identity or not progress.get('pending'):
                items,warnings=inventory(source,includes,deadline)
                progress=dict(identity=identity,inventory_at=started,pending=items,warnings=warnings,inventory_count=len(items),inventory_paths=[item['path'] for item in items])
                atomic(progress_path,progress)
            remaining=progress['pending'];row['warnings']=list(progress.get('warnings',[]))
            completed=0;checkpoint=time.monotonic();reported=checkpoint;checked_parents=set()
            def process(item):
                if time.monotonic()>deadline:raise BudgetExpired()
                rel=item['path'];destination=target/'data'/rel
                try:
                    current=inspect_source(source/rel)
                    if current!=item['source']:
                        # Re-inventory a mutable file rather than retrying its
                        # stale size/mtime forever. Record the changed snapshot.
                        item['source']=current
                    if unchanged(manifest['files'].get(rel),item,destination):return rel,None,None
                    return rel,copy_file(source/rel,destination,item['source'],deadline,checked_parents),None
                except (OSError,ValueError) as error:
                    return rel,None,{'path':rel,'kind':type(error).__name__}
            # Keep only one future per worker in flight. In-order collection
            # gives an exact resumable prefix even when copies finish out of order.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                work=iter(remaining);pending=deque()
                for _ in range(workers):
                    item=next(work,None)
                    if item is not None:pending.append(pool.submit(process,item))
                try:
                    while pending:
                        rel,record,warning=pending.popleft().result()
                        if warning:row['warnings'].append(warning)
                        elif record is None:row['unchanged']+=1
                        else:manifest['files'][rel]=record;row['copied']+=1
                        completed+=1
                        now=time.monotonic()
                        if completed%5000==0 or now-checkpoint>=60:
                            progress['pending']=remaining[completed:];progress['warnings']=row['warnings']
                            atomic(manifest_path,manifest);atomic(progress_path,progress);checkpoint=now
                        if now-reported>=15:
                            row.update(remaining_files=len(remaining)-completed,
                                       inventory_count=progress['inventory_count'],
                                       verified_files=len(manifest['files']),elapsed_seconds=round(time.time()-started,2))
                            atomic(latest,row);reported=now
                        item=next(work,None)
                        if item is not None:pending.append(pool.submit(process,item))
                finally:
                    for future in pending:future.cancel()
            progress['pending']=[]
            row['status']='partial' if row['warnings'] else 'ok'
            if row['status']=='ok':
                manifest['last_success_at']=time.time()
                manifest['last_complete_inventory_at']=progress['inventory_at']
                manifest['complete_inventory_paths']=progress['inventory_paths']
            else:
                # Persistent missing/unreadable source files remain explicit.
                # Next cycle takes a new inventory; last verified backup remains.
                manifest['last_partial_at']=time.time()
        except (BudgetExpired,KeyboardInterrupt):
            if 'remaining' in locals():progress['pending']=remaining[completed:]
            row['status']='partial';row['reason']='cycle_budget_or_interrupt'
        except Exception as error:
            row.update(status='failed',error_type=type(error).__name__)
        progress['warnings']=row['warnings']
        atomic(manifest_path,manifest);atomic(progress_path,progress)
        row.update(finished_at=time.time(),elapsed_seconds=round(time.time()-started,2),
                   remaining_files=len(progress.get('pending',[])),inventory_at=progress.get('inventory_at'),
                   inventory_count=progress.get('inventory_count',0),last_success_at=manifest.get('last_success_at'),
                   verified_files=len(manifest['files']),
                   last_complete_inventory_at=manifest.get('last_complete_inventory_at'))
        atomic(latest,row)
        logs=[target/'cycles.jsonl']
        # Historical /tmp sources are read-only recovery inputs. New runtime
        # sources inside workspace also expose freshness to the existing monitor.
        if source.is_relative_to(WORKSPACE):
            runtime_log=source/'.state/gewu-multiverse/runtime-backup.jsonl'
            runtime_log.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            logs.append(runtime_log)
        for path in logs:
            with path.open('a') as log:
                os.chmod(log.name,0o600);log.write(json.dumps(row,separators=(',',':'))+'\n')
        return row


def restore(backup, destination, paths=None):
    """Restore selected recorded files with SHA-256 verification and original modes."""
    backup=Path(backup).resolve(strict=True);destination=Path(destination)
    if destination.exists() or not destination.resolve().is_relative_to(WORKSPACE):
        raise ValueError('Require fresh workspace-local restore destination')
    manifest=json.loads((backup/'manifest.json').read_text())
    selected=list(paths) if paths is not None else manifest.get('complete_inventory_paths')
    if selected is None:raise ValueError('No complete inventory; explicitly select verified files')
    destination.mkdir(mode=0o700,parents=True)
    restored=[]
    for name in selected:
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe recorded path')
        record=manifest['files'][name];source=backup/'data'/relative
        if record['source'].get('kind')=='symlink':
            link=record['source']['target'];target=destination/relative
            if (not source.is_symlink() or os.readlink(source)!=link
                    or hashlib.sha256(link.encode()).hexdigest()!=record['sha256']
                    or Path(link).is_absolute() or not (target.parent/link).resolve().is_relative_to(destination)):
                raise ValueError('Backup link failed verification')
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True);target.symlink_to(link)
            restored.append(name);continue
        if source.is_symlink() or digest(source,float('inf'))!=record['sha256']:
            raise ValueError('Backup contents failed verification')
        target=destination/relative;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        import shutil
        shutil.copyfile(source,target)
        if digest(target,float('inf'))!=record['sha256']:raise ValueError('Restore checksum differs')
        target.chmod(record['source']['mode'] & 0o777)
        restored.append(name)
    atomic(destination/'restore-receipt.json',dict(verified_files=len(restored),paths=restored,
                                                 source_inventory_at=manifest.get('last_complete_inventory_at')))
    return restored


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--target',type=Path,required=True)
    p.add_argument('--interval',type=int,default=600);p.add_argument('--budget',type=float,default=300)
    p.add_argument('--once',action='store_true');p.add_argument('--receipt',type=Path)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--include',action='append',default=[])
    args=p.parse_args()
    if args.budget<=0 or args.interval<=0:raise SystemExit('Budget and interval must be positive')
    def stop(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    while True:
        result=cycle(args.source,args.target,args.budget,args.include,args.receipt,args.workers)
        print(json.dumps({k:result.get(k) for k in ('status','copied','unchanged','remaining_files','last_success_at')}),flush=True)
        if args.once:return 0 if result['status'] in ('ok','already_running') else 2
        time.sleep(args.interval)


if __name__=='__main__':raise SystemExit(main())
