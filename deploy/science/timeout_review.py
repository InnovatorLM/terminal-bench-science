"""Evidence-bound evaluator judgments; raw execution results stay immutable."""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

CLASSIFICATIONS={'difficulty','task_bug','platform_error','insufficient_evidence'}
CHECKS=('task_complete','environment_healthy','model_tools_healthy','sustained_problem_solving')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    path=Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Expected a regular unlinked review/evidence file')
    return path.read_bytes()


def timeout_context(trial,config):
    from datetime import datetime
    exception=(trial.get('exception_info') or {}).get('exception_type')
    budget=(config or {}).get('agents',[{}])[0].get('override_timeout_sec')
    try:
        phase=trial['agent_execution']
        elapsed=(datetime.fromisoformat(phase['finished_at'].replace('Z','+00:00'))-
                 datetime.fromisoformat(phase['started_at'].replace('Z','+00:00'))).total_seconds()
    except (KeyError,TypeError,ValueError):elapsed=None
    return dict(exception_type=exception,agent_budget_seconds=budget,agent_elapsed_seconds=elapsed)


def validate(record,assignment,request,results):
    """Recheck version, raw result and cited published evidence at every use."""
    results=Path(results);raw=read(results/(request['job_id']+'.json'));result=json.loads(raw)
    identity={**request,'review_key':assignment['key'],'commit':assignment['commit'],
              'task':assignment['task']}
    if (record.get('schema_version')!=1 or any(record.get(k)!=v for k,v in identity.items())
            or any(result.get(k)!=v for k,v in request.items())
            or record.get('result_sha256')!=sha(raw)
            or record.get('classification') not in CLASSIFICATIONS
            or not isinstance(record.get('reason'),str) or not record['reason'].strip()):
        raise ValueError('Timeout judgment identity or result changed')
    if result.get('status') not in ('agent_timeout_requires_cause_review','infrastructure_failed','incomplete_evidence','failed','cancelled'):
        raise ValueError('Only a terminal unscored/failed run can receive timeout diagnosis')
    packet=results.parent/'evidence'/request['job_id']
    if packet.is_symlink() or packet.resolve()!=packet:
        raise ValueError('Evidence packet path changed')
    classification=record['classification']
    refs=record.get('evidence',[])
    for item in refs:
        name=item.get('path','');relative=Path(name)
        if not name or relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Evidence reference escaped its packet')
        target=packet/relative
        if any(p.is_symlink() for p in [target,*target.parents] if p!=packet.parent):
            raise ValueError('Evidence reference is linked')
        if sha(read(target))!=item.get('sha256') or not str(item.get('finding','')).strip():
            raise ValueError('Cited evidence changed or has no explanation')
    if classification!='insufficient_evidence' and not refs:
        raise ValueError('Cite the actual trajectory/logs that support the diagnosis')
    if classification=='difficulty':
        context=result.get('timeout_review_context') or {}
        elapsed=context.get('agent_elapsed_seconds')
        if (result.get('status')!='agent_timeout_requires_cause_review'
                or result.get('provider')!='dawnfm' or result.get('model')!='deepseek-v4.1-flash-1017'
                or context.get('exception_type') not in ('AgentTimeoutError','AgentExecutionTimeoutError')
                or context.get('agent_budget_seconds')!=14400
                or type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<14399
                or any(result.get(k) is not True for k in
                       ('cleanup_confirmed','source_version_bound','pod_evidence_complete','agent_logs_retained'))):
            raise ValueError('Difficulty requires a real four-hour agent timeout with source, Pod and cleanup evidence')
        observations=record.get('observations',{})
        if any(not isinstance(observations.get(k),str) or not observations[k].strip() for k in CHECKS):
            raise ValueError('Explain task completeness, environment/tools health and sustained solving')
        manifest_raw=read(packet/'manifest.json');manifest=json.loads(manifest_raw)
        if sha(manifest_raw)!=result.get('published_evidence_manifest_sha256'):
            raise ValueError('Published evidence is not bound to the original result')
        files=manifest.get('files',{})
        if not any(item['path'].startswith('agent/') and item['path']!='agent/acs-collection.json'
                   and files.get(item['path'],{}).get('sha256')==item['sha256'] for item in refs):
            raise ValueError('Difficulty must cite retained model trajectory/session evidence')
    return dict(classification=classification,count_as_model_failure=classification=='difficulty',
                scoring_reward=0 if classification=='difficulty' else None,
                retry_recommended=classification=='platform_error',
                reason=record['reason'],evidence=refs,observations=record.get('observations',{}),
                raw_reward=result.get('reward'),quality_acceptance=False)


def record(assignment,request,results,assessment):
    results=Path(results);raw=read(results/(request['job_id']+'.json'))
    value=dict(schema_version=1,review_key=assignment['key'],commit=assignment['commit'],
               task=assignment['task'],**request,result_sha256=sha(raw),
               classification=assessment.get('classification'),reason=assessment.get('reason'),
               observations=assessment.get('observations',{}),evidence=assessment.get('evidence',[]))
    validate(value,assignment,request,results)
    directory=results.parent/'timeout-reviews'
    if directory.is_symlink() or not directory.is_dir():raise ValueError('Timeout review directory missing')
    path=directory/(request['job_id']+'.json')
    if path.exists():
        previous=json.loads(read(path))
        if {k:previous.get(k) for k in value}!=value:raise ValueError('Existing judgment differs; retain it and request operator correction')
        return path
    value.update(reviewer_uid=os.geteuid(),reviewed_at=time.time())
    import tempfile
    fd,name=tempfile.mkstemp(prefix='.timeout-',dir=directory)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600);json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.link(name,path)
    finally:Path(name).unlink(missing_ok=True)
    return path


def judgment(assignment,request,results):
    directory=Path(results).parent/'timeout-reviews';path=directory/(request['job_id']+'.json')
    if not path.exists():return None
    if directory.is_symlink():raise ValueError('Linked review directory')
    value=json.loads(read(path))
    if path.stat().st_uid!=directory.stat().st_uid or value.get('reviewer_uid')!=path.stat().st_uid:
        raise ValueError('Review ownership mismatch')
    return {**validate(value,assignment,request,results),'record':str(path),
            'record_sha256':sha(read(path))}
