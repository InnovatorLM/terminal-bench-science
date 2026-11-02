"""Incremental recovery backup: task work, Git, trajectories and control state.

A completed cycle covers its explicit inventory, not a transactional live-world
snapshot. Credentials, installed root filesystems and rebuildable caches are
excluded. A bounded cycle resumes its pending inventory on the next invocation.
"""
import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import time

WORKSPACE = Path('/mnt/data/zhanglinfeng/workspace')
SENSITIVE = ('*.env', '*token*', '*secret*', '*credential*', 'app.ini', 'auth.json',
             'kubeconfig*', '*.key', 'id_rsa*', 'id_ed25519*', 'jwt')
CACHES = {'.cache', '.tmp', '__pycache__', '.venv', 'node_modules', '.mypy_cache', '.pytest_cache'}
ROLE_PAYLOADS = {'workspace', 'selftest', 'evaluation', 'skills', 'home', 'runs',
                 'instruction.md', 'capabilities.json', 'review-feedback.json',
                 'review-current-assignment.json', 'review-dispatch.json', 'post-acceptance.json'}


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
    if rel.name.endswith(('.sock', '.pid', '.pending', '.backup-pending', '-wal', '-shm')):
        return True
    runtime_lock=rel.name in {'state.lock','worker.lock','backup.lock','launcher.lock','watchdog.lock','reservation.lock','dispatcher.lock','lifecycle.lock','curator.lock'} or fnmatch.fnmatch(rel.name,'author-*.lock') or fnmatch.fnmatch(rel.name,'evaluator-*.lock')
    if rel.name.endswith('.lock') and ('.git' in parts or runtime_lock):
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
                                'activity-receipt.json', 'reset-instruction.md',
                                'trajectories', '.codex'}:
                return True
            if parts[6] == '.codex' and len(parts) >= 8 and parts[7] not in {
                    'sessions', 'archived_sessions', 'history.jsonl', 'session_index.jsonl'}:
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
            'ctime_ns': value.st_ctime_ns, 'uid':value.st_uid, 'gid':value.st_gid}
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


def advance_inventory(source, includes, progress, deadline):
    """Persist a bounded file walk; copying can proceed before the walk ends.

    A complete os.walk on network storage may itself exceed a cycle's budget.
    Keep its remaining paths rather than restarting it and starving all copies.
    Directory entries are enumerated once; no symlink directory is followed.
    """
    work = deque(progress.get('inventory_walk', includes or ['.']))
    selected = {item['path']: item for item in progress.get('pending', [])}
    paths = set(progress.get('inventory_paths', []))
    warnings = progress.setdefault('warnings', [])
    while work and time.monotonic() < deadline:
        name = work.popleft(); rel = Path(name); path = source/rel
        if excluded(rel):continue
        try:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    children = sorted(str(Path(entry.path).relative_to(source)) for entry in entries)
                work.extendleft(reversed(children))
                continue
            if stat.S_ISLNK(info.st_mode):
                resolved = path.resolve()
                if (Path(os.readlink(path)).is_absolute() or not resolved.is_relative_to(source)
                        or excluded(resolved.relative_to(source))):continue
            metadata = inspect_source(path)
        except FileNotFoundError:
            warnings.append({'path':str(rel), 'kind':'source_disappeared'})
            continue
        except ValueError:
            continue
        except OSError as error:
            warnings.append({'path':str(rel), 'kind':type(error).__name__})
            continue
        key = rel.as_posix()
        selected[key] = {'path':key, 'source':metadata}; paths.add(key)
    progress.update(inventory_walk=list(work), inventory_scanning=bool(work),
                    inventory_paths=sorted(paths), inventory_count=len(paths),
                    pending=sorted(selected.values(), key=lambda item:(
                        0 if item['path'].startswith('.state/') else 1,
                        -item['source']['mtime_ns'], item['path'])))


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


