#!/usr/local/bin/python3
"""Five independent queue jobs for one immutable exclusive assignment; no keys."""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile


def read_assignment(path, catalog):
    assignment=json.loads(Path(path).read_text())
    identifier=assignment.get('evaluation_id');snapshot=assignment.get('snapshot_sha256')
    if (not isinstance(identifier,str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',identifier)
            or not isinstance(snapshot,str) or not re.fullmatch('[a-f0-9]{64}',snapshot)
            or assignment.get('state')!='reviewing' or not isinstance(assignment.get('key'),str)
            or type(assignment.get('generation')) is not int):
        raise ValueError('Require current version-bound exclusive assignment')
    entries=json.loads(Path(catalog).read_text())['evaluations']
    if sum(e.get('evaluation_id')==identifier and e.get('snapshot_sha256')==snapshot for e in entries)!=1:
        raise ValueError('Assigned execution not uniquely present in catalog')
    return assignment


def requests(assignment):
    identity={k:assignment[k] for k in ('key','generation','evaluation_id','snapshot_sha256')}
    seed=json.dumps(identity,sort_keys=True).encode()
    return [dict(job_id=hashlib.sha256(seed+b':'+str(i).encode()).hexdigest()[:32],
                 evaluation_id=identity['evaluation_id'],snapshot_sha256=identity['snapshot_sha256']) for i in range(5)]


def submit(assignment, inbox):
    inbox=Path(inbox)
    # Role-specific inbox serializes concurrent invocations and partial retries.
    with (inbox/'.batch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        batch=requests(assignment)
        for request in batch:
            path=inbox/(request['job_id']+'.json')
            if path.exists():
                if path.is_symlink() or json.loads(path.read_text())!=request:
                    raise ValueError('Existing batch request identity mismatch')
                continue
            pending=inbox/(request['job_id']+'.batch-pending')
            # A stopped writer may leave this private staging file; reuse only
            # after the same identity is checked, never overwrite another job.
            if pending.exists():
                if pending.is_symlink() or json.loads(pending.read_text())!=request:
                    raise ValueError('Pending request identity mismatch')
            else:
                fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as stream:
                    json.dump(request,stream);stream.flush();os.fsync(stream.fileno())
            pending.replace(path)
    return {'jobs':batch,'count':5,'quality_acceptance':False}


def summary(assignment, results):
    rows=[];valid=[];failures=[]
    for request in requests(assignment):
        path=Path(results)/(request['job_id']+'.json')
        if not path.exists():
            rows.append(dict(job_id=request['job_id'],status='pending'));continue
        if path.is_symlink():raise ValueError('Result symlink refused')
        result=json.loads(path.read_text())
        if any(result.get(k)!=v for k,v in request.items()):raise ValueError('Result belongs to another version')
        reward=result.get('reward')
        eligible=(result.get('status')=='solver_complete' and result.get('cleanup_confirmed') is True
                  and result.get('n_errors')==0 and type(reward) in (float,int)
                  and math.isfinite(reward) and reward in (0,1))
        if eligible:valid.append(reward)
        else:failures.append(request['job_id'])
        rows.append(dict(job_id=request['job_id'],status=result.get('status'),valid=eligible,
                         reward=reward if eligible else None,cleanup_confirmed=result.get('cleanup_confirmed') is True))
    complete=len(valid)==5
    return dict(jobs=rows,valid_trials=len(valid),invalid_jobs=failures,complete=complete,
                pass_rate=sum(valid)/5 if complete else None,
                difficulty_threshold_met=sum(valid)<=3 if complete else None,
                difficulty_strict_reject=sum(valid)>=4 if complete else None,
                quality_acceptance=False,
                note='Timeouts and infrastructure failures require diagnosis; no automatic acceptance or replacement jobs.')


def install_clients(config):
    if os.geteuid()!=0:raise PermissionError('Operator required')
    for role,entry in config['roles'].items():
        if not re.fullmatch(r'evaluator-[0-9]+',role):continue
        root=Path(entry['root']).resolve(strict=True)
        workspace=Path('/mnt/data/zhanglinfeng/workspace').resolve()
        local_runtime=str(root).startswith('/tmp/gewu-terminal-bench-science-runtime-')
        directory=root/'usr/local/bin'
        if (not root.is_relative_to(workspace) and not local_runtime) or directory.resolve()!=directory:
            raise ValueError('Unsafe role client destination')
        fd,name=tempfile.mkstemp(prefix='.role-evaluation-batch-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(Path(__file__).read_bytes())
            os.chmod(name,0o755)
            os.replace(name,directory/'role-evaluation-batch')
        finally:
            Path(name).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('submit','status'))
    args=parser.parse_args()
    assignment=read_assignment('/home/role/review-assignment.json','/evaluation/catalog.json')
    result=submit(assignment,'/evaluation/inbox') if args.action=='submit' else summary(assignment,'/evaluation/results')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
