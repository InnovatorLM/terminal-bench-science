"""Finish interrupted local evidence publication; never launch a cloud/model job."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid


def pending_review_updates(assignment):
    """Identify operator publication repairs for this already reviewed batch."""
    from deployment_paths import universe_root
    from evaluation_queue import W, root_file
    from role_evaluation_batch import requests
    state=universe_root(W)/'.state/gewu-multiverse/evaluation-queue'
    applied=assignment.get('applied_publication_updates',[])
    updates=[]
    for request in requests(assignment):
        path=state/request['job_id']/'publication-recovery.json'
        try:
            raw=root_file(path).read_bytes();proof=json.loads(raw)
        except (OSError,ValueError,TypeError):continue
        if (not isinstance(proof,dict) or proof.get('job_id')!=request['job_id']
                or proof.get('kind')!='completed_execution_publication_recovery'
                or proof.get('recovered_status')!='solver_complete'):
            continue
        update=dict(job_id=request['job_id'],recovery_sha256=hashlib.sha256(raw).hexdigest())
        if update not in applied:updates.append(update)
    return updates


def recover(config, request):
    from evaluation_queue import (W, REQUEST_KEYS, root_file, queue_dir, atomic_json,
                                  validated_template, digest_tree, aggregate, active_queue_worker)
    from execution_evidence import inspect, trial_directories
    from evidence_files import digest_file
    from publish_evidence import publish
    from selftest_logs import credential_values
    job = Path(config['state']) / request['job_id']
    if os.geteuid() != 0 or not job.resolve().is_relative_to(W):
        raise ValueError('Operator workspace-local recovery required')
    queue_dir(job)
    lock_fd = os.open(job/'publication-recovery.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        claim = json.loads(root_file(job/'claim.json').read_text())
        if any(claim.get(key) != request[key] for key in REQUEST_KEYS):
            raise ValueError('Recovery request differs from real immutable execution')
        if claim.get('mock') is not False:
            return None
        if (job/'result.json').exists():
            value = json.loads(root_file(job/'result.json').read_text())
            if any(value.get(key) != request[key] for key in REQUEST_KEYS):
                raise ValueError('Retained result identity mismatch')
            return value
        if active_queue_worker(claim, request['job_id']):
            return None
        # Be conservative if the old controller PID still exists, including
        # PID reuse; recovery must never race a possibly active cloud guard.
        pid = claim.get('controller_pid')
        if type(pid) is int and Path('/proc', str(pid)).exists():
            return None
        report_path = job/'report.json'
        if not report_path.exists():
            return None
        report = json.loads(root_file(report_path).read_text())
        if report.get('cleanup', {}).get('confirmed') is not True or report.get('harbor_exit_code') != 0:
            return None
        registry = json.loads(root_file(config['registry']).read_text())
        entry = registry[request['evaluation_id']]
        if entry.get('dispatch', 'solver') != 'solver' or entry.get('enabled', True) is not True:
            return None
        expected = validated_template(entry, request)
        actual = json.loads(root_file(job/'config.json').read_text())
        run_id = 'tbs-universe-linfeng-queue-' + request['job_id']
        if actual.get('job_name') != run_id or report.get('run_id') != run_id:
            raise ValueError('Recovery run identity mismatch')
        for key in ('runtime_image', 'verifier_image'):
            if actual['environment']['kwargs'][key] != expected['environment']['kwargs'][key]:
                raise ValueError('Recovery image binding mismatch')
        if actual['agents'][0].get('model_name') != expected['agents'][0].get('model_name'):
            raise ValueError('Recovery model binding mismatch')
        if digest_tree(Path(entry['snapshot'])) != entry['snapshot_sha256']:
            raise ValueError('Frozen recovery source changed')
        result_path = job/'runs'/run_id/'result.json'
        evidence = inspect(report, result_path, actual, entry['snapshot_sha256'], True)
        answer = dict(request, provider='dawnfm', model='deepseek-v4.1-flash-1017',
                      **aggregate(report, result_path, actual, entry['snapshot_sha256'], True))
        if (not evidence['complete'] or answer['status'] != 'solver_complete'
                or not any('/agent/sessions/' in name and name.endswith('.jsonl') for name in evidence['files'])):
            return None
        trials = trial_directories(result_path.parent)
        if len(trials) != 1:
            return None
        results = queue_dir(config['results'])
        packet_root = results.parent/'evidence'
        packet_root.mkdir(mode=0o750, exist_ok=True);queue_dir(packet_root)
        os.chown(packet_root, 0, config['evaluator_uid'])
        packet = packet_root/request['job_id']
        staging = packet_root/('.recovery-'+uuid.uuid4().hex)
        receipt = publish(trials[0], staging, credential_values(results.parents[1]), config['evaluator_uid'])
        try:
            answer.update(evidence_packet='/evaluation/evidence/'+request['job_id'],
                          sanitized_trajectory_exported=receipt['trajectory_available'],
                          sanitized_native_session_exported=receipt['sanitized_native_session_exported'],
                          published_evidence_manifest_sha256=digest_file(staging/'manifest.json', packet_root))
            binding = dict(run_id=run_id, snapshot_sha256=entry['snapshot_sha256'],
                evidence_manifest_sha256=answer['evidence_manifest_sha256'],
                pod_evidence=report.get('pod_observations', {}),
                execution_identity=report.get('execution_identity', {}), cleanup_confirmed=True)
            atomic_json(staging/'execution-binding.json', binding, 0o640)
            os.chown(staging/'execution-binding.json', 0, config['evaluator_uid'])
            answer['execution_binding_sha256'] = digest_file(staging/'execution-binding.json', packet_root)
            # Preserve any interrupted partial publication instead of deleting
            # forensic evidence. No worker is active for this immutable job.
            if packet.exists():
                queue_dir(packet)
                packet.rename(job/('publication-before-recovery-'+uuid.uuid4().hex))
            staging.rename(packet)
            previous = results/(request['job_id']+'.json')
            if previous.exists():
                backup = job/('result-before-recovery-'+uuid.uuid4().hex+'.json')
                shutil.copyfile(root_file(previous), backup);backup.chmod(0o600)
            # Original Harbor results, native sessions and Pod reports stay
            # untouched. Only derived manifests and queue receipts are rebuilt.
            atomic_json(job/'evidence-manifest.json', evidence)
            proof = dict(kind='completed_execution_publication_recovery', at=time.time(),
                job_id=request['job_id'], report_sha256=digest_file(report_path, job),
                original_files=evidence['files'], model_calls=0, cloud_jobs_started=0,
                recovered_status=answer['status'], reward=answer['reward'])
            atomic_json(job/'publication-recovery.json', proof)
            atomic_json(job/'result.json', answer)
            atomic_json(previous, answer, 0o644)
            return answer
        finally:
            if staging.exists():shutil.rmtree(staging)


def main():
    from evaluation_queue import ID, read_request, root_file
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue-config',required=True,type=Path)
    parser.add_argument('--job-id',required=True)
    args=parser.parse_args()
    if not ID.fullmatch(args.job_id):raise ValueError('Invalid job ID')
    config=json.loads(root_file(args.queue_config).read_text())
    found=[]
    for queue in config.get('role_queues') or [config]:
        path=Path(queue['inbox'])/(args.job_id+'.json')
        if path.exists():found.append(({**config,**queue},read_request(path,queue['evaluator_uid'])))
    if len(found)!=1:raise ValueError('Require one original evaluator request')
    result=recover(*found[0])
    print(json.dumps({'job_id':args.job_id,'recovered':result is not None,
                      'status':result.get('status') if result else None,
                      'reward':result.get('reward') if result else None,
                      'cloud_jobs_started':0,'model_calls':0}))
    return 0 if result is not None else 1


if __name__=='__main__':raise SystemExit(main())
