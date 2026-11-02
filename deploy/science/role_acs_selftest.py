#!/usr/local/bin/python3
"""Author-local selftest and metadata-only review client; no cloud credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import uuid

IDENTITY = ('job_id', 'idea_id', 'source_repository', 'source_task_path',
            'source_commit', 'snapshot_sha256', 'execution_commit', 'execution_task_path')


def parser():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    s=sub.add_parser('submit');s.add_argument('--task',type=Path,required=True);s.add_argument('--commit',required=True)
    s.add_argument('--idea',help='Stable idea ID recorded before starting this task')
    s.add_argument('--source-repository',type=Path,help='Workspace Git repository when uploading a staged task')
    s.add_argument('--source-task-path',help='Committed task path for staged uploads; worker verifies all bytes and modes')
    s.add_argument('--request-review',action='store_true',help='Request automatic handoff after verified iteration and budget checks')
    s.add_argument('--initial-review',action='store_true',help=argparse.SUPPRESS)
    s.add_argument('--previous-job',help='Prior completed selftest used for result-driven iteration')
    s.add_argument('--iteration-note',help='What changed in response to that execution')
    s=sub.add_parser('request-review',help='Request review of an existing successful job without rebuilding or rerunning ACS')
    s.add_argument('job_id');s.add_argument('--previous-job',required=True);s.add_argument('--iteration-note',required=True)
    s=sub.add_parser('status');s.add_argument('job_id')
    return p


def check_iteration(previous, note):
    if bool(previous)!=bool(note):raise SystemExit('Use --previous-job and --iteration-note together')
    if previous and (not re.fullmatch('[a-f0-9]{32}',previous) or not 20<=len(note.strip())<=4000):
        raise SystemExit('Invalid previous job or iteration note (20-4000 characters)')


def request_review(args,base):
    """Bind every source field to a visible result, never accept replacement IDs."""
    if not re.fullmatch('[a-f0-9]{32}',args.job_id):raise SystemExit('Invalid job ID')
    check_iteration(args.previous_job,args.iteration_note)
    if args.previous_job==args.job_id:raise SystemExit('Previous job must be a different earlier execution')
    try:status=json.loads((base/'results'/(args.job_id+'.json')).read_text())
    except (OSError,ValueError):raise SystemExit('Successful selftest result is unavailable or incomplete') from None
    if (status.get('schema_version')!=2 or status.get('job_id')!=args.job_id
            or status.get('status')!='selftest_passed' or status.get('source_git_tree_verified') is not True
            or status.get('quality_acceptance') is not False):
        raise SystemExit('Require a successful source-verified selftest result')
    if not isinstance(status.get('idea_id'),str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,95}',status['idea_id']):
        raise SystemExit('The original job must already bind a stable idea; cannot add one retroactively')
    if any(not isinstance(status.get(key),str) or not status[key] for key in IDENTITY):
        raise SystemExit('The original successful result is missing a frozen source identity')
    request=dict(schema_version=1,**{key:status[key] for key in IDENTITY},
                 iteration=dict(previous_job=args.previous_job,note=args.iteration_note.strip()))
    raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    identifier=hashlib.sha256(raw).hexdigest();destination=base/'inbox'/('review-'+identifier+'.json')
    # Publish only complete input. Concurrent identical invocations are idempotent.
    fd,name=tempfile.mkstemp(prefix='.review-',suffix='.pending',dir=base/'inbox')
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
        try:os.link(name,destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes()!=raw:
                raise SystemExit('Existing review request is not the same immutable input')
    finally:Path(name).unlink(missing_ok=True)
    print(json.dumps(dict(request_id=identifier,job_id=args.job_id,status='review_request_queued',
                          result=str(base/'results'/('review-'+identifier+'.json')),cloud_execution_started=False)))
    return identifier


def submit(a,base):
    if a.initial_review:raise SystemExit('--initial-review was removed: run a first self-test, inspect its result, commit improvements, then submit with --previous-job, --iteration-note and --request-review')
    if a.idea and not re.fullmatch('[a-z0-9][a-z0-9-]{0,95}',a.idea):raise SystemExit('Invalid stable idea ID')
    if a.request_review and not a.idea:raise SystemExit('Review handoff requires --idea')
    if a.request_review and not a.previous_job:raise SystemExit('Review requires --previous-job and --iteration-note after result-driven iteration')
    check_iteration(a.previous_job,a.iteration_note)
    if not re.fullmatch('[a-f0-9]{40}',a.commit):raise SystemExit('Use full source commit')
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
    if a.previous_job:request['iteration']={'previous_job':a.previous_job,'note':a.iteration_note.strip()}
    pending=base/'inbox'/(job+'.pending');pending.write_text(json.dumps(request));pending.replace(pending.with_suffix('.json'));print(json.dumps(request))


def main(argv=None,base=Path('/selftest')):
    a=parser().parse_args(argv);base=Path(base)
    if a.action=='status':
        if not re.fullmatch('(?:[a-f0-9]{32}|review-[a-f0-9]{64})',a.job_id):raise SystemExit('Invalid job/request ID')
        f=base/'results'/(a.job_id+'.json');print(f.read_text() if f.exists() else json.dumps({'status':'queued','job_id':a.job_id}))
    elif a.action=='request-review':request_review(a,base)
    else:submit(a,base)
    return 0


if __name__=='__main__':raise SystemExit(main())
