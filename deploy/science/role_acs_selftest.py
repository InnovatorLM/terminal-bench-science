#!/usr/local/bin/python3
"""Author-local submission client; no cloud credentials."""
import argparse,json,uuid,tarfile,hashlib,subprocess
from pathlib import Path
p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
s=sub.add_parser('submit');s.add_argument('--task',type=Path,required=True);s.add_argument('--commit',required=True)
s.add_argument('--idea',help='Stable idea ID recorded before starting this task')
s.add_argument('--source-repository',type=Path,help='Workspace Git repository when uploading a staged task')
s.add_argument('--source-task-path',help='Committed task path for staged uploads; worker verifies all bytes and modes')
s.add_argument('--request-review',action='store_true',help='Request automatic handoff after verified iteration and budget checks')
s.add_argument('--initial-review',action='store_true',help='Request first independent review after the initial selftest')
s.add_argument('--previous-job',help='Prior completed selftest used for result-driven iteration')
s.add_argument('--iteration-note',help='What changed in response to that execution')
s=sub.add_parser('status');s.add_argument('job_id');a=p.parse_args();base=Path('/selftest')
if a.action=='status':
 if not __import__('re').fullmatch('[a-f0-9]{32}',a.job_id):raise SystemExit('Invalid job ID')
 f=base/'results'/(a.job_id+'.json');print(f.read_text() if f.exists() else json.dumps({'status':'queued','job_id':a.job_id}))
else:
 if a.idea and not __import__('re').fullmatch('[a-z0-9][a-z0-9-]{0,95}',a.idea):raise SystemExit('Invalid stable idea ID')
 if a.request_review and not a.idea:raise SystemExit('Review handoff requires --idea')
 if a.request_review and not a.initial_review and not a.previous_job:raise SystemExit('Non-initial review requires linked result-driven iteration')
 if bool(a.previous_job)!=bool(a.iteration_note):raise SystemExit('Use --previous-job and --iteration-note together')
 if a.previous_job and (not __import__('re').fullmatch('[a-f0-9]{32}',a.previous_job) or not 20<=len(a.iteration_note.strip())<=4000):raise SystemExit('Invalid previous job or iteration note (20-4000 characters)')
 if not __import__('re').fullmatch('[a-f0-9]{40}',a.commit):raise SystemExit('Use full source commit')
 task=a.task.resolve(strict=True)
 if bool(a.source_repository)!=bool(a.source_task_path):raise SystemExit('Use --source-repository and --source-task-path together')
 if not (task.is_relative_to(Path('/workspace')) or (a.source_repository and task.is_relative_to(Path('/tmp')))):raise SystemExit('Task must be in workspace or explicitly source-bound staging')
 repository=Path(subprocess.check_output(['git','-C',str(a.source_repository or task),'rev-parse','--show-toplevel'],text=True).strip()).resolve(strict=True)
 if not repository.is_relative_to(Path('/workspace')):raise SystemExit('Source repository must be in workspace')
 task_path=a.source_task_path if a.source_repository else task.relative_to(repository).as_posix()
 if Path(task_path).is_absolute() or '..' in Path(task_path).parts:raise SystemExit('Source task path must be relative')
 if task_path=='.':raise SystemExit('Task must be a subdirectory of its source repository')
 files=list(task.rglob('*'));total=0
 for f in files:
  if f.is_symlink() or not(f.is_file() or f.is_dir()):raise SystemExit('Links and special files refused')
  if f.is_file():total+=f.stat().st_size
 if total>128*1024*1024 or len(files)>10000:raise SystemExit('Task exceeds limit')
 job=uuid.uuid4().hex;archive=base/'inbox'/(job+'.tar')
 with tarfile.open(archive,'w') as t:
  for f in files:t.add(f,arcname=f.relative_to(task),recursive=False)
 request={'job_id':job,'task_name':task.name,'source_commit':a.commit,'source_repository':repository.relative_to('/workspace').as_posix(),'source_task_path':task_path,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
 if a.idea:request['idea_id']=a.idea
 if a.request_review:request['request_review']=True
 if a.initial_review:
  if not a.request_review:raise SystemExit('--initial-review requires --request-review')
  request['initial_review']=True
 if a.previous_job:request['iteration']={'previous_job':a.previous_job,'note':a.iteration_note.strip()}
 pending=base/'inbox'/(job+'.pending');pending.write_text(json.dumps(request));pending.replace(pending.with_suffix('.json'));print(json.dumps(request))
