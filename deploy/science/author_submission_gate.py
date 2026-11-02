"""Validate operator attestation before allocating evaluator work."""
import json,re,math,os,stat,hashlib
from pathlib import Path
W=Path('/mnt/data/zhanglinfeng/workspace')
from deployment_paths import universe_root, allowed_development_path
CHECKS=('schema','container_build','container_start','author_solve','verifier_positive','verifier_negative','post_iteration_rerun')
REQUIRED_ORDER=('schema','container_build','container_start','author_solve','verifier_positive','verifier_negative','post_iteration_rerun')
def validate_identity(commit,task):
    if not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):
        raise ValueError('Invalid source commit')
    if not isinstance(task,str) or not task or len(task)>1024 or '\\' in task or '\x00' in task:
        raise ValueError('Invalid task path')
    parts=task.split('/')
    if any(part in ('','.','..') for part in parts):
        raise ValueError('Task must be a canonical relative path')
def read_operator_file(path,limit=16*1024*1024):
    path=Path(path)
    if not path.is_absolute() or not allowed_development_path(path):raise ValueError('Operator evidence outside workspace')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022 or info.st_size>limit:
            raise ValueError('Invalid operator evidence type, ownership or size')
        data=stream.read(limit+1)
        after=os.fstat(stream.fileno())
        if len(data)>limit or len(data)!=info.st_size or (info.st_mtime_ns,info.st_ctime_ns)!=(after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError('Operator evidence changed during read')
        return data
def validate_receipt(receipt,commit,task):
    validate_identity(commit,task)
    if not isinstance(receipt,dict):raise ValueError('Author preflight must be an object')
    if receipt.get('commit')!=commit or receipt.get('task')!=task:raise ValueError('Stale author preflight identity')
    if not isinstance(receipt.get('snapshot_sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',receipt['snapshot_sha256']):raise ValueError('Missing frozen execution identity')
    if not isinstance(receipt.get('runtime_image'),str) or not re.fullmatch(r'.+@sha256:[a-f0-9]{64}',receipt['runtime_image']):raise ValueError('Missing image digest')
    checks=receipt.get('checks')
    if not isinstance(checks,dict) or any(checks.get(k) is not True for k in CHECKS):raise ValueError('Author container preflight incomplete')
    order=receipt.get('pre_submission_order')
    if order != list(REQUIRED_ORDER):raise ValueError('Pre-submission checks must be recorded in order')
    if receipt.get('iteration_applied') is not True:raise ValueError('No result-driven iteration')
    active=receipt.get('active_iteration_seconds')
    legacy_unknown=receipt.get('schema_version')==4 and receipt.get('budget_status')=='legacy_budget_unknown'
    if legacy_unknown:
        if active is not None:raise ValueError('Unknown historical budget cannot claim a measured duration')
    elif isinstance(active,bool) or not isinstance(active,(int,float)) or not math.isfinite(active) or not 0<=active<=18000:raise ValueError('Author iteration exceeds five hours')
    if receipt.get('difficulty_viable') is not True:raise ValueError('Direction abandoned: difficulty not viable')
    if not isinstance(receipt.get('evidence_files'),list) or not receipt['evidence_files']:raise ValueError('Missing actual execution evidence')
    for entry in receipt['evidence_files']:
        if not isinstance(entry,dict) or not isinstance(entry.get('path'),str) or not isinstance(entry.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',entry['sha256']):
            raise ValueError('Invalid execution evidence entry')
        if hashlib.sha256(read_operator_file(entry['path'])).hexdigest()!=entry['sha256']:raise ValueError('Invalid operator evidence digest')
    # Auto-generated v2 receipts could claim every check passed while explicitly
    # recording no previous execution/iteration. They must be requalified.
    if receipt.get('schema_version')==2:
        raise ValueError('Legacy automatic preflight requires evidence requalification')
    if receipt.get('schema_version') in (3,4) or receipt.get('origin')=='automatic_selftest_handoff':
        paths={entry['path'] for entry in receipt['evidence_files']}
        checks_evidence=receipt.get('check_evidence')
        if not isinstance(checks_evidence,dict) or any(
                not isinstance(checks_evidence.get(k),list) or not checks_evidence[k]
                or any(not isinstance(p,str) or p not in paths for p in checks_evidence[k]) for k in CHECKS):
            raise ValueError('Each automatic check requires bound execution evidence')
        proof_path=receipt.get('handoff_evidence')
        if not isinstance(proof_path,str) or proof_path not in paths:raise ValueError('Missing bound handoff evidence')
        proof=json.loads(read_operator_file(proof_path))
        bridge=proof.get('bridge',{});iteration=proof.get('iteration',{});budget=proof.get('budget',{})
        if (bridge.get('source_commit')!=commit or bridge.get('source_task_path')!=task
                or bridge.get('snapshot_sha256')!=receipt['snapshot_sha256']):
            raise ValueError('Handoff proof version mismatch')
        # Bind an admission to this exact executed job, not another successful
        # run of the same commit/task. Check the current idea disposition on
        # every approval, including cached qualification and dispatch retries.
        status_path=Path(proof_path).parent/'status.json'
        if str(status_path) not in paths:raise ValueError('Missing bound selftest status')
        status_raw=read_operator_file(status_path)
        status=json.loads(status_raw)
        if (hashlib.sha256(status_raw).hexdigest()!=bridge.get('selftest_status_sha256')
                or status.get('source_commit')!=commit or status.get('source_task_path')!=task
                or status.get('snapshot_sha256')!=receipt['snapshot_sha256']
                or status.get('job_id')!=status_path.parent.name):
            raise ValueError('Handoff source job identity mismatch')
        from selftest_budget_admission import assert_current_idea_active
        assert_current_idea_active(status_path.parent,status)
        if (iteration.get('initial_review') or iteration.get('linked_execution_verified') is not True
                or iteration.get('changed_committed_task') is not True
                or not re.fullmatch('[a-f0-9]{32}',str(iteration.get('previous_job','')))):
            raise ValueError('Handoff lacks verified result-driven iteration')
        if legacy_unknown:
            from selftest_budget_admission import validate_admission
            validate_admission(budget,commit,task,expected_status=status)
        elif budget.get('within_budget') is not True or budget.get('active_iteration_seconds_upper_bound')!=active:
            raise ValueError('Handoff budget evidence mismatch')
    return receipt

def approved(commit,task):
    validate_identity(commit,task)
    key=commit+'-'+__import__('hashlib').sha256(task.encode()).hexdigest()[:16]
    p=universe_root(W)/'.state/science-author-preflight'/ (key+'.json')
    if not p.exists():return False
    validate_receipt(json.loads(read_operator_file(p,1024*1024)),commit,task)
    return True


def review_budget_summary(commit,task):
    """Expose the admission limitation to reviewers without private file paths."""
    validate_identity(commit,task)
    key=commit+'-'+hashlib.sha256(task.encode()).hexdigest()[:16]
    path=universe_root(W)/'.state/science-author-preflight'/(key+'.json')
    if not path.exists():return None
    receipt=validate_receipt(json.loads(read_operator_file(path,1024*1024)),commit,task)
    if receipt.get('schema_version') not in (3,4):return None
    proof=json.loads(read_operator_file(receipt['handoff_evidence']))
    return dict(budget_status=receipt.get('budget_status','verified_within_budget'),
                active_iteration_seconds=receipt['active_iteration_seconds'],
                iteration_cutoff_at=proof['budget'].get('iteration_cutoff_at'),
                scope=proof['budget'].get('scope','completed_frozen_version_only'))
