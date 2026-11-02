"""Small role-visible evidence index; never load a multi-hour trajectory here.

The index checks published manifest/binding identity and file availability.
Formal acceptance separately hashes original and published evidence contents.
"""
import hashlib
import json
from pathlib import Path
import re


def native_sessions(files):
    return [name for name in files if name.startswith('agent/sessions/') and name.endswith('.jsonl')]


def observed_containers(binding):
    """Keep one useful actual observation per Pod/container, not requested limits."""
    rows = {}
    for record in (binding.get('pod_evidence') or {}).values():
        for event in record.get('states', []):
            pod = event.get('pod') or {}
            if pod.get('run_id') != binding.get('run_id'):
                continue
            for container in pod.get('containers', []):
                resources = container.get('resources') or {}
                row = {key: pod.get(key) for key in ('name', 'uid', 'namespace', 'role', 'node')}
                row.update(container=container.get('name'), image=container.get('image'),
                           image_id=container.get('image_id'),
                           resources={kind: {key: value for key, value in (resources.get(kind) or {}).items()
                                             if key in ('cpu', 'memory', 'ephemeral-storage', 'nvidia.com/gpu')}
                                      for kind in ('requests', 'limits')})
                key = (row['uid'], row['name'], row['container'])
                # Do not replace a running observation with a later deletion
                # snapshot that no longer carries imageID/resources.
                complete = row['node'] and row['image_id'] and all(
                    row['resources'][kind].get('cpu') and row['resources'][kind].get('memory')
                    for kind in ('requests', 'limits'))
                if key not in rows or complete:
                    rows[key] = row
    return list(rows.values())


