"""Operator-owned author selftest queue: frozen upload -> ACR -> ACS oracle/nop."""
import contextlib,json,os,re,signal,subprocess,sys,tarfile,hashlib,time,fcntl
from pathlib import Path,PurePosixPath
from evaluation_queue import W,atomic_json,digest_tree
from check_harbor_task import inspect_task
from acs_network_preflight import NetworkPreflightError
from selftest_lifecycle import owned_run, recover_interrupted, install_stop_handlers
from build_task_images import builder_slot,validate_dockerfile
from selftest_source import verify_source
from selftest_logs import credential_values, publish as publish_logs
from selftest_upload import snapshot_upload
from acs_result_diagnostics import classify_trial
STATE=Path(os.environ.get('GEWU_AUTHOR_SELFTEST_STATE',W/'.state/science-author-selftest'))
PYTHON=W/'acs-harbor-test/.venv/bin/python'
BASE='public.ecr.aws/docker/library/python@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377'
def build_command(repo,commit,name,output):
 codex=Path(os.environ.get('GEWU_CODEX_BINARY',str(W/'.local/libexec/science-runtime/codex')))
 rg=Path(os.environ.get('GEWU_RG_BINARY',str(W/'.local/libexec/science-runtime/rg')))
 for binary in (codex,rg):
  if not binary.resolve().is_relative_to(W.resolve()) or not binary.is_file() or not os.access(binary,os.X_OK):
   raise ValueError('Missing workspace-local Codex runtime binary: '+str(binary))
 return [str(PYTHON),str(Path(__file__).with_name('build_task_images.py')),'--repo',str(repo),'--commit',commit,'--task-path',name,'--output',str(output),'--python-base',BASE,'--harness','codex','--codex-binary',str(codex),'--rg-binary',str(rg),'--publish']
def unpack(archive,destination):
 with tarfile.open(archive) as t:
  members=t.getmembers()
  if len(members)>10000 or sum(m.size for m in members)>128*1024*1024:raise ValueError('Archive too large')
  for m in members:
   p=PurePosixPath(m.name)
   if p.is_absolute() or '..' in p.parts or not(m.isfile() or m.isdir()):raise ValueError('Unsafe task archive')
  destination.mkdir(parents=True)
  for m in members:
   p=destination/m.name
   if m.isdir():p.mkdir(parents=True,exist_ok=True)
   else:
    p.parent.mkdir(parents=True,exist_ok=True)
    with t.extractfile(m) as src,p.open('xb') as out:__import__('shutil').copyfileobj(src,out)
    p.chmod(0o700 if m.mode&0o111 else 0o600)
