#!/usr/bin/env python3
"""Verify retained legacy packets and add index fields without rescoring a job."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from evaluation_queue import W, ID, REQUEST_KEYS, active_queue_worker, digest_tree, root_file
from evidence_files import digest_file

FIELDS = ('run_id', 'raw_trajectory_retained', 'native_session_retained',
          'sanitized_native_session_exported', 'published_evidence_manifest_sha256',
          'execution_binding_sha256', 'raw_trajectory_exported')
TRANSACTION = 'evidence-receipt-refresh'


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def regular(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve().is_relative_to(W.resolve()), 'outside_workspace')
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'linked_operator_file')
    return root_file(path)


def read(path):
    return regular(path).read_bytes()


def directory(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve().is_relative_to(W.resolve()), 'outside_workspace')
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'linked_directory')
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'unsafe_operator_directory')
    return path


def relative(root, name):
    require(isinstance(name, str) and bool(name), 'invalid_evidence_path')
    path = Path(name)
    require(not path.is_absolute() and '..' not in path.parts and str(path) == name,
            'invalid_evidence_path')
    return Path(root) / path


def file_digest(path, root):
    # Hash large native trajectories by streaming, never by read_bytes().
    regular(path)
    return digest_file(path, root)


def inactive(claim, job_id):
    if active_queue_worker(claim, job_id):
        return False
    pid = claim.get('controller_pid')
    # A reused controller PID is conservatively unknown, not proof of inactivity.
    return not (type(pid) is int and pid > 1 and Path('/proc', str(pid)).exists())


def replace_bytes(path, raw, mode, gid):
    fd, pending = tempfile.mkstemp(prefix='.receipt-refresh-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, gid)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


def save_once(path, raw):
    if path.exists():
        require(read(path) == raw, 'backup_conflict')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def original_receipts(job, private_raw, public_raw, request):
    transaction = job / TRANSACTION
    journal = transaction / 'journal.json'
    if not journal.exists():
        require(json.loads(private_raw) == json.loads(public_raw), 'private_public_result_mismatch')
        return private_raw, public_raw, None
    directory(transaction)
    record = json.loads(read(journal))
    require(record.get('schema_version') == 1 and record.get('request') == request,
            'refresh_transaction_identity_mismatch')
    old_private = read(transaction / 'private.before.json')
    old_public = read(transaction / 'public.before.json')
    planned = read(transaction / 'result.after.json')
    require(sha(old_private) == record.get('private_before_sha256')
            and sha(old_public) == record.get('public_before_sha256')
            and sha(planned) == record.get('after_sha256'), 'refresh_transaction_hash_mismatch')
    require(private_raw in (old_private, planned) and public_raw in (old_public, planned),
            'refresh_transaction_result_conflict')
    require(json.loads(old_private) == json.loads(old_public), 'private_public_result_mismatch')
    return old_private, old_public, record


def verified_fields(config, job, request, result):
    registry = json.loads(read(config['registry']))
    registered = registry[request['evaluation_id']]
    require(registered.get('dispatch', 'solver') == 'solver', 'not_a_solver_job')
    require(registered.get('snapshot_sha256') == request['snapshot_sha256'], 'registered_version_mismatch')
    snapshot = directory(registered['snapshot'])
    require(digest_tree(snapshot) == request['snapshot_sha256'], 'frozen_source_changed')
    template_raw = read(registered['template'])
    require(sha(template_raw) == registered.get('template_sha256'), 'registered_template_changed')
    template = json.loads(template_raw)
    actual = json.loads(read(job / 'config.json'))
    report = json.loads(read(job / 'report.json'))
    evidence = json.loads(read(job / 'evidence-manifest.json'))
    run_id = 'tbs-universe-linfeng-queue-' + request['job_id']
    require(sha(json.dumps(evidence, sort_keys=True).encode()) == result.get('evidence_manifest_sha256'),
            'original_manifest_mismatch')
    require(evidence.get('snapshot_sha256') == request['snapshot_sha256'], 'original_version_mismatch')
    require(evidence.get('run_id') == report.get('run_id') == actual.get('job_name') == run_id,
            'run_identity_mismatch')
    require(result.get('run_id') in (None, run_id), 'result_run_identity_mismatch')
    identity = report.get('execution_identity') or {}
    images = {key: actual['environment']['kwargs'].get(key) for key in ('runtime_image', 'verifier_image')}
    require(identity.get('run_id') == run_id and identity.get('config_sha256') ==
            sha(json.dumps(actual, sort_keys=True).encode()) and identity.get('images') == images,
            'report_config_identity_mismatch')
    require(all(images[key] == template['environment']['kwargs'].get(key) for key in images),
            'registered_image_mismatch')
    require(len(actual['agents']) == len(template['agents']) == 1 and
            actual['agents'][0].get('model_name') == template['agents'][0].get('model_name'),
            'registered_model_mismatch')
    current_dataset = [{'path': str(snapshot.parent), 'task_names': [snapshot.name]}]
    require(actual.get('datasets') in (template.get('datasets'), current_dataset), 'registered_dataset_mismatch')
    require(identity.get('tasks') == [{'task_names': d.get('task_names', [])} for d in actual['datasets']],
            'report_task_identity_mismatch')
    results = directory(config['results'])
    packet = directory(results.parent / 'evidence' / request['job_id'])
    manifest_raw, binding_raw = read(packet / 'manifest.json'), read(packet / 'execution-binding.json')
    manifest, binding = json.loads(manifest_raw), json.loads(binding_raw)
    for key, raw in [('published_evidence_manifest_sha256', manifest_raw), ('execution_binding_sha256', binding_raw)]:
        require(result.get(key) in (None, sha(raw)), 'existing_' + key + '_mismatch')
    require(binding.get('run_id') == run_id and binding.get('snapshot_sha256') == request['snapshot_sha256']
            and binding.get('evidence_manifest_sha256') == result['evidence_manifest_sha256']
            and binding.get('execution_identity') == identity
            and binding.get('pod_evidence') == report.get('pod_observations', {})
            and binding.get('cleanup_confirmed') is (result.get('cleanup_confirmed') is True)
            and binding.get('cleanup_confirmed') is ((report.get('cleanup') or {}).get('confirmed') is True),
            'published_execution_binding_mismatch')
    require(manifest.get('sanitized') is True and manifest.get('raw_trajectory_exported') is False,
            'packet_not_sanitized')
    originals, published = evidence.get('files'), manifest.get('files')
    require(isinstance(originals, dict) and bool(originals)
            and isinstance(published, dict) and bool(published), 'evidence_files_missing')
    run = directory(job / 'runs' / run_id)
    checked = {}
    prefixes = set()
    for name, checksum in originals.items():
        path = relative(run, name)
        require(len(Path(name).parts) >= 2, 'original_trial_path_missing')
        prefixes.add(Path(name).parts[0])
        checked[name] = file_digest(path, run)
        require(checked[name] == checksum, 'original_file_hash_mismatch')
    require(len(prefixes) == 1, 'multiple_original_trials')
    trial_name = next(iter(prefixes)); trial = directory(run / trial_name)
    for name, metadata in published.items():
        require(isinstance(metadata, dict), 'invalid_published_file_metadata')
        target = relative(packet, name)
        require(file_digest(target, packet) == metadata.get('sha256')
                and target.stat().st_size == metadata.get('bytes'), 'published_file_hash_mismatch')
        source = relative(trial, name)
        source_name = trial_name + '/' + name
        source_hash = checked.get(source_name)
        if source_hash is None:
            source_hash = file_digest(source, run)
        require(source_hash == metadata.get('source_sha256'), 'published_source_hash_mismatch')
    native_sources = [name for name in originals if name.startswith(trial_name + '/agent/sessions/')
                      and name.endswith('.jsonl') and originals[name] != sha(b'')]
    native_exports = [meta for name, meta in published.items() if name.startswith('agent/sessions/')
                      and name.endswith('.jsonl') and meta['bytes'] > 0]
    normalized = trial_name + '/agent/trajectory.json'
    return dict(run_id=run_id,
                raw_trajectory_retained=(evidence.get('checks') or {}).get('trajectory_retained') is True
                    and normalized in originals and originals[normalized] != sha(b''),
                native_session_retained=bool(native_sources),
                sanitized_native_session_exported=bool(native_exports),
                published_evidence_manifest_sha256=sha(manifest_raw),
                execution_binding_sha256=sha(binding_raw), raw_trajectory_exported=False)


def refresh(config, job_id, *, apply=False):
    require(os.geteuid() == 0 and isinstance(job_id, str) and bool(ID.fullmatch(job_id)), 'operator_and_job_id_required')
    job = directory(Path(config['state']) / job_id)
    private, public = job / 'result.json', directory(config['results']) / (job_id + '.json')
    lock_fd = os.open(job / 'publication-recovery.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        claim_raw = read(job / 'claim.json'); claim = json.loads(claim_raw)
        request = {key: claim[key] for key in REQUEST_KEYS}
        require(request['job_id'] == job_id and all(isinstance(v, str) for v in request.values()), 'claim_identity_mismatch')
        outcome = dict(job_id=job_id, model_calls=0, cloud_jobs_started=0)
        if claim.get('mock') is not False:
            return dict(outcome, outcome='skipped_mock')
        if not inactive(claim, job_id):
            return dict(outcome, outcome='skipped_active')
        private_raw, public_raw = read(private), read(public)
        old_private, old_public, transaction = original_receipts(job, private_raw, public_raw, request)
        result = json.loads(old_private)
        require(all(result.get(key) == value for key, value in request.items()), 'result_request_identity_mismatch')
        if result.get('historical_evidence_delivery'):
            return dict(outcome, outcome='skipped_historical_delivery')
        fields = verified_fields(config, job, request, result)
        answer = dict(result, **fields)
        after = (json.dumps(answer, indent=2) + '\n').encode()
        changed = [key for key in FIELDS if result.get(key) != fields[key]]
        if transaction:
            require(sha(after) == transaction['after_sha256'], 'refresh_transaction_evidence_changed')
        if not changed and not transaction:
            return dict(outcome, outcome='unchanged', updated_fields=[])
        if private_raw == after and public_raw == after:
            return dict(outcome, outcome='unchanged', updated_fields=[])
        if not apply:
            return dict(outcome, outcome='would_refresh', updated_fields=changed)
        require(read(job / 'claim.json') == claim_raw and inactive(claim, job_id), 'job_became_active')
        require(read(private) == private_raw and read(public) == public_raw, 'result_changed_during_verification')
        backup = job / TRANSACTION
        if not backup.exists():
            backup.mkdir(mode=0o700)
        directory(backup)
        save_once(backup / 'private.before.json', old_private)
        save_once(backup / 'public.before.json', old_public)
        save_once(backup / 'result.after.json', after)
        record = dict(schema_version=1, request=request, private_before_sha256=sha(old_private),
                      public_before_sha256=sha(old_public), after_sha256=sha(after), updated_fields=changed,
                      model_calls=0, cloud_jobs_started=0)
        save_once(backup / 'journal.json', (json.dumps(record, indent=2) + '\n').encode())
        for path, current in [(private, private_raw), (public, public_raw)]:
            if current != after:
                info = regular(path).stat()
                replace_bytes(path, after, stat.S_IMODE(info.st_mode), info.st_gid)
        require(read(private) == read(public) == after, 'receipt_readback_mismatch')
        return dict(outcome, outcome='refreshed', updated_fields=changed,
                    backup_directory=TRANSACTION)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue-config', required=True, type=Path)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        require(bool(ID.fullmatch(args.job_id)), 'invalid_job_id')
        config = json.loads(read(args.queue_config))
        found = [{**config, **queue} for queue in config.get('role_queues') or [config]
                 if (Path(queue['results']) / (args.job_id + '.json')).is_file()]
        require(len(found) == 1, 'require_one_existing_result_queue')
        print(json.dumps(refresh(found[0], args.job_id, apply=args.apply)))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(json.dumps(dict(job_id=args.job_id, outcome='refused',
                              reason=str(error) if type(error) is ValueError else type(error).__name__,
                              model_calls=0, cloud_jobs_started=0)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
