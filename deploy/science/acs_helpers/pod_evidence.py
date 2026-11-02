"""Allowlisted Kubernetes observations; never persist Pod env, commands or Secrets."""
from datetime import datetime, timezone
import hashlib
import json
import re


def execution_identity(config):
    kw = config['environment']['kwargs']
    mode='unspecified'
    datasets=config.get('datasets',[])
    if len(datasets)==1 and datasets[0].get('path') and len(datasets[0].get('task_names',[]))==1:
        from pathlib import Path
        import tomllib
        path=Path(datasets[0]['path'])/datasets[0]['task_names'][0]/'task.toml'
        if path.is_file():
            task=tomllib.loads(path.read_text())
            verifier=task.get('verifier') or {}
            mode=verifier.get('environment_mode') or ('separate' if verifier.get('environment') is not None else 'shared')
    return {
        'run_id': config['job_name'],
        'verifier_mode':mode,
        'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        'tasks': [{'task_names': d.get('task_names', [])} for d in config.get('datasets', [])],
        'images': {key: kw.get(key) for key in ('runtime_image', 'verifier_image')},
    }


def pod_snapshot(pod, run_id):
    meta, spec, status = (pod.get(key) or {} for key in ('metadata', 'spec', 'status'))
    if (meta.get('labels') or {}).get('evaluation-run') != run_id:
        raise ValueError('Pod evidence belongs to another run')
    containers = []
    statuses = {c.get('name'): c for c in status.get('containerStatuses', [])}
    for container in spec.get('containers', []):
        current = statuses.get(container.get('name'), {})
        states = {}
        for state, value in (current.get('state') or {}).items():
            if state in ('running', 'terminated', 'waiting'):
                states[state] = {key: value[key] for key in ('startedAt', 'finishedAt', 'exitCode', 'signal', 'reason') if key in value}
        resources = container.get('resources') or {}
        containers.append({'name': container.get('name'), 'image': container.get('image'),
                           'image_id': current.get('imageID'), 'ready': current.get('ready'),
                           'restart_count': current.get('restartCount'), 'state': states,
                           'resources': {kind: {k: v for k, v in (resources.get(kind) or {}).items()
                                                if k in ('cpu', 'memory', 'ephemeral-storage', 'nvidia.com/gpu')}
                                         for kind in ('requests', 'limits')}})
    return {'name': meta.get('name'), 'uid': meta.get('uid'), 'namespace': meta.get('namespace'),
            'role': (meta.get('labels') or {}).get('universe-role'), 'run_id': run_id,
            'created_at': meta.get('creationTimestamp'), 'deletion_timestamp': meta.get('deletionTimestamp'),
            'phase': status.get('phase'), 'node': spec.get('nodeName'),
            'active_deadline_seconds': spec.get('activeDeadlineSeconds'),
            'started_at': status.get('startTime'), 'containers': containers}


def observe(snapshot, report):
    observations = report.setdefault('pod_observations', {})
    now = datetime.now(timezone.utc).isoformat()
    for pod in snapshot.get('pods', []):
        value = pod_snapshot(pod, report['run_id'])
        key = value['name'] + ':' + str(value['uid'])
        retained = observations.setdefault(key, {'first_observed_at': now, 'last_observed_at': now, 'states': []})
        retained['last_observed_at'] = now
        if not retained['states'] or retained['states'][-1]['pod'] != value:
            retained['states'].append({'observed_at': now, 'pod': value})
            # Bound control-plane evidence while retaining initial state and latest transitions.
            if len(retained['states']) > 128:
                retained['states'] = retained['states'][:1] + retained['states'][-127:]


def validate(report, config=None):
    identity = report.get('execution_identity') or {}
    checks = {'run_id_bound': identity.get('run_id') == report.get('run_id') and bool(report.get('run_id')),
              'config_bound': bool(re.fullmatch('[a-f0-9]{64}', str(identity.get('config_sha256', '')))),
              'runtime_observed': False, 'verifier_observed': False}
    if config is not None:
        checks['config_bound'] = identity == execution_identity(config)
    for record in (report.get('pod_observations') or {}).values():
        for observation in record.get('states', []):
            pod = observation.get('pod') or {}
            if pod.get('run_id') != report.get('run_id') or not pod.get('node'):
                continue
            for c in pod.get('containers', []):
                if not c.get('image_id') or not all((c.get('resources', {}).get(kind) or {}).get('cpu') and
                                                    (c.get('resources', {}).get(kind) or {}).get('memory')
                                                    for kind in ('requests', 'limits')):
                    continue
                for role in ('runtime', 'verifier'):
                    image = identity.get('images', {}).get(role + '_image')
                    if image and '@sha256:' in image and c.get('image') == image and pod.get('role') == role:
                        checks[role + '_observed'] = True
    if identity.get('verifier_mode')=='shared':
        checks['verifier_observed']=checks['runtime_observed']
    if report.get('pod_capture_error_type'):
        checks['capture_without_errors']=False
    return {'complete': all(checks.values()), 'checks': checks,
            'missing': [key for key, value in checks.items() if not value]}


def capture(pod, directory, stage):
    """Persist an allowlisted lifecycle observation before the Pod can disappear."""
    import os
    from pathlib import Path
    import uuid
    root=Path('/mnt/data/zhanglinfeng/workspace')
    directory=Path(directory)
    if not directory.is_absolute() or not directory.resolve().is_relative_to(root) or directory.is_symlink():
        raise ValueError('Evidence journal must be workspace-local')
    run_id=(pod.get('metadata',{}).get('labels') or {}).get('evaluation-run')
    if not isinstance(run_id,str) or not run_id.startswith('tbs-universe-linfeng-'):
        raise ValueError('Missing tagged run identity')
    value=pod_snapshot(pod,run_id)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    name=uuid.uuid4().hex
    pending=directory/(name+'.pending');target=directory/(name+'.json')
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump({'observed_at':datetime.now(timezone.utc).isoformat(),'stage':stage,'pod':value},stream)
    pending.replace(target)


def merge_captured(report, directory, known):
    """Merge complete adapter journal entries; never infer a missed Pod state."""
    from pathlib import Path
    import stat
    directory=Path(directory)
    for path in sorted(directory.glob('*.json')):
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_size>1024*1024:
            raise ValueError('Unsafe Pod evidence journal entry')
        item=json.loads(path.read_text());pod=item['pod']
        if pod.get('run_id')!=report['run_id']:
            raise ValueError('Captured Pod belongs to another run')
        key=pod['name']+':'+str(pod['uid'])
        records=report.setdefault('pod_observations',{})
        record=records.setdefault(key,{'first_observed_at':item['observed_at'],'last_observed_at':item['observed_at'],'states':[]})
        record['first_observed_at']=min(record['first_observed_at'],item['observed_at'])
        record['last_observed_at']=max(record['last_observed_at'],item['observed_at'])
        event={'observed_at':item['observed_at'],'pod':pod,'capture_stage':item.get('stage')}
        if event not in record['states']:
            record['states'].append(event)
            record['states'].sort(key=lambda r:r['observed_at'])
        known[('pods',pod['name'])]=pod['uid']
