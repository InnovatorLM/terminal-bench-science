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
    batch_generation=assignment.get('batch_generation',assignment['generation'])
    if type(batch_generation) is not int or not 0 <= batch_generation <= assignment['generation']:
        raise ValueError('Invalid original batch generation')
    entries=json.loads(Path(catalog).read_text())['evaluations']
    if sum(e.get('evaluation_id')==identifier and e.get('snapshot_sha256')==snapshot for e in entries)!=1:
        raise ValueError('Assigned execution not uniquely present in catalog')
    return assignment


def requests(assignment):
    identity={k:assignment[k] for k in ('key','evaluation_id','snapshot_sha256')}
    identity['generation']=assignment.get('batch_generation',assignment['generation'])
    seed=json.dumps(identity,sort_keys=True).encode()
    identifiers=assignment.get('batch_job_ids')
    if identifiers is None:
        identifiers=[hashlib.sha256(seed+b':'+str(i).encode()).hexdigest()[:32] for i in range(5)]
    if (not isinstance(identifiers,list) or len(identifiers)!=5
            or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{32}',v) for v in identifiers)
            or len(set(identifiers))!=5):
        raise ValueError('Require five distinct version-bound batch jobs')
    return [dict(job_id=identifier,evaluation_id=identity['evaluation_id'],
                 snapshot_sha256=identity['snapshot_sha256']) for identifier in identifiers]


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


def summary(assignment, results, inbox=None):
    rows=[];valid=[];failures=[];not_submitted=[]
    from rollout_evidence_index import entry as evidence_entry
    evidence_index=[]
    inbox=Path(inbox) if inbox is not None else Path(results).parent/'inbox'
    for request in requests(assignment):
        path=Path(results)/(request['job_id']+'.json')
        if not path.exists():
            queued=inbox/(request['job_id']+'.json')
            submitted=queued.is_file() and not queued.is_symlink()
            if submitted and json.loads(queued.read_text())!=request:
                raise ValueError('Queued request belongs to another version')
            if not submitted:not_submitted.append(request['job_id'])
            rows.append(dict(job_id=request['job_id'],status='pending' if submitted else 'not_submitted'))
            evidence_index.append(evidence_entry(request,None,results));continue
        if path.is_symlink():raise ValueError('Result symlink refused')
        result=json.loads(path.read_text())
        if any(result.get(k)!=v for k,v in request.items()):raise ValueError('Result belongs to another version')
        evidence_index.append(evidence_entry(request,result,results))
        reward=result.get('reward')
        from timeout_review import judgment
        reviewed=judgment(assignment,request,results)
        eligible=(result.get('status')=='solver_complete' and result.get('cleanup_confirmed') is True
                  and result.get('evidence_complete') is True and result.get('source_version_bound') is True
                  and result.get('n_errors')==0 and type(reward) in (float,int)
                  and math.isfinite(reward) and reward in (0,1))
        if reviewed:
            eligible=reviewed['count_as_model_failure']
            reward=reviewed['scoring_reward']
        if eligible:valid.append(reward)
        else:failures.append(request['job_id'])
        rows.append(dict(job_id=request['job_id'],run_id=result.get('run_id') or evidence_index[-1]['run_id'],
                         status=result.get('status'),valid=eligible,
                         reward=reward if eligible else None,cleanup_confirmed=result.get('cleanup_confirmed') is True,
                         evidence_status=result.get('evidence_status','incomplete_evidence'),
                         missing_evidence=result.get('missing_evidence',[]),
                         raw_reward=result.get('reward'),timeout_review=reviewed,
                         score_source='timeout_review' if reviewed and eligible else 'verifier' if eligible else None))
    # Multiple job files must not make one execution appear independent.
    # Old receipts without run IDs remain subject to final evidence validation.
    runs={}
    for row in rows:
        if row.get('run_id'):runs.setdefault(row['run_id'],[]).append(row)
    duplicates={row['job_id'] for group in runs.values() if len(group)>1 for row in group}
    for row in rows:
        if row['job_id'] in duplicates:
            row.update(valid=False,reward=None,score_source=None,evidence_status='duplicate_run_identity')
    for item in evidence_index:
        if item['job_id'] in duplicates:
            item.update(packet_index_verified=False,execution_identity_verified=False)
            item['evidence_errors'].append('duplicate_run_identity')
    valid=[row['reward'] for row in rows if row.get('valid')]
    failures=[row['job_id'] for row in rows if row.get('valid') is False]
    complete=len(valid)==5
    return dict(jobs=rows,evidence_index=evidence_index,
                independent_runs_verified=len(runs)==5 and all(item['execution_identity_verified'] for item in evidence_index),
                evidence_index_scope='Published manifest/binding identity and file presence/size; final acceptance also hashes file contents.',
                valid_trials=len(valid),invalid_jobs=failures,complete=complete,
                not_submitted_jobs=not_submitted,batch_submitted=not not_submitted,
                next_action='Run role-evaluation-batch submit once; status does not enqueue jobs.' if not_submitted else
                            'Wait for existing jobs or diagnose terminal results; do not create another batch.',
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
        for source,target in [('timeout_review.py','timeout_review.py'),
                              ('rollout_evidence_index.py','rollout_evidence_index.py'),
                              ('role_evaluation_batch.py','role_evaluation_batch.py'),
                              ('role_review_status.py','role-review-status')]:
            fd,name=tempfile.mkstemp(prefix='.review-client-',dir=directory)
            try:
                with os.fdopen(fd,'wb') as stream:stream.write(Path(__file__).with_name(source).read_bytes())
                os.chmod(name,0o755 if target=='role-review-status' else 0o644)
                os.replace(name,directory/target)
            finally:Path(name).unlink(missing_ok=True)
        reviews=root/'evaluation/timeout-reviews'
        if reviews.parent.exists():
            if reviews.is_symlink():raise ValueError('Unsafe timeout review destination')
            reviews.mkdir(mode=0o700,exist_ok=True)
            os.chown(reviews,entry['uid'],entry.get('gid',entry['uid']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('submit','status','review-timeout'))
    parser.add_argument('--job')
    parser.add_argument('--assessment',type=Path)
    args=parser.parse_args()
    assignment=read_assignment('/home/role/review-assignment.json','/evaluation/catalog.json')
    if args.action=='review-timeout':
        from timeout_review import record
        selected=[r for r in requests(assignment) if r['job_id']==args.job]
        if len(selected)!=1 or args.assessment is None:raise ValueError('Specify a job in this batch and --assessment JSON')
        path=record(assignment,selected[0],'/evaluation/results',json.loads(args.assessment.read_text()))
        result=dict(record=str(path),summary=summary(assignment,'/evaluation/results'))
    else:result=submit(assignment,'/evaluation/inbox') if args.action=='submit' else summary(assignment,'/evaluation/results')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