def process(role,entry,request):
 job=request.stem
 if not re.fullmatch('[a-f0-9]{32}',job):return
 root=Path(entry['root']);result=root/'selftest/results'/(job+'.json');work=STATE/job
 def publish(payload):
  atomic_json(result,payload);os.chown(result,0,entry['gid']);result.chmod(0o640)
 if recover_interrupted(work,result,publish):return
 work.mkdir(mode=0o700);status={'schema_version':2,'job_id':job,'role':role,'status':'validating','quality_acceptance':False,'received_at':time.time()}
 secrets=credential_values(root)
 def save():
  status['diagnostics']=publish_logs(work,root,job,entry['gid'],secrets)
  status['updated_at']=time.time();publish(status);atomic_json(work/'status.json',status)
 save()
 try:
  q=json.loads(snapshot_upload(request,entry['uid'],16384));name=q['task_name'];archive=request.with_suffix('.tar')
  if q.get('idea_id') is not None:
   if not isinstance(q['idea_id'],str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,95}',q['idea_id']):raise ValueError('Invalid stable idea ID')
   status['idea_id']=q['idea_id']
  if q.get('request_review') is not None and q['request_review'] is not True:raise ValueError('Invalid review request')
  if q.get('request_review') and not q.get('idea_id'):raise ValueError('Review request requires stable idea ID')
  if q.get('initial_review') or (q.get('request_review') and not q.get('iteration')):
   raise ValueError('Review request requires previous selftest and result-driven iteration; initial_review bypass is not supported')
  atomic_json(work/'request.json',q)
  if q['job_id']!=job or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,90}',name) or not re.fullmatch('[a-f0-9]{40}',q['source_commit']):raise ValueError('Invalid request identity')
  frozen=work/'upload.tar';snapshot_upload(archive,entry['uid'],140*1024*1024,frozen)
  if hashlib.sha256(frozen.read_bytes()).hexdigest()!=q['archive_sha256']:raise ValueError('Upload changed')
  repo=work/'source';task=repo/name;unpack(frozen,task)
  # Validate the real task policy against the same job environment used below,
  # before acquiring the builder or starting any cloud work. Never rewrite the
  # frozen source to force a verifier mode or change its network contract.
  run_template=json.loads((W/'.config/harbor-api/template.json').read_text())
  mechanical=inspect_task(task,acs_environment=run_template['environment'])
  atomic_json(work/'mechanical.json',mechanical)
  status['network_preflight']=mechanical['acs_network_preflight']
  status.update(verify_source(root/'workspace',q['source_repository'],q['source_task_path'],q['source_commit'],task));save()
  for args in [['init','-q'],['config','user.email','operator@localhost'],['config','user.name','operator'],['add','.'],['commit','-qm','Frozen author upload']]:subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
  commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip();status.update(status='waiting_builder',source_commit=q['source_commit'],execution_commit=commit,execution_task_path=name,snapshot_sha256=digest_tree(task));save()
  if 'iteration' in q:
   from selftest_iteration import validate
   status['iteration']=validate(q['iteration'],status,STATE,role);save()
  task_tmp=work/'tmp';task_home=work/'home';task_tmp.mkdir(mode=0o700,exist_ok=True);task_home.mkdir(mode=0o700,exist_ok=True)
  env=os.environ.copy();env.update(TMPDIR=str(task_tmp),PYTHONDONTWRITEBYTECODE='1',HOME=str(task_home))
  # Explicit deployment transport for public Docker official images only.
  # pin_bases retains the original reference and resolved immutable digest.
  env.setdefault('GEWU_DOCKER_LIBRARY_MIRROR','public.ecr.aws/docker/library')
  # Wait outside the timed build so a busy builder never looks like a broken
  # task. The child inherits this exact locked descriptor, rather than trying
  # to acquire the same slot again and deadlocking.
  with builder_slot() as lock_fd:
   status['status']='building';save()
   with (work/'build.log').open('w') as log:
    r=owned_run(build_command(repo,commit,name,work/'build')+['--lock-fd',str(lock_fd)],env=env,stdout=log,stderr=log,timeout=3600,pass_fds=(lock_fd,))
  if r.returncode:raise RuntimeError('Image build failed; operator evidence: '+str(work/'build.log'))
  images=json.loads((work/'build/receipt.json').read_text())['images'];status['images']={k:v['published_digest'] for k,v in images.items()};status['trials']=[]
  for agent in ['oracle','nop']:
   status['status']='running_'+agent;save();c=json.loads(json.dumps(run_template));run='tbs-universe-linfeng-selftest-'+job[:12]+'-'+agent
   c.update(job_name=run,jobs_dir=str(work/'runs'),n_attempts=1,n_concurrent_trials=1,datasets=[{'path':str(repo),'task_names':[name]}],agents=[{'name':agent,'override_timeout_sec':14400}]);c['retry']={'max_retries':0};c.setdefault('verifier',{}).update(include_logs=[],exclude_logs=[])
   # These are task.toml fields, not Harbor job VerifierConfig fields.
   # Remove historical no-op overrides; actual shared/separate mode and
   # artifact transfer remain exactly as declared by the frozen task.
   c['verifier'].pop('environment_mode',None);c['verifier'].pop('environment',None)
   c['environment']['delete']=True;c['environment']['override_cpus']=4;c['environment']['override_memory_mb']=8192
   # Always use the version-controlled adapter.  The historical default ACK
   # path could load a removed local import and, more importantly, did not
   # preserve the runtime/verifier digest contract.
   c['environment']['import_path']='universe_ack:UniverseACK'
   kw=c['environment']['kwargs'];kw.update(use_sandbox_claim=False,runtime_image=images['official']['published_digest'],verifier_image=images['verifier']['published_digest']);kw.pop('verifier_image_manifest',None)
   spec=kw['pod_overrides']['spec'];spec.update(nodeName='virtual-kubelet-cn-beijing-i',activeDeadlineSeconds=14400);kw['pod_overrides'].setdefault('metadata',{}).setdefault('labels',{})['evaluation-run']=run
   for container in spec['containers']:
    container['image']=images['official']['published_digest']
    for k in ['requests','limits']:container['resources'][k].update(cpu='4',memory='8Gi')
   cfg=work/(agent+'.json');atomic_json(cfg,c);report=work/(agent+'-report.json')
   acs_started_at=time.time()
   with (work/(agent+'.log')).open('w') as log:r=owned_run([str(Path(__file__).with_name('run-acs-evaluation')),str(cfg),str(report)],env=env,stdout=log,stderr=log,timeout=15000,cleanup_grace=150)
   d=json.loads(report.read_text()) if report.exists() else {};trials=[];diagnostics=[]
   if d.get('result_path'):
    for f in Path(d['result_path']).parent.glob('*/result.json'):
     t=json.loads(f.read_text());diagnostics.append(classify_trial(t));trials.append({'rewards':(t.get('verifier_result') or {}).get('rewards'),'exception_type':(t.get('exception_info') or {}).get('exception_type')})
   status['trials'].append({'acs_started_at':acs_started_at,'acs_finished_at':time.time(),'agent':agent,'exit_code':r.returncode,'cleanup_confirmed':d.get('cleanup',{}).get('confirmed',False),'results':trials,
                            'diagnostics':diagnostics});save()
  def valid(t,reward):return t['exit_code']==0 and t['cleanup_confirmed'] and len(t['results'])==1 and all(d['scored'] for d in t['diagnostics']) and not t['results'][0]['exception_type'] and type((t['results'][0]['rewards'] or {}).get('reward')) in (int,float) and (t['results'][0]['rewards'] or {}).get('reward')==reward
  status['status']='selftest_passed' if valid(status['trials'][0],1) and valid(status['trials'][1],0) else 'selftest_failed'
  categories=[item['diagnostic'] for trial in status['trials'] for item in trial.get('diagnostics',[]) if not item['scored']]
  if categories:status['error_class']=categories[0]
  status['note']='Author selftest only; iteration/rerun and independent evaluation still required.'
 except NetworkPreflightError as e:
  from selftest_logs import sanitize
  status.update(status='failed',error_class='task_acs_network_incompatible',stage='network_preflight',
                network_preflight=e.report,error=sanitize(str(e),secrets)[:600],cloud_execution_started=False)
  atomic_json(work/'network-preflight.json',e.report)
 except KeyboardInterrupt:
  status.update(status='interrupted',error='Worker stopped; inspect preserved logs and cloud cleanup.');save();raise
 except Exception as e:
  from selftest_logs import sanitize
  status.update(status='failed',error=sanitize(str(e),secrets)[:600])
 status['completed_at']=time.time()
 save()
