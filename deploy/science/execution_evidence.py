"""Validate and hash retained solver evidence without exposing private file paths."""
import hashlib
import json
import math
from pathlib import Path
import stat
from acs_helpers.pod_evidence import validate as validate_pods


def read_file(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError('Evidence path escaped job')
    if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError('Evidence symlink refused')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
        raise ValueError('Evidence file type/size refused')
    return path.read_bytes()


def inspect(report, result_path, config=None, snapshot_sha256=None, snapshot_unchanged=False):
    result_path = Path(result_path)
    run_root = result_path.parent
    checks = {'trajectory_retained': False, 'verifier_retained': False, 'trial_complete': False,
              'run_id_bound': report.get('run_id') == run_root.name,
              'source_version_bound': bool(snapshot_sha256) and snapshot_unchanged is True,
              'pod_evidence_complete': validate_pods(report, config)['complete']}
    hashes = {}
    errors = []
    trials = trial_directories(run_root)
    raw_agent_files = []
    if len(trials) == 1:
        trial = trials[0]
        def read(name):
            data = read_file(trial / name, run_root)
            hashes[str((trial / name).relative_to(run_root))] = hashlib.sha256(data).hexdigest()
            return data
        # Hash partial native evidence independently. Missing result.json or an
        # unfinished ATIF conversion must not hide the actual Codex session.
        for path in sorted((trial / 'agent').rglob('*')):
            if path.is_file():
                try:
                    name = str(path.relative_to(trial))
                    read(name)
                    if name != 'agent/acs-collection.json': raw_agent_files.append(name)
                except (OSError, ValueError, TypeError) as exc:
                    errors.append(type(exc).__name__)
        result = {}
        try:
            result = json.loads(read('result.json'))
            checks['trial_complete'] = bool(result.get('finished_at')) and not result.get('exception_info') and not result.get('cleanup_exception_info')
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(type(exc).__name__)
        try:
            trace = json.loads(read('agent/trajectory.json'))
            steps = trace.get('steps')
            checks['trajectory_retained'] = (str(trace.get('schema_version', '')).startswith('ATIF-')
                and isinstance(steps, list) and bool(steps)
                and all(isinstance(step, dict) for step in steps)
                and any(step.get('source') == 'agent' for step in steps))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(type(exc).__name__)
        try:
            rewards = (result.get('verifier_result') or {}).get('rewards', {})
            if (trial/'verifier/reward.json').is_file():
                actual_rewards=json.loads(read('verifier/reward.json'))
            else:
                actual_rewards={'reward':float(read('verifier/reward.txt').decode().strip())}
            # A verifier may deliberately be silent. Retaining the log file
            # proves collection; its byte count does not determine scoring.
            read('verifier/test-stdout.txt')
            valid=(isinstance(rewards,dict) and bool(rewards)
                and all(type(v) in (int,float) and math.isfinite(v) for v in rewards.values()))
            checks['verifier_retained'] = (valid and actual_rewards == rewards
                and rewards.get('reward') in (0,1))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(type(exc).__name__)
    else:
        errors.append('ExpectedExactlyOneTrial')
    if config is not None:
        checks['run_id_bound'] = checks['run_id_bound'] and config.get('job_name') == run_root.name
    missing = [key for key, passed in checks.items() if not passed]
    return {'schema_version': 1, 'run_id': report.get('run_id'), 'snapshot_sha256': snapshot_sha256,
            'complete': not missing and not errors, 'checks': checks, 'missing': missing,
            'errors': errors, 'files': hashes, 'raw_agent_files': raw_agent_files,
            'agent_logs_retained': bool(raw_agent_files), 'scientific_acceptance': False}


def trial_directories(run_root):
    root = Path(run_root)
    return sorted({p.parent for p in root.glob('*/result.json')} |
                  {p.parent for p in root.glob('*/trial.log')} |
                  {p.parent for p in root.glob('*/agent') if p.is_dir()})


def public_status(evidence):
    return {'evidence_complete': evidence['complete'], 'evidence_status': 'complete' if evidence['complete'] else 'incomplete_evidence',
            'trajectory_retained': evidence['checks']['trajectory_retained'],
            'agent_logs_retained': evidence.get('agent_logs_retained', False),
            'verifier_evidence_retained': evidence['checks']['verifier_retained'],
            'pod_evidence_complete': evidence['checks']['pod_evidence_complete'],
            'source_version_bound': evidence['checks']['source_version_bound'],
            'missing_evidence': evidence['missing'],
            'evidence_manifest_sha256': hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}


def materialize_private_trajectory(private, result_path, entry, config):
    """Bind legacy controller-private retention into its equally private job."""
    from evaluation_queue import root_file
    private=Path(private);run=Path(result_path).parent
    trials=list(run.glob('*/result.json'))
    if len(trials)!=1:raise ValueError('Expected one private trajectory trial')
    identity_bytes=root_file(private/'identity.json').read_bytes()
    identity=json.loads(identity_bytes);retention=json.loads(root_file(private/'retention.json').read_text())
    if (identity.get('snapshot_sha256')!=entry['snapshot_sha256'] or identity.get('snapshot_path')!=entry['snapshot']
            or identity.get('provider')!='dawnfm' or identity.get('model')!='deepseek-v4.1-flash-1017'
            or retention.get('review_ready') is not True or retention.get('snapshot_sha256')!=entry['snapshot_sha256']
            or retention.get('identity_sha256')!=hashlib.sha256(identity_bytes).hexdigest()):
        raise ValueError('Private trajectory identity mismatch')
    if any(identity.get(k)!=config['environment']['kwargs'][k] for k in ('runtime_image','verifier_image')):
        raise ValueError('Private trajectory image mismatch')
    if not config['job_name'].endswith(private.name):raise ValueError('Private trajectory run mismatch')
    files={}
    for name,key in [('trajectory.json','atif_sha256'),('mini-swe-agent.trajectory.json','raw_sha256')]:
        raw=root_file(private/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=retention.get(key):raise ValueError('Private trajectory digest mismatch')
        files[name]=raw
    trace=json.loads(files['trajectory.json'])
    if not str(trace.get('schema_version','')).startswith('ATIF') or not trace.get('steps'):
        raise ValueError('Private trajectory has no ATIF steps')
    target=trials[0].parent/'agent';target.mkdir(exist_ok=True,mode=0o700)
    for name,raw in files.items():
        path=target/name
        if path.exists():
            if read_file(path,run)!=raw:raise ValueError('Existing trial trajectory differs from retention')
        else:
            with path.open('xb') as stream:stream.write(raw)
            path.chmod(0o600)
    return True
