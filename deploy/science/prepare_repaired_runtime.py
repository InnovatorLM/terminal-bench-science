"""Inventory a stopped runtime and checkpoint small controls without copying rootfs.

This produces a migration plan, not a runnable universe. Latest mutable state
must be restored and path-bound evidence revalidated before a controller starts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

WORKSPACE=Path('/mnt/data/zhanglinfeng/workspace')
ROLE_BASE=Path('.config/gewu-multiverse/agents')
SOFTWARE=('usr/local/bin/python3','home/role/.local/env/bin/python','bin/bash','usr/bin/git')
CONTROL_FILES=(
    '.config/gewu-multiverse/multiverse.toml',
    '.config/gewu-multiverse/credentials.toml',
    '.config/gewu-multiverse/last-seen.toml',
    '.config/science-evaluation/queue.json',
    '.config/science-evaluation/registry.json',
    '.state/gewu-multiverse/role-broker/config.json',
    '.state/gewu-multiverse/deployment.json',
    '.state/gewu-multiverse/review-dispatcher/ledger.json',
    '.state/gewu-multiverse/review-dispatcher/config.json',
    '.state/gewu-multiverse/author-selftest/config.json',
    '.state/gewu-multiverse/command-watchdog/config.json',
)


def valid_source(path):
    path=Path(path).resolve(strict=True)
    if not path.is_relative_to(WORKSPACE) and not str(path).startswith('/tmp/gewu-terminal-bench-science-runtime-'):
        raise ValueError('Source must be workspace-local or a named historical runtime')
    return path


def private_json(path,data):
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(data,stream,indent=2);stream.write('\n')


def inventory(path,deadline,limit=100000):
    """Bound metadata traversal and report incomplete inventories explicitly."""
    result={'exists':path.exists(),'files':0,'bytes':0,'directories':0,'symlinks':0,'complete':True}
    if not result['exists']:return result
    stack=[path]
    try:
        while stack:
            if time.monotonic()>deadline or result['files']+result['directories']>=limit:
                result.update(complete=False,reason='inventory_budget');break
            current=stack.pop();info=current.lstat()
            if stat.S_ISLNK(info.st_mode):result['symlinks']+=1
            elif stat.S_ISREG(info.st_mode):result['files']+=1;result['bytes']+=info.st_size
            elif stat.S_ISDIR(info.st_mode):
                result['directories']+=1
                with os.scandir(current) as entries:
                    for entry in entries:
                        if time.monotonic()>deadline:
                            result.update(complete=False,reason='inventory_budget');return result
                        stack.append(Path(entry.path))
            else:result.setdefault('special_files',0);result['special_files']+=1
    except OSError as error:result.update(complete=False,reason=type(error).__name__)
    return result


def prepare(source,donor,output,seconds=20):
    source=valid_source(source);donor=Path(donor).resolve(strict=True);output=Path(output)
    if not donor.is_relative_to(WORKSPACE):raise ValueError('Software donor must be inside workspace')
    if not output.is_absolute() or not output.resolve().is_relative_to(WORKSPACE) or output.exists():
        raise ValueError('Require fresh workspace-local output')
    broker_path=source/'.state/gewu-multiverse/role-broker/config.json'
    broker=json.loads(broker_path.read_text())
    roles=broker.get('roles')
    if not isinstance(roles,dict) or not roles:raise ValueError('Missing source role identities')
    output.mkdir(parents=True,mode=0o700)
    controls=output/'controls';controls.mkdir(mode=0o700)
    records=[]
    selected=list(CONTROL_FILES)
    for role in roles:
        if not role.replace('-','').isalnum():raise ValueError('Invalid role identity')
        selected.extend(str(ROLE_BASE/role/p) for p in ['instruction.md','review-feedback.json','review-current-assignment.json','home/role/.state/author-iteration/ledger.json'])
    for relative in selected:
        src=source/relative
        try:info=src.lstat()
        except FileNotFoundError:continue
        if not stat.S_ISREG(info.st_mode) or info.st_size>2*1024*1024:
            raise ValueError('Control file is linked, special or too large')
        data=src.read_bytes();after=src.stat()
        if (info.st_size,info.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Source changed while preparing controls')
        dst=controls/relative;dst.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(data)
        records.append({'path':relative,'sha256':hashlib.sha256(data).hexdigest(),
                        'bytes':len(data),'source_uid':info.st_uid,'source_gid':info.st_gid,'source_mode':stat.S_IMODE(info.st_mode)})
    role_plans=[];scopes=[]
    for role,entry in roles.items():
        root=source/ROLE_BASE/role;donor_root=donor/ROLE_BASE/role
        role_plans.append({'role':role,'uid':entry['uid'],'gid':entry['gid'],
            'latest_root':str(root),'workspace_software_root':str(donor_root),
            'donor_software_present':{name:(donor_root/name).is_file() for name in SOFTWARE},
            'latest_software_present':{name:(root/name).is_file() for name in SOFTWARE},
            'software_copy_performed':False})
        scopes.extend([ROLE_BASE/role/'workspace',ROLE_BASE/role/'selftest',ROLE_BASE/role/'evaluation',
                       ROLE_BASE/role/'home/role/.state'])
    scopes.extend([Path('.state/gewu-multiverse'),Path('.local/share/gewu-multiverse/forgejo/instance/data')])
    deadline=time.monotonic()+max(1,seconds)
    estimates=[]
    for scope in scopes:
        estimates.append({'path':str(scope),**inventory(source/scope,deadline)})
    result={'schema_version':1,'status':'control_checkpoint_only','source':str(source),
        'software_donor':str(donor),'roles':role_plans,'controls':records,'mutable_scopes':estimates,
        'runnable':False,'production_started':False,'software_copied':False,
        'estimated_inventory_complete':all(row['complete'] for row in estimates),
        'required_before_start':[
            'Restore latest role workspace/Git, selftest/evaluation evidence and complete controller trajectories; never overwrite them with donor history.',
            'Restore Forgejo database and repository objects from the same stopped source; preserve required signing secrets privately.',
            'Reuse workspace-owned software only after per-role interpreter/import/capability validation; no shared writable environment between roles.',
            'Rewrite service descriptors and runtime roots explicitly; revalidate evidence hashes instead of bulk-rewriting historical signed JSON.',
            'Install current role clients and skills, rebuild the controller, inspect exact review assignments and orphan cleanup before enabling authors.',
            'Preserve source UID/GID and access modes when restoring controls; checkpoint files are root-private and are not deployment-ready.',
        ]}
    private_json(output/'migration-plan.json',result)
    for record in records:
        if hashlib.sha256((controls/record['path']).read_bytes()).hexdigest()!=record['sha256']:
            raise ValueError('Control checkpoint verification failed')
    return result


SOFTWARE_SCOPES=('bin','usr','lib','lib64','etc','opt','home/role/.local')


def replace_private_json(path,data):
    """Write a small resumable journal without exposing its private payload."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    pending=path.with_name(path.name+'.pending')
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(data,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    pending.chmod(0o600);pending.replace(path)


def verify_role_software(root,entry):
    """Probe the moved environment as its own role, without model/cloud calls."""
    program='import json,os,numpy,scipy; assert int(numpy.arange(5).sum())==10; print(json.dumps({"uid":os.getuid(),"numpy":numpy.__version__,"scipy":scipy.__version__,"calculation":10}))'
    result=subprocess.run(['setpriv','--no-new-privs','chroot',
        '--userspec='+str(entry['uid'])+':'+str(entry['gid']),
        '--groups='+str(entry['gid']),str(root),'/usr/bin/env','-i',
        'HOME=/home/role','PATH=/home/role/.local/env/bin:/usr/local/bin:/usr/bin:/bin',
        'LD_LIBRARY_PATH=/home/role/.local/env/lib','PYTHONDONTWRITEBYTECODE=1',
        '/home/role/.local/env/bin/python','-c',program],
        capture_output=True,text=True,timeout=180)
    if result.returncode:
        raise RuntimeError('Isolated software probe failed with exit code '+str(result.returncode))
    receipt=json.loads(result.stdout)
    if receipt['uid']!=entry['uid']:raise RuntimeError('Software probe ran as wrong role')
    return receipt


def stage_software(source,donor,runtime,journal_path,probe=verify_role_software):
    """Move stopped workspace-owned software, preserving independent role roots.

    The historical live source remains read-only. Every move is same-filesystem
    and journaled before/after; mutable workspaces and donor control state stay
    in place. Re-running resumes only journaled moves, never overwrites data.
    """
    from role_exec import role_processes
    source=valid_source(source);donor=Path(donor).resolve(strict=True)
    runtime=Path(runtime).absolute();journal_path=Path(journal_path).absolute()
    for path in (donor,runtime,journal_path):
        if not path.resolve().is_relative_to(WORKSPACE) or path.resolve()==WORKSPACE:
            raise ValueError('All software migration writes must remain below workspace')
    if runtime==donor or runtime.is_relative_to(donor) or donor.is_relative_to(runtime):
        raise ValueError('Donor and target roots must not overlap')
    broker=json.loads((source/'.state/gewu-multiverse/role-broker/config.json').read_text())
    roles=broker['roles']
    for role,entry in roles.items():
        if role_processes(entry['uid']):raise RuntimeError('Role still has live processes: '+role)
    # Root-owned management processes must also not be using donor cwd/chroots.
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name)==os.getpid():continue
        for name in ('cwd','root'):
            try:location=(process/name).resolve(strict=True)
            except (FileNotFoundError,PermissionError,ProcessLookupError):continue
            if location==donor or location.is_relative_to(donor):
                raise RuntimeError('Software donor is in use by process '+process.name)
    runtime.mkdir(parents=True,exist_ok=True,mode=0o755)
    if journal_path.exists():
        journal=json.loads(journal_path.read_text())
        if (journal.get('source'),journal.get('donor'),journal.get('runtime'))!=(str(source),str(donor),str(runtime)):
            raise ValueError('Migration journal belongs to another deployment')
    else:
        journal={'schema_version':1,'source':str(source),'donor':str(donor),'runtime':str(runtime),
                 'status':'moving_software','production_started':False,'roles':{},'started_at':time.time()}
        replace_private_json(journal_path,journal)
    for role,entry in roles.items():
        if not role.replace('-','').isalnum():raise ValueError('Invalid role')
        record=journal['roles'].setdefault(role,{'uid':entry['uid'],'gid':entry['gid'],'moves':[]})
        if record.get('status')=='verified':continue
        origin=donor/ROLE_BASE/role;target=runtime/ROLE_BASE/role
        if target.is_symlink() or origin.is_symlink():raise ValueError('Role root link refused')
        target.mkdir(parents=True,exist_ok=True,mode=0o755);target.chmod(0o755)
        home=target/'home';home.mkdir(exist_ok=True,mode=0o755);home.chmod(0o755)
        private_home=home/'role';private_home.mkdir(exist_ok=True,mode=0o700)
        os.chown(private_home,entry['uid'],entry['gid']);private_home.chmod(0o700)
        for relative in SOFTWARE_SCOPES:
            src=origin/relative;dst=target/relative
            item=next((x for x in record['moves'] if x['scope']==relative),None)
            if item and item['status']=='moved':
                current=dst.lstat()
                if (current.st_dev,current.st_ino)!=(item['device'],item['inode']) or src.exists():
                    raise ValueError('Journaled software move changed unexpectedly')
                continue
            if item and item['status']=='moving' and not src.exists() and dst.exists():
                current=dst.lstat()
                if (current.st_dev,current.st_ino)!=(item['device'],item['inode']):raise ValueError('Interrupted move identity mismatch')
                item['status']='moved';replace_private_json(journal_path,journal);continue
            if not src.exists() and not src.is_symlink():continue
            if dst.exists() or dst.is_symlink():raise ValueError('Software destination already exists: '+role+'/'+relative)
            info=src.lstat()
            if not stat.S_ISDIR(info.st_mode):raise ValueError('Software scope must be a real directory')
            dst.parent.mkdir(parents=True,exist_ok=True)
            if info.st_dev!=dst.parent.stat().st_dev:raise ValueError('Software move requires the same filesystem')
            if item is None:
                item={'scope':relative,'source':str(src),'target':str(dst),'device':info.st_dev,'inode':info.st_ino,
                      'uid':info.st_uid,'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode),'status':'moving'}
                record['moves'].append(item)
            replace_private_json(journal_path,journal)
            src.rename(dst)
            item['status']='moved';replace_private_json(journal_path,journal)
        for relative,owner,mode in [('dev',0,0o755),('proc',0,0o755),('tmp',entry['uid'],0o700),('cache',entry['uid'],0o700),('runs',entry['uid'],0o700)]:
            directory=target/relative;directory.mkdir(exist_ok=True,mode=mode)
            os.chown(directory,owner,entry['gid'] if owner else 0);directory.chmod(mode)
        sink=target/'dev/null';sink.touch(exist_ok=True);sink.chmod(0o666)
        try:record['probe']=probe(target,entry)
        except BaseException as error:
            record['status']='probe_failed';record['error_type']=type(error).__name__
            journal['status']='software_probe_failed';replace_private_json(journal_path,journal);raise
        record['status']='verified';record['verified_at']=time.time();replace_private_json(journal_path,journal)
        print(json.dumps({'role':role,'software':'moved_and_verified','uid':entry['uid']}),flush=True)
    journal['status']='software_ready_payload_pending';journal['software_finished_at']=time.time()
    replace_private_json(journal_path,journal)
    return journal


def restore_private_controls(source,runtime,journal_path):
    """Copy only deployment credentials, privately, from the matching live source."""
    source=valid_source(source);runtime=Path(runtime).resolve(strict=True)
    if not runtime.is_relative_to(WORKSPACE):raise ValueError('Runtime must remain in workspace')
    broker=json.loads((source/'.state/gewu-multiverse/role-broker/config.json').read_text())
    selected=[Path('.config/gewu-multiverse/credentials.toml')]
    instance=Path('.local/share/gewu-multiverse/forgejo/instance')
    selected.append(instance/'app.ini')
    for name in ('home','secrets','custom'):
        directory=source/instance/name
        if directory.exists():
            for path in directory.rglob('*'):
                if path.is_symlink():raise ValueError('Linked private Forgejo state refused')
                if path.is_file():selected.append(path.relative_to(source))
    for role in broker['roles']:
        selected.append(ROLE_BASE/role/'home/role/git-credential.json')
    records=[]
    for relative in selected:
        src=source/relative;info=src.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size>2*1024*1024:
            raise ValueError('Private control is not a small regular file')
        data=src.read_bytes();dst=runtime/relative
        if dst.exists():
            if dst.is_symlink() or dst.read_bytes()!=data:raise ValueError('Private target differs; refusing overwrite')
        else:
            dst.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(data)
        dst.chmod(0o600);os.chown(dst,info.st_uid,info.st_gid)
        # Copy private subtree ownership without changing broad role parents.
        if 'home/role/' in str(relative) or relative.parent in (instance/'home',instance/'secrets'):
            dst.parent.chmod(0o700);os.chown(dst.parent,info.st_uid,info.st_gid)
        records.append({'path':str(relative),'bytes':len(data),'uid':info.st_uid,'gid':info.st_gid,
                        'sha256':hashlib.sha256(data).hexdigest(),'mode':0o600})
    receipt={'status':'private_controls_restored','source':str(source),'runtime':str(runtime),
             'files':records,'production_started':False,'restored_at':time.time()}
    replace_private_json(journal_path,receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--software-donor',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--inventory-seconds',type=int,default=20)
    parser.add_argument('--runtime-root',type=Path,help='Move stopped donor software to this runtime; --output names the private migration journal')
    args=parser.parse_args()
    if args.runtime_root:
        result=stage_software(args.source,args.software_donor,args.runtime_root,args.output)
        print(json.dumps({'status':result['status'],'roles':len(result['roles']),'production_started':False}))
        return
    result=prepare(args.source,args.software_donor,args.output,args.inventory_seconds)
    print(json.dumps({'status':result['status'],'roles':len(result['roles']),
        'control_files':len(result['controls']),'inventory_complete':result['estimated_inventory_complete'],
        'runnable':False,'output':str(args.output)}))


if __name__=='__main__':main()