def pending_requests(roles,state):
 """Include abandoned work directories so recovery can publish a terminal receipt."""
 from selftest_lifecycle import TERMINAL
 candidates=[]
 for role,entry in roles.items():
  if not re.fullmatch(r'author-[0-9]+',role):continue
  root=Path(entry['root']);inbox=root/'selftest/inbox';results=root/'selftest/results'
  for q in inbox.glob('*.json'):
   if not re.fullmatch('[a-f0-9]{32}',q.stem):continue
   result=results/(q.stem+'.json')
   try:
    if json.loads(result.read_text()).get('status') in TERMINAL:continue
   except (OSError,ValueError,AttributeError):pass
   try:candidates.append((q.stat().st_mtime_ns,role,q))
   except OSError:continue
 return sorted(candidates,key=lambda item:(item[0],item[1],item[2].name))

def run_one(role,entry,q,controller_uid):
 """A separate process owns one author's job and its cloud cleanup."""
 locks=STATE/'locks';locks.mkdir(mode=0o700,parents=True,exist_ok=True)
 with contextlib.ExitStack() as stack:
  for name in (role,q.stem):
   lock=stack.enter_context((locks/(name+'.lock')).open('a'))
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:return 75
  from iteration_lifecycle import scan_clocks
  scan_clocks(dict(controller_uid=controller_uid,roles={role:entry}),state_root=STATE.parent,observation_root=STATE.parent/'command-watchdog/iteration-observations')
  process(role,entry,q)
  work=STATE/q.stem
  if (work/'status.json').exists():
   from selftest_handoff import reconcile
   from deployment_paths import universe_root
   reconcile(work,STATE,entry,universe_root(W))
 return 0