def copy_file(source, target, expected, deadline, checked_parents=None, recovery_roots=None):
    if inspect_source(source) != expected:
        raise ValueError('Source changed since inventory')
    is_link=expected.get('kind')=='symlink'
    if target.is_symlink() and not is_link:raise ValueError('Linked backup destination refused')
    # This private tree has one writer. Rechecking its entire ancestor chain
    # for every small file creates severe metadata overhead on network storage.
    if checked_parents is None or target.parent not in checked_parents:
        if any(p.is_symlink() for p in target.parents):raise ValueError('Linked backup destination refused')
        if recovery_roots:
            source_root,data_root=recovery_roots
            chain=[target.parent,*target.parent.parents]
            for folder in reversed([p for p in chain if p==data_root or p.is_relative_to(data_root)]):
                if checked_parents is not None and folder in checked_parents:continue
                folder.mkdir(mode=0o700,parents=True,exist_ok=True)
                metadata=(source_root/folder.relative_to(data_root)).stat()
                os.chown(folder,metadata.st_uid,metadata.st_gid)
                folder.chmod(stat.S_IMODE(metadata.st_mode))
                if checked_parents is not None:checked_parents.add(folder)
        else:
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
        if recovery_roots:
            os.chown(pending,expected['uid'],expected['gid'],follow_symlinks=False)
            if not is_link:pending.chmod(expected['mode'])
        elif not is_link:pending.chmod(0o600)
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