def entry(request, result, results):
    """Index only the packet belonging to this immutable queue request."""
    result = result or {}
    row = dict(job_id=request['job_id'], run_id=result.get('run_id'),
               snapshot_sha256=request['snapshot_sha256'], evidence_packet=None,
               runtime_image=None, runtime_digest=None, verifier_image=None, verifier_digest=None,
               verifier_mode=None, observed_containers=[],
               raw_trajectory_retained=result.get('raw_trajectory_retained', result.get('trajectory_retained')) is True,
               native_session_retained=result.get('native_session_retained') is True,
               trajectory_available=False, sanitized_trajectory_exported=False,
               sanitized_native_session_exported=False, verifier_available=False,
               pod_evidence_complete=False, cleanup_confirmed=result.get('cleanup_confirmed') is True,
               packet_index_verified=False, execution_identity_verified=False,
               historical_packet=False, missing_evidence=result.get('missing_evidence', []), evidence_errors=[])
    packet = Path(results).parent / 'evidence' / request['job_id']
    if not packet.exists():
        if result:
            row['evidence_errors'].append('published_packet_missing')
        return row
    try:
        if packet.is_symlink() or packet.resolve() != packet.absolute():
            raise ValueError('published_packet_link_refused')
        def read(name):
            path = packet / name
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ValueError('published_file_link_refused')
            return path.read_bytes()
        raw = read('manifest.json')
        historical = result.get('historical_evidence_delivery') or {}
        historical_packet = not result.get('published_evidence_manifest_sha256') and bool(historical)
        expected_manifest = (historical.get('packet_manifest_sha256') if historical_packet
                             else result.get('published_evidence_manifest_sha256'))
        if hashlib.sha256(raw).hexdigest() != expected_manifest:
            raise ValueError('published_manifest_mismatch')
        manifest = json.loads(raw)
        raw_binding = read('execution-binding.json')
        if (result.get('execution_binding_sha256') and
                hashlib.sha256(raw_binding).hexdigest() != result['execution_binding_sha256']):
            raise ValueError('execution_binding_hash_mismatch')
        binding = json.loads(raw_binding)
        identity = binding.get('execution_identity') or {}
        run_id = binding.get('run_id')
        if historical_packet:
            # The historical recovery explicitly lacks actual Pod evidence.
            # Expose retained traces without inventing missing image/resources
            # or changing the historical scientific decision.
            identity_ok = (historical.get('source_snapshot_sha256') == request['snapshot_sha256']
                and binding.get('historical_job_id') == request['job_id']
                and run_id == 'tbs-universe-linfeng-queue-' + request['job_id']
                and bool(re.fullmatch('[a-f0-9]{64}', str(binding.get('source_report_sha256', '')))))
        else:
            identity_ok = (run_id == 'tbs-universe-linfeng-queue-' + request['job_id']
                and run_id == identity.get('run_id')
                and (result.get('run_id') is None or run_id == result['run_id'])
                and binding.get('snapshot_sha256') == request['snapshot_sha256']
                and bool(result.get('evidence_manifest_sha256'))
                and binding.get('evidence_manifest_sha256') == result['evidence_manifest_sha256'])
        if not identity_ok:
            raise ValueError('published_execution_identity_mismatch')
        if manifest.get('sanitized') is not True:
            raise ValueError('published_packet_not_sanitized')
        files = manifest['files']
        available = set()
        for name, info in files.items():
            path = Path(name)
            if not isinstance(name, str) or path.is_absolute() or '..' in path.parts:
                raise ValueError('published_file_path_refused')
            full = packet / path
            if full.is_symlink() or full.resolve() != full.absolute():
                raise ValueError('published_file_link_refused')
            if full.is_file() and full.stat().st_size == info.get('bytes'):
                available.add(name)
            else:
                row['evidence_errors'].append('published_file_missing_or_size_mismatch:' + name)
        row.update(run_id=run_id, evidence_packet='/evaluation/evidence/' + request['job_id'],
                   verifier_mode=identity.get('verifier_mode'),
                   observed_containers=observed_containers(binding),
                   cleanup_confirmed=row['cleanup_confirmed'] and binding.get('cleanup_confirmed') is True,
                   packet_index_verified=not row['evidence_errors'],
                   historical_packet=historical_packet, execution_identity_verified=not historical_packet,
                   published_manifest_sha256=hashlib.sha256(raw).hexdigest(),
                   execution_binding_sha256=hashlib.sha256(raw_binding).hexdigest())
        for kind in ('runtime', 'verifier'):
            image = (identity.get('images') or {}).get(kind + '_image')
            if isinstance(image, str) and re.search(r'@sha256:[a-f0-9]{64}$', image):
                row[kind + '_image'] = image
                row[kind + '_digest'] = image.rsplit('@', 1)[1]
        trajectory = manifest.get('trajectory_available') is True and 'agent/trajectory.json' in available
        native = [name for name in native_sessions(files) if name in available and files[name].get('bytes', 0) > 0]
        row.update(trajectory_available=trajectory, sanitized_trajectory_exported=trajectory,
                   sanitized_native_session_exported=bool(native),
                   # Old packets lack explicit retention flags but include
                   # nonempty source hashes from operator-owned publication.
                   raw_trajectory_retained=row['raw_trajectory_retained'] or
                       trajectory and bool(files['agent/trajectory.json'].get('source_sha256')),
                   native_session_retained=row['native_session_retained'] or any(
                       files[name].get('source_sha256') not in (None, hashlib.sha256(b'').hexdigest()) for name in native),
                   verifier_available=manifest.get('verifier_available') is True and
                       'verifier/test-stdout.txt' in available and
                       bool({'verifier/reward.txt', 'verifier/reward.json'} & available))
        observed = set()
        for container in row['observed_containers']:
            role = container['role']
            if (role in ('runtime', 'verifier') and container['image'] == row[role + '_image']
                    and row[role + '_image'] and container['image_id'] and container['node']
                    and all(container['resources'][kind].get('cpu') and container['resources'][kind].get('memory')
                            for kind in ('requests', 'limits'))):
                observed.add(role)
        if row['verifier_mode'] == 'shared' and 'runtime' in observed:
            observed.add('verifier')
        row['pod_evidence_complete'] = (result.get('pod_evidence_complete') is True
                                        and observed == {'runtime', 'verifier'})
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        # Never expose host-private paths or a malformed packet's contents.
        reason = str(error) if type(error) is ValueError and str(error).startswith(
            ('published_', 'execution_binding_')) else 'published_packet_unreadable'
        row['evidence_errors'].append(reason)
        row['packet_index_verified'] = False
    return row