def role_locked(role):
 """Do not repeatedly spawn workers while a surviving child owns this role."""
 path=STATE/'locks'/(role+'.lock')
 if not path.exists():return False
 fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
 try:
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return True
  fcntl.flock(fd,fcntl.LOCK_UN)
  return False
 finally:os.close(fd)

def publish_worker_interruption(role,entry,q,error_type):
 """Make child crashes actionable without pretending cloud cleanup occurred."""
 root=Path(entry['root']);result=root/'selftest/results'/(q.stem+'.json');work=STATE/q.stem
 def publish(payload):
  atomic_json(result,payload);os.chown(result,0,entry['gid']);result.chmod(0o640)
 try:existing=json.loads(result.read_text())
 except (OSError,ValueError):existing={}
 from selftest_lifecycle import TERMINAL
 if existing.get('status') in TERMINAL:
  if existing.get('status')=='selftest_passed':
   existing['review_handoff']={'state':'waiting_platform','stage':'handoff_worker_failed',
      'reason':'Selftest completed, but platform handoff failed; preserve this result and request operator repair.',
      'error_type':error_type,'scientific_acceptance':False}
   publish(existing)
  return
 if recover_interrupted(work,result,publish):return
 publish({'job_id':q.stem,'role':role,'status':'interrupted','quality_acceptance':False,
          'error':'Worker stopped before a terminal receipt; cloud cleanup must be verified.',
          'error_type':error_type})

def schedule(pool,candidates,broker_config,command_for):
 """Fill only vacant slots, and never run two jobs for the same author."""
 pool.reap()
 active_roles={key[0] for key in pool.children}
 for _,role,q in candidates:
  if len(pool.children)>=pool.limit:break
  if role in active_roles or role_locked(role):continue
  key=(role,q.stem)
  if pool.start(key,command_for(role,q)):
   active_roles.add(role)

def main():
 global STATE
 p=__import__('argparse').ArgumentParser();p.add_argument('--broker-config',type=Path,default=W/'.state/science-role-broker/config.json');p.add_argument('--state',type=Path,default=STATE)
 # Ten authors share this pool. All ten can overlap ACS execution while the
 # separate build slot keeps the local Docker daemon's workload bounded.
 p.add_argument('--workers',type=int,default=int(os.environ.get('GEWU_SELFTEST_WORKERS','10')))
 p.add_argument('--single-request',type=Path,help=__import__('argparse').SUPPRESS);p.add_argument('--role',help=__import__('argparse').SUPPRESS)
 args=p.parse_args();STATE=args.state
 if not 1<=args.workers<=15:raise SystemExit('workers must be between 1 and 15')
 STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
 broker_config=json.loads(args.broker_config.read_text());roles=broker_config['roles']
 if args.single_request is not None:
  if args.role not in roles or not re.fullmatch(r'author-[0-9]+',args.role):raise SystemExit('Invalid author')
  q=args.single_request;inbox=Path(roles[args.role]['root'])/'selftest/inbox'
  if q.parent.resolve()!=inbox.resolve() or q.suffix!='.json' or not re.fullmatch('[a-f0-9]{32}',q.stem):raise SystemExit('Request outside author inbox')
  install_stop_handlers()
  try:return run_one(args.role,roles[args.role],q,broker_config['controller_uid'])
  except KeyboardInterrupt:
   publish_worker_interruption(args.role,roles[args.role],q,'KeyboardInterrupt')
   return 130
  except Exception as error:
   publish_worker_interruption(args.role,roles[args.role],q,type(error).__name__)
   return 1
 from queue_parallel import ActiveJobs
 with (STATE/'worker.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  pool=ActiveJobs(args.workers);stopping=False
  def stop(_signum,_frame):
   nonlocal stopping
   stopping=True
  previous=signal.signal(signal.SIGTERM,stop)
  previous_int=signal.signal(signal.SIGINT,stop)
  def command_for(role,q):
   return [sys.executable,str(Path(__file__).resolve()),'--broker-config',str(args.broker_config.resolve()),'--state',str(STATE.resolve()),'--workers',str(args.workers),'--single-request',str(q),'--role',role]
  try:
   while not stopping:
    broker_config=json.loads(args.broker_config.read_text());roles=broker_config['roles']
    schedule(pool,pending_requests(roles,STATE),broker_config,command_for)
    time.sleep(2)
  finally:
   pool.stop()
   signal.signal(signal.SIGTERM,previous);signal.signal(signal.SIGINT,previous_int)
 return 0
if __name__=='__main__':raise SystemExit(main())