def priority_inventory(source, includes, progress, deadline):
    """Refresh live sessions, then resume a bounded walk of role workspaces.

    Git objects and old clones remain in the bulk inventory. A pending bulk
    inventory must never prevent discovery of new sessions or appended logs.
    Workspace traversal has its own cursor so a slow scan cannot starve either
    trajectories or the bulk copy. Its cutoff stays fixed until that walk ends.
    """
    selected = {}; warnings = []
    scopes = [Path(name) for name in includes]
    cutoff = progress.get('priority_cutoff_ns', int(progress['inventory_at']*1e9)-120_000_000_000)

    def allowed(rel):
        return (not excluded(rel) and (not scopes or any(
            rel == scope or rel.is_relative_to(scope) or scope.is_relative_to(rel) for scope in scopes)))

    def collect(path, force=False):
        rel = path.relative_to(source)
        if not allowed(rel):return
        try:
            info = inspect_source(path)
            if info.get('kind') == 'symlink':
                resolved = path.resolve()
                if (Path(info['target']).is_absolute() or not resolved.is_relative_to(source)
                        or excluded(resolved.relative_to(source))):return
            if force or info['mtime_ns'] >= cutoff:
                selected[str(rel)] = {'path':str(rel), 'source':info}
        except FileNotFoundError:
            pass  # A fresh scan does not promise files already removed.
        except ValueError:
            pass
        except OSError as error:
            warnings.append({'path':str(rel), 'kind':type(error).__name__})

    def entries(path):
        try:return list(os.scandir(path))
        except FileNotFoundError:return []

    state = source/'.state/gewu-multiverse'
    fast = []
    live_runs = []
    for entry in entries(state):
        path = Path(entry.path)
        if not allowed(path.relative_to(source)):continue
        if entry.is_file(follow_symlinks=False):collect(path)
        elif entry.is_dir(follow_symlinks=False):
            children = entries(path)
            for child in children:
                if child.is_file(follow_symlinks=False):collect(Path(child.path), force=True)
            if entry.name.startswith(('author-', 'evaluator-')) and entry.name != 'author-selftest' or entry.name == 'curator':
                runs = sorted((c.name for c in children if c.name.startswith('run-')), reverse=True)
                fast.extend(str((path/name).relative_to(source)) for name in runs[:2])
                if runs:live_runs.append((entry.name, path/runs[0]))
            elif entry.name in {'review-dispatcher', 'command-watchdog', 'role-reset', 'monitor-briefs'}:
                fast.append(str(path.relative_to(source)))
            elif entry.name in {'author-selftest', 'evaluation-queue'}:
                # Job receipts and logs are small; task/build trees use bulk.
                for child in children:
                    if child.is_dir(follow_symlinks=False):
                        for leaf in entries(Path(child.path)):
                            if leaf.is_file(follow_symlinks=False):collect(Path(leaf.path))
    # Commands in the latest transcript identify the task currently being
    # edited, even before a self-test receipt exists. Read only a bounded tail;
    # these strings select ordinary files, and are never executed. The complete
    # workspace walk remains the fallback for work without an absolute path.
    for role, run in live_runs:
        try:
            with (run/'transcript.jsonl').open('rb') as stream:
                stream.seek(0, os.SEEK_END);stream.seek(max(0, stream.tell()-131072))
                tail=stream.read().decode('utf-8', errors='replace')
        except OSError:continue
        root=source/'.config/gewu-multiverse/agents'/role
        task_scopes=set()
        for name in reversed(re.findall(r'/workspace/[A-Za-z0-9_./-]+', tail)):
            path=root/name.lstrip('/');rel=path.relative_to(source)
            if '..' in rel.parts or '.git' in rel.parts or 'tasks' not in rel.parts or not allowed(rel):continue
            try:
                if not path.resolve().is_relative_to(root/'workspace') or not path.exists():continue
                folder=path if path.is_dir() else path.parent
                candidates=[folder,*folder.parents]
                task=next((p for p in candidates if p.is_relative_to(root/'workspace') and (p/'task.toml').is_file()),folder)
                if task.is_symlink():continue
                task_scopes.add(str(task.relative_to(source)))
                if len(task_scopes)>=2:break
            except OSError:continue
        fast.extend(sorted(task_scopes))
    while fast and time.monotonic() < deadline:
        name = fast.pop(0); folder = source/name
        for entry in entries(folder):
            path = Path(entry.path); rel = path.relative_to(source)
            if not allowed(rel):continue
            if entry.is_dir(follow_symlinks=False):fast.append(str(rel))
            else:collect(path, force=True)

    work = progress.get('priority_walk')
    if work is None:
        work = []
        for entry in entries(source/'.config/gewu-multiverse/agents'):
            if not entry.is_dir(follow_symlinks=False):continue
            role = Path(entry.path)
            for child in entries(role):
                path=Path(child.path)
                if child.is_file(follow_symlinks=False):collect(path, force=True)
            for suffix in ['workspace', 'home/role/trajectories', 'home/role/.codex', 'home/role/.state']:
                path=role/suffix
                if path.is_dir() and not path.is_symlink() and allowed(path.relative_to(source)):
                    work.append(str(path.relative_to(source)))
        progress['priority_cutoff_ns'] = cutoff
        progress['priority_walk_started_at'] = time.time()
    def task_work(name):return '/workspace/' in name and ('/tasks/' in name or name.endswith('/tasks'))
    work = deque(sorted(work, key=lambda name:0 if task_work(name) else 1))
    while work and time.monotonic() < deadline:
        name = work.popleft(); folder = source/name
        # User-created virtual environments are rebuildable; their ordinary
        # bulk treatment is unchanged. Do not spend the priority scan on them.
        children = entries(folder)
        if any(entry.name in {'pyvenv.cfg', 'conda-meta'} for entry in children):continue
        for entry in children:
            path = Path(entry.path); rel = path.relative_to(source)
            if not allowed(rel) or '.git' in rel.parts:continue
            if entry.is_dir(follow_symlinks=False):
                # Actual task packages precede CI fixtures and old repo clones.
                if task_work(str(rel)):work.appendleft(str(rel))
                else:work.append(str(rel))
            else:collect(path)
    progress['priority_walk'] = list(work) if work else None
    if not work:
        progress['priority_cutoff_ns'] = int(progress.get('priority_walk_started_at', time.time())*1e9)-120_000_000_000
    old = {item['path']:item for item in progress.get('priority_pending', [])}
    old.update(selected)
    items = sorted(old.values(), key=lambda item:(
        0 if '/run-' in item['path'] or '/trajectories/' in item['path'] else
        1 if item['path'].startswith('.state/') else 2,
        -item['source']['mtime_ns'], item['path']))
    return items, warnings, not fast and not work


