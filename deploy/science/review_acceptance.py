"""Finalize explicit evaluator acceptance against retained, immutable evidence.

The dispatcher owns this step. It does not judge scientific quality or start
new evaluations; an explicit evaluator decision remains necessary.
"""
import hashlib
import json
from pathlib import Path
import stat

from acs_helpers.pod_evidence import validate as validate_pods
from formal_review_decision import apply, record
from role_evaluation_batch import requests, summary
from evidence_files import sha256_stream


class EvidenceGap(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise EvidenceGap(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def trusted(path):
    path = Path(path)
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'linked_evidence')
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'evidence_must_be_operator_owned')
    return path


def read(path):
    return trusted(path).read_bytes()


def digest(path):
    with trusted(path).open('rb') as stream:
        return sha256_stream(stream)


def relative(root, name):
    require(isinstance(name, str) and bool(name), 'missing_evidence_path')
    path = Path(name)
    require(not path.is_absolute() and '..' not in path.parts and str(path) == name,
            'unsafe_evidence_path')
    return Path(root) / path


def verify_trial(assignment, request, results, jobs, score):
    """Check both private originals and the reviewer-visible sanitized packet."""
    job_id = request['job_id']
    raw_result = read(results / (job_id + '.json'))
    result = json.loads(raw_result)
    require(all(result.get(k) == v for k, v in request.items()), 'result_version_mismatch')
    require(result.get('provider') == 'dawnfm' and result.get('model') == 'deepseek-v4.1-flash-1017',
            'wrong_evaluation_model')
    job = Path(jobs) / job_id
    require(json.loads(read(job / 'result.json')) == result, 'private_result_mismatch')
    evidence_raw = read(job / 'evidence-manifest.json')
    evidence = json.loads(evidence_raw)
    require(sha(json.dumps(evidence, sort_keys=True).encode()) == result.get('evidence_manifest_sha256'),
            'original_manifest_mismatch')
    require(evidence.get('snapshot_sha256') == assignment['snapshot_sha256'], 'original_version_mismatch')
    report_raw = read(job / 'report.json')
    report = json.loads(report_raw)
    config = json.loads(read(job / 'config.json'))
    run_id = evidence.get('run_id')
    require(isinstance(run_id, str) and run_id and Path(run_id).name == run_id,
            'missing_run_identity')
    require(run_id == report.get('run_id') == config.get('job_name')
            and validate_pods(report, config)['complete'], 'pod_resources_or_image_identity_missing')
    require(report.get('cleanup', {}).get('confirmed') is True, 'cleanup_not_confirmed')
    originals = evidence.get('files', {})
    require(isinstance(originals, dict) and bool(originals), 'original_files_missing')
    for name, checksum in originals.items():
        require(digest(relative(job / 'runs' / run_id, name)) == checksum, 'original_file_hash_mismatch')
    require(any('/agent/sessions/' in name and name.endswith('.jsonl') for name in originals),
            'native_session_missing')
    require(any(name.endswith('/agent/trajectory.json') for name in originals),
            'normalized_trajectory_missing')
    if score.get('score_source') == 'verifier':
        require(evidence.get('complete') is True, 'original_evidence_incomplete')
        require(any(name.endswith('/verifier/test-stdout.txt') for name in originals), 'verifier_log_missing')
    packet = results.parent / 'evidence' / job_id
    manifest_raw = read(packet / 'manifest.json')
    require(sha(manifest_raw) == result.get('published_evidence_manifest_sha256'), 'published_manifest_mismatch')
    manifest = json.loads(manifest_raw)
    files = manifest.get('files', {})
    require(isinstance(files, dict) and bool(files), 'published_files_missing')
    for name, metadata in files.items():
        path = relative(packet, name)
        require(digest(path) == metadata.get('sha256') and path.stat().st_size == metadata.get('bytes'),
                'published_file_hash_mismatch')
        if name.startswith(('agent/', 'verifier/')):
            matches = [value for source, value in originals.items() if source.endswith('/' + name)]
            require(matches == [metadata.get('source_sha256')], 'published_source_mismatch')
    require('agent/trajectory.json' in files and any(
        name.startswith('agent/sessions/') and name.endswith('.jsonl') for name in files),
        'published_trajectory_missing')
    binding_raw = read(packet / 'execution-binding.json')
    binding = json.loads(binding_raw)
    require(binding.get('run_id') == run_id and binding.get('snapshot_sha256') == assignment['snapshot_sha256']
            and binding.get('evidence_manifest_sha256') == result.get('evidence_manifest_sha256')
            and binding.get('cleanup_confirmed') is True
            and binding.get('execution_identity') == report.get('execution_identity')
            and binding.get('pod_evidence') == report.get('pod_observations'), 'published_execution_binding_mismatch')
    return dict(job_id=job_id, run_id=run_id, result_sha256=sha(raw_result),
                original_manifest_sha256=sha(evidence_raw), report_sha256=sha(report_raw),
                published_manifest_sha256=sha(manifest_raw), execution_binding_sha256=sha(binding_raw),
                reward=score['reward'], timeout_review=score.get('timeout_review'))


