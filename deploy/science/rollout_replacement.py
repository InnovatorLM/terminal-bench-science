"""Audited replacement of invalid platform runs; never retry valid model failures.

Only the operator records a plan. The single review dispatcher consumes it and
submits the replacement jobs without changing the frozen task or stealing an
evaluator from another review. Original jobs and evidence are never rewritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time

from evaluation_queue import active_queue_worker, root_file
from role_evaluation_batch import requests
from selftest_review_supplement import immutable_json, operator_lock

IDENTITY = ('key', 'commit', 'task', 'evaluation_id', 'snapshot_sha256', 'role')
CLASSIFICATION = 'platform_pod_deadline_preempts_collection'
RUNTIME_TRUST = 'platform_runtime_ca_missing'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def directory(state, key):
    return Path(state)/'rollout-replacements'/digest(key.encode())


def evidence(path):
    path=root_file(path)
    return dict(path=str(path),sha256=digest(path.read_bytes()))


def load_checked(path):
    raw=root_file(path).read_bytes()
    value=json.loads(raw)
    for item in value['evidence_files']:
        if evidence(item['path'])!=item:
            raise ValueError('Replacement evidence changed')
    return value,digest(raw)


def plans(state, assignment):
    """Replay an append-only per-slot chain, independent of review generation."""
    base=dict(assignment)
    base.pop('batch_job_ids',None)
    batch=requests(base)
    history=[]
    for index,path in enumerate(sorted(directory(state,assignment['key']).glob('[0-9]*.json')),1):
        plan,sha=load_checked(path)
        if (path.name!=f'{index:04d}.json' or plan.get('schema_version')!=1
                or any(plan.get(k)!=assignment.get(k) for k in IDENTITY)
                or plan.get('batch_generation')!=assignment.get('batch_generation',assignment['generation'])
                or plan.get('previous_jobs')!=batch or not plan.get('replacements')):
            raise ValueError('Replacement plan belongs to another batch or version')
        seen=set()
        for row in plan['replacements']:
            slot=row['slot'];new=row['request']
            if (type(slot) is not int or not 0<=slot<5 or slot in seen
                    or row['original_job_id']!=batch[slot]['job_id']
                    or set(new)!={'job_id','evaluation_id','snapshot_sha256'}
                    or new['evaluation_id']!=assignment['evaluation_id']
                    or new['snapshot_sha256']!=assignment['snapshot_sha256']
                    or not re.fullmatch('[a-f0-9]{32}',str(new['job_id']))
                    or new['job_id'] in {r['job_id'] for r in batch}):
                raise ValueError('Invalid replacement slot or execution binding')
            seen.add(slot);batch[slot]=new
        history.append(dict(path=path,sha256=sha,plan=plan))
    return batch,history

def repair_evidence(path, classification):
    smoke=json.loads(root_file(path).read_text())
    files=[evidence(path)]
    if classification==CLASSIFICATION:
        if (smoke.get('mode')!='live-acs-evidence-transport-smoke'
                or smoke.get('model_calls')!=0
                or any(smoke.get(k) is not True for k in ('checkpoint_retained','final_collection_retained','command_timeout_observed'))
                or smoke.get('cleanup',{}).get('confirmed') is not True):
            raise ValueError('Require completed live ACS lifecycle/transport smoke evidence')
    elif classification==RUNTIME_TRUST:
        if smoke.get('mode')!='live-acs-runtime-trust-smoke':
            raise ValueError('Require completed live ACS runtime trust smoke evidence')
        artifacts=smoke.get('artifacts',{})
        names=('result','config','report','runtime_trust','manifest')
        values={}
        for name in names:
            item=artifacts.get(name,{})
            if not item.get('path') or evidence(item['path'])!=item:
                raise ValueError('Runtime trust smoke artifact changed')
            files.append(item);values[name]=json.loads(root_file(item['path']).read_text())
        result,config,report,trust,manifest=(values[name] for name in names)
        agents=config.get('agents',[])
        if (result.get('job_id')!=smoke.get('job_id') or result.get('status')!='solver_complete'
                or result.get('reward')!=1 or result.get('n_errors')!=0
                or result.get('provider')!='dawnfm' or result.get('model')!='deepseek-v4.1-flash-1017'
                or any(result.get(k) is not True for k in ('source_version_bound','cleanup_confirmed','evidence_complete','trajectory_retained','pod_evidence_complete'))
                or len(agents)!=1 or agents[0].get('name') is not None
                or agents[0].get('import_path')!='universe_codex:UniverseCodex'
                or report.get('run_id')!='tbs-universe-linfeng-queue-'+result['job_id']
                or report.get('cleanup',{}).get('confirmed') is not True
                or report.get('harbor_exit_code')!=0
                or report.get('execution_identity',{}).get('config_sha256')!=digest(json.dumps(config,sort_keys=True).encode())
                or trust.get('tls_verified') is not True
                or trust.get('endpoint')!='https://dawnfm.gravarc.cn/v1/models'
                or result.get('published_evidence_manifest_sha256')!=artifacts['manifest']['sha256']
                or manifest.get('files',{}).get('agent/runtime-trust.json',{}).get('sha256')!=artifacts['runtime_trust']['sha256']):
            raise ValueError('Runtime trust repair requires actual adapter, verified TLS, model response and complete evidence')
    else:
        raise ValueError('Unknown platform failure classification')
    return files


def record(state, queue_state, key, job_ids, fix_evidence, reason):
    """Record one diagnosed incident after cleanup and the fix's smoke test."""
    if os.geteuid()!=0:raise PermissionError('Operator required')
    state,queue_state=Path(state),Path(queue_state)
    if not job_ids or len(set(job_ids))!=len(job_ids) or len(reason.strip())<40:
        raise ValueError('Name invalid jobs and explain the verified repair')
    target=directory(state,key);target.mkdir(parents=True,mode=0o700,exist_ok=True)
    with operator_lock(target/'record.lock'):
        assignment=dict(json.loads(root_file(state/'ledger.json').read_text())[key],key=key)
        batch,history=plans(state,assignment)
        # Repeating the same request does not allocate another set of cloud jobs.
        for item in history:
            if set(job_ids)=={r['original_job_id'] for r in item['plan']['replacements']}:
                return item['path']
        if assignment.get('state')!='waiting_platform' or assignment.get('stage')!='five_rollouts':
            raise ValueError('Only a parked five-rollout platform failure can be replaced')
        if not set(job_ids)<={r['job_id'] for r in batch}:
            raise ValueError('Only current batch slots can be replaced')
        files=[];replacements=[]
        for slot,request in enumerate(batch):
            job=request['job_id']
            if job not in job_ids:continue
            work=queue_state/job
            result=json.loads(root_file(work/'result.json').read_text())
            diagnosis_path=work/'platform-diagnosis.json'
            if not diagnosis_path.exists():diagnosis_path=work/'timeout-diagnosis.json'
            diagnosis=json.loads(root_file(diagnosis_path).read_text())
            classification=diagnosis.get('classification')
            fix_files=repair_evidence(fix_evidence,classification)
            for item in fix_files:
                if item not in files:files.append(item)
            report=json.loads(root_file(work/'report.json').read_text())
            claim=json.loads(root_file(work/'claim.json').read_text())
            if (any(result.get(k)!=v for k,v in request.items())
                    or result.get('status') not in (('infrastructure_failed','cancelled') if classification==RUNTIME_TRUST else ('infrastructure_failed',))
                    or result.get('reward') is not None
                    or result.get('source_version_bound') is not True
                    or result.get('cleanup_confirmed') is not True
                    or report.get('cleanup',{}).get('confirmed') is not True
                    or active_queue_worker(claim,job)
                    or diagnosis.get('job_id')!=job
                    or diagnosis.get('snapshot_sha256')!=request['snapshot_sha256']
                    or diagnosis.get('count_as_model_failure') is not False):
                raise ValueError('Require terminated, cleaned-up, diagnosed platform failure')
            local=[evidence(work/name) for name in ('result.json','report.json','claim.json')]+[evidence(diagnosis_path)]
            if classification==RUNTIME_TRUST:
                refs=diagnosis.get('evidence_files',[])
                if (diagnosis.get('evaluation_id')!=request['evaluation_id'] or not refs
                        or diagnosis.get('model_response_observed') is not False):
                    raise ValueError('Runtime trust failure needs version-bound no-response diagnosis')
                for item in refs:
                    if evidence(item['path'])!=item:raise ValueError('Platform diagnosis evidence changed')
                local+=refs
            files+=local
            seed=json.dumps([request,files[0],local],sort_keys=True).encode()
            replacement=dict(request,job_id=digest(seed)[:32])
            replacements.append(dict(slot=slot,original_job_id=job,request=replacement,
                                     classification=classification))
        value=dict(schema_version=1,recorded_at=time.time(),reason=reason,
                   batch_generation=assignment.get('batch_generation',assignment['generation']),
                   previous_jobs=batch,replacements=replacements,evidence_files=files,
                   **{k:assignment[k] for k in IDENTITY})
        path=target/f'{len(history)+1:04d}.json'
        immutable_json(path,value)
        plans(state,assignment)
        return path