def warning_counts(warnings):
    """Classify without discarding the original path/type evidence."""
    kinds = Counter(); categories = Counter()
    for warning in warnings:
        rel = Path(warning['path']); kind = warning['kind']; kinds[kind] += 1
        git = '.git' in rel.parts or any(part.endswith('.git') for part in rel.parts)
        if kind in {'FileNotFoundError', 'source_disappeared'} and git:
            category = 'git_entry_disappeared'
        elif kind in {'FileNotFoundError', 'source_disappeared'} and rel.name == 'current-tool':
            category = 'transient_tool_marker_disappeared'
        elif kind in {'FileNotFoundError', 'source_disappeared'}:
            category = 'source_file_disappeared'
        else:category = 'copy_or_inventory_error'
        categories[category] += 1
    return {'count':len(warnings), 'by_kind':dict(kinds), 'by_category':dict(categories)}


def cycle(source, target, budget=300, includes=None, receipt=None, workers=4, recovery=False):
    source = Path(source).resolve(strict=True);target=Path(target)
    if workers < 1 or workers > 64:raise ValueError('Backup workers must be between 1 and 64')
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
        if manifest.get('recovery') not in (None,recovery):raise ValueError('Cannot change backup purpose')
        manifest['recovery']=recovery
        progress=json.loads(progress_path.read_text()) if progress_path.exists() else {}
        row=dict(identity,started_at=started,status='running',copied=0,unchanged=0,warnings=[],
                 coverage=manifest.get('scope','selected_scope' if includes else 'recovery_payload'),
                 purpose='recovery_import' if recovery else 'independent_backup',
                 consistency='per-file-verified-live-inventory',last_success_at=manifest.get('last_success_at'))
        latest=Path(receipt) if receipt else target/'latest.json'
        if not latest.resolve().is_relative_to(WORKSPACE):raise ValueError('Receipt must be workspace-local')
        atomic(latest,row)
        try:
            resuming = progress.get('identity') == identity and any((
                progress.get('pending'), progress.get('priority_pending'),
                progress.get('priority_walk'), progress.get('inventory_scanning')))
            if not resuming:
                progress=dict(identity=identity,inventory_at=started,pending=[],warnings=[],
                              inventory_count=0,inventory_paths=[],inventory_scanning=True)
            if progress.get('inventory_scanning'):
                row['phase']='bulk_inventory'
                atomic(latest,row)
                advance_inventory(source,includes,progress,min(deadline,time.monotonic()+budget*0.2))
                atomic(progress_path,progress)
            remaining=progress['pending'];row['warnings']=list(progress.get('warnings',[]))
            row['carried_warning_count']=len(row['warnings']) if resuming else 0
            completed=0;checkpoint=time.monotonic();reported=checkpoint;checked_parents=set()
            def process(item, copy_deadline=deadline):
                if time.monotonic()>copy_deadline:raise BudgetExpired()
                rel=item['path'];destination=target/'data'/rel
                try:
                    current=inspect_source(source/rel)
                    if current!=item['source']:
                        # Re-inventory a mutable file rather than retrying its
                        # stale size/mtime forever. Record the changed snapshot.
                        item['source']=current
                    if unchanged(manifest['files'].get(rel),item,destination):return rel,None,None
                    roots=(source,target/'data') if recovery else None
                    return rel,copy_file(source/rel,destination,item['source'],copy_deadline,checked_parents,roots),None
                except (OSError,ValueError) as error:
                    return rel,None,{'path':rel,'kind':type(error).__name__}
            if resuming and not recovery:
                # Reserve at least half the cycle for the resumable bulk copy.
                # A large historical backlog cannot starve live author work,
                # and continuously growing logs cannot starve the backlog.
                hot_deadline = (min(deadline, time.monotonic()+budget*0.5)
                    if remaining or progress.get('inventory_scanning') else deadline)
                row['phase']='priority_inventory'
                atomic(latest,row)
                hot, hot_warnings, scan_complete = priority_inventory(
                    source, includes, progress, min(hot_deadline, time.monotonic()+budget*0.2))
                hot_completed = 0
                row.update(priority_copied=0, priority_unchanged=0, priority_warnings=hot_warnings)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    work=iter(hot);pending=deque()
                    for _ in range(workers):
                        item=next(work,None)
                        if item is not None:pending.append(pool.submit(process,item,hot_deadline))
                    try:
                        while pending:
                            rel,record,warning=pending.popleft().result()
                            if warning:row['priority_warnings'].append(warning)
                            elif record is None:
                                row['priority_unchanged']+=1;row['unchanged']+=1
                            else:
                                manifest['files'][rel]=record
                                row['priority_copied']+=1;row['copied']+=1
                            if not warning:
                                manifest['files'][rel]['source_checked_at']=time.time()
                            hot_completed+=1
                            now=time.monotonic()
                            if now-checkpoint>=60:
                                progress['priority_pending']=hot[hot_completed:]
                                atomic(manifest_path,manifest);atomic(progress_path,progress);checkpoint=now
                            if now-reported>=15:
                                row.update(phase='priority_copy',priority_remaining_files=len(hot)-hot_completed,
                                           priority_scan_remaining_dirs=len(progress.get('priority_walk') or []),
                                           verified_files=len(manifest['files']),elapsed_seconds=round(time.time()-started,2))
                                atomic(latest,row);reported=now
                            item=next(work,None)
                            if item is not None:pending.append(pool.submit(process,item,hot_deadline))
                    except BudgetExpired:
                        pass
                    finally:
                        for future in pending:future.cancel()
                progress['priority_pending']=hot[hot_completed:]
                progress['priority_paths']=sorted(set(progress.get('priority_paths', [])) | {
                    item['path'] for item in hot[:hot_completed] if item['path'] in manifest['files']})
                row.update(priority_remaining_files=len(progress['priority_pending']),
                           priority_scan_remaining_dirs=len(progress.get('priority_walk') or []),
                           priority_scan_complete=scan_complete, priority_checked_at=started)
                atomic(manifest_path,manifest);atomic(progress_path,progress)
                row['phase']='bulk_copy'
                atomic(latest,row)
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
                        if not warning:manifest['files'][rel]['source_checked_at']=time.time()
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
            row['status']='partial' if (row['warnings'] or row.get('priority_warnings')
                or progress.get('priority_pending') or progress.get('priority_walk')
                or progress.get('inventory_scanning')) else 'ok'
            if row['status']=='ok':
                manifest['last_success_at']=time.time()
                manifest['last_complete_inventory_at']=progress['inventory_at']
                manifest['complete_inventory_paths']=sorted(set(progress['inventory_paths']) | set(progress.get('priority_paths', [])))
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
                   inventory_remaining_paths=len(progress.get('inventory_walk',[])),
                   priority_remaining_files=len(progress.get('priority_pending',[])),
                   priority_scan_remaining_dirs=len(progress.get('priority_walk') or []),
                   last_complete_inventory_at=manifest.get('last_complete_inventory_at'))
        row['warning_summary'] = {
            'carried_from_inventory':warning_counts(row['warnings'][:row.get('carried_warning_count', 0)]),
            'new_bulk':warning_counts(row['warnings'][row.get('carried_warning_count', 0):]),
            'this_cycle_priority':warning_counts(row.get('priority_warnings', [])),
        }
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
    p.add_argument('--owner-pid',type=int,help='Exit when the owning launcher exits or its PID is reused')
    p.add_argument('--recovery-import',action='store_true',help='Preserve owner/mode for a private recovery staging tree')
    p.add_argument('--include',action='append',default=[])
    args=p.parse_args()
    if args.budget<=0 or args.interval<=0:raise SystemExit('Budget and interval must be positive')
    from command_watchdog import process_info, owner_alive
    owner=process_info(args.owner_pid) if args.owner_pid is not None else None
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    while not stopping and owner_alive(owner):
        result=cycle(args.source,args.target,args.budget,args.include,args.receipt,args.workers,args.recovery_import)
        print(json.dumps({k:result.get(k) for k in ('status','reason','error_type','copied','unchanged',
            'priority_copied','priority_remaining_files','remaining_files','last_success_at','warning_summary')}),flush=True)
        if args.once:return 0 if result['status'] in ('ok','already_running') else 2
        until=time.monotonic()+args.interval
        while not stopping and owner_alive(owner) and time.monotonic()<until:
            time.sleep(min(30,max(0,until-time.monotonic())))


if __name__=='__main__':raise SystemExit(main())
