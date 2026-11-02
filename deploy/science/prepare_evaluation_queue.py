"""One-time operator bootstrap of the frozen calibration infrastructure pilot.

Does not accept or publish a scientific task. Refuses to overwrite an existing
queue configuration; future task registrations need separate reviewed IDs.
"""
import hashlib,importlib.util,json,os,shutil
from pathlib import Path
w=Path('/mnt/data/zhanglinfeng/workspace');source=w/'gewu-multiverse/deploy/science/evaluation_queue.py';spec=importlib.util.spec_from_file_location('evaluation_queue',source);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
if os.geteuid()!=0:raise SystemExit('Run as operator/root')
if (w/'.config/science-evaluation/queue.json').exists():raise SystemExit('Queue already prepared; existing immutable registry unchanged')
base=w/'.state/science-evaluation-queue';base.mkdir(mode=0o700,exist_ok=True)
settings=w/'.config/science-evaluation';settings.mkdir(mode=0o700,exist_ok=True);settings.chmod(0o700)
snapshot=base/'registered/calibration-v1/robust-sensor-calibration';snapshot.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
original=w/'universes/my-terminal-bench-science/workspaces/author/candidates/robust-sensor-calibration'
if not snapshot.exists():shutil.copytree(original,snapshot,symlinks=True)
hash_value=m.digest_tree(snapshot)
template=json.loads((w/'.state/harbor-eval/candidate-live.json').read_text());template['datasets']=[{'path':str(snapshot.parent),'task_names':[snapshot.name]}]
template_path=settings/'calibration-v1-template.json';template_path.write_text(json.dumps(template,indent=2));template_path.chmod(0o600)
registry={'calibration-v1':{'template':str(template_path),'template_sha256':hashlib.sha256(template_path.read_bytes()).hexdigest(),'snapshot':str(snapshot),'snapshot_sha256':hash_value}}
registry_path=settings/'registry.json';registry_path.write_text(json.dumps(registry,indent=2));registry_path.chmod(0o600)
role_root=w/'universes/my-terminal-bench-science/.config/gewu-multiverse/agents/evaluator'
queue=role_root/'evaluation';queue.mkdir(exist_ok=True,mode=0o755);os.chown(queue,0,0);queue.chmod(0o755)
for name,uid,mode in [('inbox',21002,0o700),('results',0,0o755)]:
 p=queue/name;p.mkdir(exist_ok=True,mode=mode);os.chown(p,uid,uid);p.chmod(mode)
config={'inbox':str(queue/'inbox'),'results':str(queue/'results'),'state':str(base),'registry':str(registry_path),'broker_config':str(w/'.state/science-role-broker/config.json'),'evaluator_uid':21002,'max_jobs_per_day':3}
config_path=settings/'queue.json';config_path.write_text(json.dumps(config,indent=2));config_path.chmod(0o600)
# Public descriptor contains IDs/hashes only, no host paths or private material.
(queue/'catalog.json').write_text(json.dumps({'evaluations':[{'evaluation_id':'calibration-v1','snapshot_sha256':hash_value,'purpose':'infrastructure pilot; not a scientific acceptance'}]},indent=2));(queue/'catalog.json').chmod(0o644)
print(json.dumps({'registered_evaluation_id':'calibration-v1','snapshot_sha256':hash_value,'key_staged_to_role':False}))
