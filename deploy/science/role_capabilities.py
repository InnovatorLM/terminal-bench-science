"""Operator capability probes run as the actual isolated role, without secrets."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import errno

from evaluation_queue import W, atomic_json
from install_role_skills import selected, bundle, discovery, SOURCE


def read_text_retry(path, attempts=8):
    """Read CPFS-backed control files through transient stale handles.

    CPFS can return ESTALE/EIO for a recently replaced small file.  Capability
    probing is a startup gate, so one transient metadata failure must not make
    the whole universe appear unstartable.
    """
    last = None
    for attempt in range(attempts):
        try:
            with open(path, 'rb') as stream:
                return stream.read().decode()
        except OSError as error:
            last = error
            if error.errno not in (errno.ESTALE, errno.EIO, errno.EINTR):
                raise
            time.sleep(0.25 * (attempt + 1))
    raise last

MODULES = ('numpy','scipy','pytest','pydantic','yaml')
TOOLS = ('bash','git','tar','gzip','seq','nohup','curl','role-install','role-forgejo')

PROBE = '''import importlib,json,os,shutil,sys,subprocess
os.chdir('/')
modules={}
for name in ['numpy','scipy','pytest','pydantic','yaml']:
 try:
  m=importlib.import_module(name);modules[name]={'ok':True,'version':getattr(m,'__version__','unknown')}
 except Exception as e:modules[name]={'ok':False,'error_type':type(e).__name__}
tools={name:shutil.which(name) for name in ['bash','git','tar','gzip','seq','nohup','curl','role-install','role-forgejo']}
commands={}
for name in sys.argv[1:]:
 executable=shutil.which(name)
 try:
  completed=subprocess.run([executable,'--help'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10) if executable else None
  commands[name]={'executable':executable,'help_ok':completed is not None and completed.returncode==0}
 except (OSError,subprocess.TimeoutExpired):commands[name]={'executable':executable,'help_ok':False}
print(json.dumps({'python':sys.executable,'uid':os.getuid(),'modules':modules,'tools':tools,'commands':commands}))
'''


def enabled_roles(config, settings):
    # Probe roles that are actually enabled for this runtime. Stopped roles may
    # be provisioned later; requiring their full scientific environments here
    # would block an evaluator-only recovery on a local runtime snapshot.
    configured={row['name'] for row in settings['agents']}
    if configured-set(config['roles']):
        raise ValueError('Configured role absent from broker configuration (including stopped roles)')
    return {row['name'] for row in settings['agents'] if row.get('desired')=='started'}


def inspect(role, entry, runner=subprocess.run):
    root = Path(entry['root']).resolve(strict=True)
    if not root.is_relative_to(W) and not str(root).startswith('/tmp/gewu-terminal-bench-science-runtime-'):
        raise ValueError('Role root outside workspace')
    skill_discovery=discovery(root/'skills')
    skills = {}; resources = {}; skills_current = {}
    for name in selected(role):
        path = root/'skills'/name/'SKILL.md'
        if not path.is_file() or path.is_symlink(): raise ValueError('Missing required skill: '+name)
        skills[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        resources[name] = bundle(path.parent)
        skills_current[name] = resources[name] == bundle(SOURCE/name)
    instruction = root/'instruction.md'
    python = '/home/role/.local/env/bin/python'
    local_runtime = str(root).startswith('/tmp/gewu-terminal-bench-science-runtime-')
    if local_runtime and not (root/python.lstrip('/')).is_file():
        python = '/usr/local/bin/python3'
    if not (root/python.lstrip('/')).is_file(): raise ValueError('Default scientific environment missing')
    required = ['role-acs-selftest'] if role.startswith('author-') else (['role-evaluation-batch'] if role.startswith('evaluator-') else [])
    command = ['/usr/bin/setpriv','--no-new-privs','/usr/sbin/chroot',
               '--userspec='+str(entry['uid'])+':'+str(entry['gid']),
               '--groups='+str(entry['gid']),str(root),'/usr/bin/env','-i',
               'HOME=/home/role','PATH=/home/role/.local/bin:/home/role/.local/env/bin:/usr/local/bin:/usr/bin:/bin',
               'LD_LIBRARY_PATH=/home/role/.local/env/lib','PYTHONDONTWRITEBYTECODE=1',python,'-c',PROBE,*required]
    result = runner(command, capture_output=True, text=True, timeout=120)
    if result.returncode: raise ValueError('Isolated capability probe failed; no raw output published')
    observed = json.loads(result.stdout)
    if observed.get('uid') != entry['uid']: raise ValueError('Probe ran under unexpected identity')
    entrypoints = {name:(root/'usr/local/bin'/name).is_file() for name in required}
    if role.startswith('author-'):
        deployed=root/'usr/local/bin/role-acs-selftest'
        expected=Path(__file__).with_name('role_acs_selftest.py')
        entrypoints['selftest_client_current']=deployed.is_file() and not deployed.is_symlink() and hashlib.sha256(deployed.read_bytes()).digest()==hashlib.sha256(expected.read_bytes()).digest()
    if role.startswith('evaluator-'):
        deployed=root/'usr/local/bin/role-evaluation-batch'
        expected=Path(__file__).with_name('role_evaluation_batch.py')
        entrypoints['role-evaluation-batch']=deployed.is_file() and not deployed.is_symlink() and os.access(deployed,os.X_OK)
        entrypoints['evaluation_batch_client_current']=entrypoints['role-evaluation-batch'] and hashlib.sha256(deployed.read_bytes()).digest()==hashlib.sha256(expected.read_bytes()).digest()
    if set(observed.get('modules',{}))!=set(MODULES) or set(observed.get('tools',{}))!=set(TOOLS):
        raise ValueError('Incomplete capability probe response')
    command_checks=observed.get('commands',{})
    if set(command_checks)!=set(required):raise ValueError('Incomplete command capability probe response')
    healthy = (all(v.get('ok') is True for v in observed['modules'].values()) and all(observed['tools'].values())
               and all(entrypoints.values()) and all(skills_current.values())
               and all(v.get('help_ok') is True and v.get('executable') for v in command_checks.values()))
    return dict(role=role,observed_at=time.time(),ready=healthy,skills=skills,skill_discovery=skill_discovery,
                instruction_sha256=hashlib.sha256(instruction.read_bytes()).hexdigest(),
                skill_resources=resources,skills_current=skills_current,
                environment=observed,entrypoints=entrypoints,
                scope='Executable local capability check; cloud queue liveness and model skill use require separate verification')


@contextlib.contextmanager
def probe_lock(config,role,wait_seconds=30):
    # Broker command cleanup owns the entire role UID. An operator probe must
    # use the same lock or normal command completion can kill its interpreter.
    base=Path(config['_lock_dir']) if config.get('_lock_dir') else Path(config['socket'].removeprefix('@')).parent
    if not base.resolve().is_relative_to(W.resolve()) and not str(base).startswith('/tmp/gewu-terminal-bench-science-runtime-'):
        raise ValueError('Probe lock outside workspace')
    base.mkdir(parents=True,exist_ok=True)
    with (base/(role+'.lock')).open('a') as stream:
        deadline=time.monotonic()+wait_seconds
        while True:
            try:
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise TimeoutError('Role is busy; capability probe deferred')
                time.sleep(.1)
        try:yield
        finally:fcntl.flock(stream,fcntl.LOCK_UN)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=W/'.state/science-role-broker/config.json');p.add_argument('--role');p.add_argument('--settings',type=Path,help='Check only roles configured desired=started');args=p.parse_args()
    if os.geteuid()!=0: raise ValueError('Operator required')
    config=json.loads(read_text_retry(args.config));failed=[]
    enabled=None
    if args.settings:
        import tomllib
        settings=tomllib.loads(read_text_retry(args.settings))
        enabled=enabled_roles(config,settings)
    for role,entry in config['roles'].items():
        if args.role and role!=args.role:continue
        if enabled is not None and role not in enabled:continue
        try:
            with probe_lock(config,role):
                receipt=inspect(role,entry)
        except TimeoutError:
            print(json.dumps(dict(role=role,deferred=True,reason='Role command is active; prior capability receipt retained')))
            failed.append(role);continue
        except (ValueError,OSError,subprocess.SubprocessError,json.JSONDecodeError) as error:
            receipt=dict(role=role,ready=False,observed_at=time.time(),error_type=type(error).__name__,
                         reason='Capability probe did not complete; inspect operator evidence before scheduling')
        path=Path(entry['root'])/'capabilities.json';atomic_json(path,receipt,0o644)
        print(json.dumps(dict(role=role,ready=receipt['ready'],skills=list(receipt.get('skills',{})))))
        if not receipt['ready']:failed.append(role)
    if args.role and args.role not in config['roles']:raise ValueError('Unknown role')
    if failed:raise SystemExit('Capability checks failed for: '+','.join(failed))


if __name__=='__main__':main()