def publish_request(request, entry):
    """Use the existing role queue protocol; no model credentials here."""
    inbox=Path(entry['root'])/'evaluation/inbox'
    path=inbox/(request['job_id']+'.json')
    # The operator side serializes with role-evaluation-batch submit.
    import fcntl
    with (inbox/'.batch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if path.exists():
            if path.is_symlink() or path.stat().st_uid!=entry['uid'] or json.loads(path.read_text())!=request:
                raise ValueError('Replacement queue request identity or ownership changed')
            return
        import tempfile
        fd,name=tempfile.mkstemp(prefix='.replacement-',dir=inbox)
        try:
            with os.fdopen(fd,'w') as stream:
                os.fchmod(stream.fileno(),0o600)
                os.fchown(stream.fileno(),entry['uid'],entry['gid'])
                json.dump(request,stream);stream.flush();os.fsync(stream.fileno())
            os.link(name,path)
        finally:Path(name).unlink(missing_ok=True)


def apply(state, ledger, config):
    """Called only by the dispatcher single writer; preserves review ownership."""
    for key,row in ledger.items():
        if not directory(state,key).exists():continue
        batch,history=plans(state,dict(row,key=key))
        if not history:continue
        receipts=[item['sha256'] for item in history]
        if row.get('batch_replacement_receipts')==receipts:continue
        if row.get('state')!='waiting_platform' or row.get('stage')!='five_rollouts':
            raise ValueError('Review changed before replacement plan was consumed')
        # Persist batch identity in the dispatcher ledger on the next normal
        # save. A crash before that save simply replays these same job IDs.
        entry=config['roles'][row['role']]
        for item in history:
            for replacement in item['plan']['replacements']:
                publish_request(replacement['request'],entry)
        row.update(batch_job_ids=[r['job_id'] for r in batch],
                   batch_replacement_receipts=receipts,
                   batch_generation=row.get('batch_generation',row['generation']),
                   reason='Diagnosed platform failures replaced on the same frozen version; waiting for the replacement results.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--queue-state',type=Path,required=True)
    parser.add_argument('--key',required=True)
    parser.add_argument('--job',action='append',required=True)
    parser.add_argument('--fix-evidence',type=Path,required=True)
    parser.add_argument('--reason',required=True)
    args=parser.parse_args()
    print(record(args.state,args.queue_state,args.key,args.job,args.fix_evidence,args.reason))


if __name__=='__main__':main()