def verify(assignment, entry, jobs, execution):
    completion = assignment.get('review_completion') or {}
    require(completion.get('key') == assignment['key']
            and completion.get('generation') == assignment['generation']
            and completion.get('role') == assignment['role']
            and completion.get('review_recommendation') == 'accept'
            and completion.get('reason_sha256') == sha(assignment.get('reason', '').encode()),
            'matching_evaluator_completion_missing')
    require(execution == {k: assignment[k] for k in ('evaluation_id', 'snapshot_sha256')},
            'registered_version_mismatch')
    results = Path(entry['root']) / 'evaluation/results'
    batch = summary(assignment, results)
    require(batch['complete'], 'five_valid_rollouts_required')
    require(batch['difficulty_threshold_met'] is True, 'pass_rate_exceeds_sixty_percent')
    trials = []
    for request, score in zip(requests(assignment), batch['jobs']):
        try:
            trials.append(verify_trial(assignment, request, results, jobs, score))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            code = str(error) if isinstance(error, EvidenceGap) else type(error).__name__
            raise EvidenceGap(code + ':job=' + request['job_id']) from None
    require(len({trial['run_id'] for trial in trials}) == 5, 'five_independent_runs_required')
    return dict(schema_version=1, pass_rate=batch['pass_rate'], trials=trials,
                review_completion=completion)


def finalize(state, ledger, config, jobs, lookup_execution):
    """Caller persists ledger first while holding the dispatcher single-writer lock.

    The immutable receipt is written before publishing acceptance. A crash
    between these steps recovers through formal_review_decision.apply().
    """
    blocked = {}
    for key, row in ledger.items():
        if (row.get('state') != 'completed' or row.get('review_recommendation') != 'accept'
                or row.get('scientific_acceptance') is True):
            continue
        try:
            proof = verify(dict(row, key=key), config['roles'][row['role']], jobs,
                           lookup_execution(row['commit'], row['task']))
            record(state, key, 'review-dispatcher',
                   'Explicit evaluator acceptance with five independently retained, version-bound ACS results; pass rate at most 60%.',
                   evidence=proof)
            apply(state, {key: row})
            row.pop('acceptance_blockers', None)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            code = str(error) if isinstance(error, EvidenceGap) else type(error).__name__
            row.update(formal_decision='pending_evidence', scientific_acceptance=False,
                       acceptance_blockers=[code])
            blocked[key] = dict(key=key, author=row.get('author_role'), evaluator=row['role'],
                                commit=row.get('commit'), task=row.get('task'), owner='platform',
                                blockers=[code], next_action='Repair the retained evidence or evaluator decision for this exact version. Do not rebuild or resubmit the task.')
    return blocked
