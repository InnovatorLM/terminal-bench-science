"""Operator-owned author selftest queue: frozen upload -> ACR -> ACS oracle/nop."""
import json,os,re,subprocess,tarfile,hashlib,time,fcntl,tomllib
from pathlib import Path,PurePosixPath
from evaluation_queue import W,atomic_json,digest_tree
from check_harbor_task import inspect_task
from selftest_lifecycle import owned_run, recover_interrupted, install_stop_handlers
from build_task_images import validate_dockerfile
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
  if q.get('request_review') and not(q.get('idea_id') and q.get('iteration')):raise ValueError('Review request requires idea and iteration linkage')
  atomic_json(work/'request.json',q)
  if q['job_id']!=job or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,90}',name) or not re.fullmatch('[a-f0-9]{40}',q['source_commit']):raise ValueError('Invalid request identity')
  frozen=work/'upload.tar';snapshot_upload(archive,entry['uid'],140*1024*1024,frozen)
  if hashlib.sha256(frozen.read_bytes()).hexdigest()!=q['archive_sha256']:raise ValueError('Upload changed')
  repo=work/'source';task=repo/name;unpack(frozen,task);atomic_json(work/'mechanical.json',inspect_task(task))
  status.update(verify_source(root/'workspace',q['source_repository'],q['source_task_path'],q['source_commit'],task));save()
  for args in [['init','-q'],['config','user.email','operator@localhost'],['config','user.name','operator'],['add','.'],['commit','-qm','Frozen author upload']]:subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
  commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip();status.update(status='building',source_commit=q['source_commit'],execution_commit=commit,execution_task_path=name,snapshot_sha256=digest_tree(task));save()
  if 'iteration' in q:
   from selftest_iteration import validate
   status['iteration']=validate(q['iteration'],status,STATE,role);save()
  task_tmp=work/'tmp';task_home=work/'home';task_tmp.mkdir(mode=0o700,exist_ok=True);task_home.mkdir(mode=0o700,exist_ok=True)
  env=os.environ.copy();env.update(TMPDIR=str(task_tmp),PYTHONDONTWRITEBYTECODE='1',HOME=str(task_home))
  # Explicit deployment transport for public Docker official images only.
  # pin_bases retains the original reference and resolved immutable digest.
  env.setdefault('GEWU_DOCKER_LIBRARY_MIRROR','public.ecr.aws/docker/library')
  with (work/'build.log').open('w') as log:
   r=owned_run(build_command(repo,commit,name,work/'build'),env=env,stdout=log,stderr=log,timeout=3600)
  if r.returncode:raise RuntimeError('Image build failed; operator evidence: '+str(work/'build.log'))
  images=json.loads((work/'build/receipt.json').read_text())['images'];status['images']={k:v['published_digest'] for k,v in images.items()};status['trials']=[]
  for agent in ['oracle','nop']:
   status['status']='running_'+agent;save();c=json.loads((W/'.config/harbor-api/template.json').read_text());run='tbs-universe-linfeng-selftest-'+job[:12]+'-'+agent
   c.update(job_name=run,jobs_dir=str(work/'runs'),n_attempts=1,n_concurrent_trials=1,datasets=[{'path':str(repo),'task_names':[name]}],agents=[{'name':agent,'override_timeout_sec':14400}]);c['retry']={'max_retries':0};c.setdefault('verifier',{}).update(include_logs=[],exclude_logs=[])
   # ACK/ACS cannot change a container's network policy after start. Always
   # isolate verifier startup, and copy any explicit task verifier policy into
   # that environment so Harbor has no phase switch to perform.
   task_cfg=tomllib.loads((task/'task.toml').read_text())
   task_verifier=task_cfg.get('verifier') or {}
   c['verifier']['environment_mode']='separate'
   verifier_env=dict(task_verifier.get('environment') or {})
   if 'network_mode' in task_verifier:
    verifier_env['network_mode']=task_verifier['network_mode']
   if 'allow_internet' in task_verifier:
    verifier_env['allow_internet']=task_verifier['allow_internet']
   c['verifier']['environment']=verifier_env
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
   with (work/(agent+'.log')).open('w') as log:r=owned_run([str(Path(__file__).with_name('run-acs-evaluation')),str(cfg),str(report)],env=env,stdout=log,stderr=log,timeout=15000)
   d=json.loads(report.read_text()) if report.exists() else {};trials=[];diagnostics=[]
   if d.get('result_path'):
    for f in Path(d['result_path']).parent.glob('*/result.json'):
     t=json.loads(f.read_text());diagnostics.append(classify_trial(t));trials.append({'rewards':(t.get('verifier_result') or {}).get('rewards'),'exception_type':(t.get('exception_info') or {}).get('exception_type')})
   status['trials'].append({'acs_started_at':acs_started_at,'acs_finished_at':time.time(),'agent':agent,'exit_code':r.returncode,'cleanup_confirmed':d.get('cleanup',{}).get('confirmed',False),'results':trials,
                            'diagnostics':diagnostics});save()
  def valid(t,reward):return t['exit_code']==0 and t['cleanup_confirmed'] and len(t['results'])==1 and not t['results'][0]['exception_type'] and (t['results'][0]['rewards'] or {}).get('reward')==reward
  status['status']='selftest_passed' if valid(status['trials'][0],1) and valid(status['trials'][1],0) else 'selftest_failed'
  categories=[item['diagnostic'] for trial in status['trials'] for item in trial.get('diagnostics',[]) if not item['scored']]
  if categories:status['error_class']=categories[0]
  status['note']='Author selftest only; iteration/rerun and independent evaluation still required.'
 except KeyboardInterrupt:
  status.update(status='interrupted',error='Worker stopped; inspect preserved logs and cloud cleanup.');save();raise
 except Exception as e:
  from selftest_logs import sanitize
  status.update(status='failed',error=sanitize(str(e),secrets)[:600])
 status['completed_at']=time.time()
 save()
def main():
 global STATE
 p=__import__('argparse').ArgumentParser();p.add_argument('--broker-config',type=Path,default=W/'.state/science-role-broker/config.json');p.add_argument('--state',type=Path,default=STATE);args=p.parse_args()
 STATE=args.state
 install_stop_handlers()
 STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
 with (STATE/'worker.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  while True:
   broker_config=json.loads(args.broker_config.read_text());roles=broker_config['roles']
   # Select one newest pending submission globally.  Iterating authors in
   # config order lets one long image build starve every later author.
   candidates=[]
   for role,entry in roles.items():
    if not role.startswith('author-'): continue
    inbox=Path(entry['root'])/'selftest/inbox'
    for q in inbox.glob('*.json'):
     if re.fullmatch('[a-f0-9]{32}',q.stem) and not (STATE/q.stem).exists():
      candidates.append((q.stat().st_mtime,role,entry,q))
   if candidates:
    _,role,entry,q=max(candidates,key=lambda item:item[0])
    from iteration_lifecycle import scan_clocks
    scan_clocks(dict(controller_uid=broker_config['controller_uid'],roles={role:entry}),state_root=STATE.parent,observation_root=STATE.parent/'command-watchdog/iteration-observations')
    process(role,entry,q)
    work=STATE/q.stem
    if (work/'status.json').exists():
     from selftest_handoff import reconcile
     from deployment_paths import universe_root
     reconcile(work,STATE,entry,universe_root(W))
   time.sleep(10)
if __name__=='__main__':main()
