"""Legacy receipt refresh from local evidence; never start a model/cloud process."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from evaluation_queue import W, aggregate, digest_tree
from execution_evidence import inspect
from evidence_files import digest_file
from publish_evidence import publish
import refresh_evidence_receipt as module
from rollout_evidence_index import entry
import test_execution_evidence as fixtures


class RefreshReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=W / '.tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.f = self.fixture(self.root / 'complete')
        no_process = patch('subprocess.Popen', side_effect=AssertionError('No model/cloud process permitted'))
        no_process.start(); self.addCleanup(no_process.stop)

    def fixture(self, root, partial=False):
        root.mkdir(); jobs = root / 'jobs'; jobs.mkdir()
        job_id = 'a' * 32; job = jobs / job_id; job.mkdir()
        results = root / 'role/evaluation/results'; results.mkdir(parents=True)
        snapshot = root / 'task'; snapshot.mkdir()
        (snapshot / 'instruction.md').write_text('Frozen fixture task.\n')
        (snapshot / 'task.toml').write_text('version="1.0"\n[verifier]\ntimeout_sec=600\n')
        request = dict(job_id=job_id, evaluation_id='fixture-task', snapshot_sha256=digest_tree(snapshot))
        fixture_source = fixtures.EvidenceTests(); actual = fixture_source.config()
        actual['job_name'] = 'tbs-universe-linfeng-queue-' + job_id
        actual['datasets'] = [{'path': str(snapshot.parent), 'task_names': [snapshot.name]}]
        actual['agents'][0]['model_name'] = 'openai/deepseek-v4.1-flash-1017'
        result_path, trial = fixture_source.fixture(job / 'runs', actual)
        native = trial / 'agent/sessions/day/run.jsonl'; native.parent.mkdir(parents=True)
        native.write_text('{"event":"SECRET-CANARY"}\n')
        (trial / 'trial.log').write_text('fixture diagnostic\n')
        report = fixture_source.report(actual)
        if partial:
            (trial / 'agent/trajectory.json').unlink()
            for path in (trial / 'verifier').iterdir(): path.unlink()
            (trial / 'result.json').unlink()
            report.update(error_type='TimeoutError', stats={}, harbor_exit_code=None)
            report['cleanup'] = {'confirmed': False}
        evidence = inspect(report, result_path, actual, request['snapshot_sha256'], True)
        result = dict(request, provider='dawnfm', model='deepseek-v4.1-flash-1017',
                      scientific_acceptance=False, **aggregate(report, result_path, actual, request['snapshot_sha256'], True))
        for key in module.FIELDS:
            result.pop(key, None)
        result['raw_trajectory_exported'] = True  # The ambiguous legacy field.
        packet = results.parent / 'evidence' / job_id
        manifest = publish(trial, packet, ['SECRET-CANARY'])
        for key in ('raw_trajectory_retained', 'native_session_retained',
                    'sanitized_trajectory_exported', 'sanitized_native_session_exported'):
            manifest.pop(key, None)
        (packet / 'manifest.json').write_text(json.dumps(manifest))
        binding = dict(run_id=actual['job_name'], snapshot_sha256=request['snapshot_sha256'],
                       evidence_manifest_sha256=result['evidence_manifest_sha256'],
                       execution_identity=report['execution_identity'],
                       pod_evidence=report.get('pod_observations', {}),
                       cleanup_confirmed=result['cleanup_confirmed'])
        (packet / 'execution-binding.json').write_text(json.dumps(binding))
        for name, value in [('claim.json', dict(request, mock=False)), ('config.json', actual),
                            ('report.json', report), ('evidence-manifest.json', evidence), ('result.json', result)]:
            (job / name).write_text(json.dumps(value)); (job / name).chmod(0o600)
        private, public = job / 'result.json', results / (job_id + '.json')
        public.write_text(json.dumps(result, indent=2)); public.chmod(0o640)
        template = root / 'template.json'; template.write_text(json.dumps(actual))
        registry = root / 'registry.json'
        registry.write_text(json.dumps({'fixture-task': dict(snapshot=str(snapshot),
            snapshot_sha256=request['snapshot_sha256'], template=str(template),
            template_sha256=hashlib.sha256(template.read_bytes()).hexdigest())}))
        config = dict(state=str(jobs), results=str(results), registry=str(registry), evaluator_uid=os.getgid())
        return dict(root=root, job=job, job_id=job_id, config=config, private=private, public=public,
                    packet=packet, trial=trial, native=native, snapshot=snapshot,
                    result=result, request=request, evidence=evidence)

    def write_result(self, result, fixture=None):
        f = fixture or self.f
        for path in (f['private'], f['public']): path.write_text(json.dumps(result))

    def protected_hashes(self, f):
        return {path: digest_file(path, f['root']) for root in (f['job'], f['packet'])
                for path in root.rglob('*') if path.is_file() and path != f['private']}

    def test_preview_apply_idempotence_and_original_evidence_preservation(self):
        f = self.f; old_private = f['private'].read_bytes(); old_public = f['public'].read_bytes()
        before = self.protected_hashes(f)
        preview = module.refresh(f['config'], f['job_id'])
        self.assertEqual(preview['outcome'], 'would_refresh')
        self.assertEqual(f['private'].read_bytes(), old_private)
        self.assertFalse((f['job'] / module.TRANSACTION).exists())
        # The new schema must not change the original evidence-manifest hash.
        with patch('execution_evidence.inspect', side_effect=AssertionError('Do not re-inspect old evidence')):
            result = module.refresh(f['config'], f['job_id'], apply=True)
        self.assertEqual(result['outcome'], 'refreshed')
        receipt = json.loads(f['private'].read_bytes())
        self.assertEqual(receipt, json.loads(f['public'].read_bytes()))
        for key, value in f['result'].items():
            if key not in module.FIELDS: self.assertEqual(receipt[key], value)
        self.assertFalse(receipt['raw_trajectory_exported'])
        self.assertTrue(receipt['raw_trajectory_retained'])
        self.assertTrue(receipt['native_session_retained'])
        self.assertTrue(receipt['sanitized_native_session_exported'])
        self.assertEqual(receipt['evidence_manifest_sha256'], f['result']['evidence_manifest_sha256'])
        for path, checksum in before.items(): self.assertEqual(digest_file(path, f['root']), checksum)
        backup = f['job'] / module.TRANSACTION
        self.assertEqual((backup / 'private.before.json').read_bytes(), old_private)
        self.assertEqual((backup / 'public.before.json').read_bytes(), old_public)
        for path in backup.iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(f['public'].stat().st_mode & 0o777, 0o640)
        self.assertEqual(module.refresh(f['config'], f['job_id'], apply=True)['outcome'], 'unchanged')
        indexed = entry(f['request'], receipt, f['public'].parent)
        self.assertTrue(indexed['packet_index_verified']); self.assertTrue(indexed['trajectory_available'])

    def test_partial_timeout_can_publish_index_without_score_or_missing_evidence_upgrade(self):
        f = self.fixture(self.root / 'partial', partial=True)
        result = module.refresh(f['config'], f['job_id'], apply=True)
        self.assertEqual(result['outcome'], 'refreshed')
        receipt = json.loads(f['private'].read_text())
        self.assertFalse(receipt['raw_trajectory_retained'])
        self.assertTrue(receipt['native_session_retained'])
        self.assertTrue(receipt['sanitized_native_session_exported'])
        self.assertFalse(receipt['evidence_complete']); self.assertIsNone(receipt['reward'])
        self.assertFalse(receipt['cleanup_confirmed']); self.assertFalse(receipt['scientific_acceptance'])
        self.assertEqual(receipt['status'], f['result']['status'])
        self.assertEqual(receipt['missing_evidence'], f['result']['missing_evidence'])

    def test_active_jobs_and_historical_delivery_are_skipped(self):
        f = self.f; original = f['private'].read_bytes()
        claim = f['job'] / 'claim.json'; before = claim.read_bytes()
        claim.write_text(json.dumps(dict(f['request'], mock=False, controller_pid=os.getpid())))
        self.assertEqual(module.refresh(f['config'], f['job_id'], apply=True)['outcome'], 'skipped_active')
        claim.write_bytes(before)
        with patch.object(module, 'active_queue_worker', return_value=True):
            self.assertEqual(module.refresh(f['config'], f['job_id'], apply=True)['outcome'], 'skipped_active')
        self.assertEqual(f['private'].read_bytes(), original)
        result = dict(f['result'], historical_evidence_delivery={'source_snapshot_sha256': f['request']['snapshot_sha256']})
        self.write_result(result); (f['packet'] / 'manifest.json').unlink()
        self.assertEqual(module.refresh(f['config'], f['job_id'], apply=True)['outcome'], 'skipped_historical_delivery')
        self.assertEqual(json.loads(f['private'].read_text()), result)

    def test_private_public_disagreement_and_changed_snapshot_refuse_mutation(self):
        f = self.f
        f['public'].write_text(json.dumps(dict(f['result'], reward=0)))
        with self.assertRaisesRegex(ValueError, 'private_public_result_mismatch'):
            module.refresh(f['config'], f['job_id'], apply=True)
        self.write_result(f['result']); (f['snapshot'] / 'instruction.md').write_text('Changed frozen source')
        with self.assertRaisesRegex(ValueError, 'frozen_source_changed'):
            module.refresh(f['config'], f['job_id'], apply=True)
        self.assertFalse((f['job'] / module.TRANSACTION).exists())

    def test_existing_published_hashes_cannot_be_replaced(self):
        for key in ('published_evidence_manifest_sha256', 'execution_binding_sha256'):
            with self.subTest(key=key):
                self.write_result(dict(self.f['result'], **{key: 'f' * 64}))
                with self.assertRaisesRegex(ValueError, 'existing_' + key + '_mismatch'):
                    module.refresh(self.f['config'], self.f['job_id'], apply=True)
                self.assertEqual(json.loads(self.f['private'].read_text())[key], 'f' * 64)
        self.assertFalse((self.f['job'] / module.TRANSACTION).exists())

    def test_original_and_published_content_hashes_and_source_link_are_verified(self):
        f = self.f; original = f['native'].read_bytes()
        f['native'].write_bytes(original.replace(b'event', b'other'))
        with self.assertRaisesRegex(ValueError, 'original_file_hash_mismatch'):
            module.refresh(f['config'], f['job_id'], apply=True)
        f['native'].write_bytes(original)
        published = f['packet'] / 'agent/sessions/day/run.jsonl'; saved = published.read_bytes()
        published.write_bytes(saved.replace(b'event', b'other'))
        with self.assertRaisesRegex(ValueError, 'published_file_hash_mismatch'):
            module.refresh(f['config'], f['job_id'], apply=True)
        published.write_bytes(saved)
        path = f['packet'] / 'manifest.json'; manifest = json.loads(path.read_text())
        manifest['files']['config.json']['source_sha256'] = 'e' * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'published_source_hash_mismatch'):
            module.refresh(f['config'], f['job_id'], apply=True)
        self.assertFalse((f['job'] / module.TRANSACTION).exists())

    def test_report_binding_or_original_manifest_disagreement_is_refused(self):
        f = self.f
        for path, key, value, reason in [
            (f['job'] / 'evidence-manifest.json', 'complete', False, 'original_manifest_mismatch'),
            (f['job'] / 'config.json', 'job_name', 'foreign', 'run_identity_mismatch'),
            (f['packet'] / 'execution-binding.json', 'cleanup_confirmed', False, 'published_execution_binding_mismatch')]:
            with self.subTest(path=path.name):
                original = path.read_bytes(); data = json.loads(original); data[key] = value
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, reason): module.refresh(f['config'], f['job_id'], apply=True)
                path.write_bytes(original)
        self.assertFalse((f['job'] / module.TRANSACTION).exists())

    def test_missing_original_and_packet_symlink_are_refused_without_promoting_retention(self):
        f = self.f; raw = f['native'].read_bytes(); f['native'].unlink()
        with self.assertRaises(OSError): module.refresh(f['config'], f['job_id'], apply=True)
        f['native'].write_bytes(raw)
        published = f['packet'] / 'agent/sessions/day/run.jsonl'; published.unlink(); published.symlink_to(f['native'])
        with self.assertRaisesRegex(ValueError, 'linked_operator_file'):
            module.refresh(f['config'], f['job_id'], apply=True)
        self.assertFalse((f['job'] / module.TRANSACTION).exists())

    def interrupt_public_write(self):
        original_replace = module.replace_bytes; calls = []
        def replace(path, raw, mode, gid):
            calls.append(path)
            if path == self.f['public']: raise OSError('fixture interruption')
            return original_replace(path, raw, mode, gid)
        with patch.object(module, 'replace_bytes', side_effect=replace):
            with self.assertRaisesRegex(OSError, 'fixture interruption'):
                module.refresh(self.f['config'], self.f['job_id'], apply=True)
        self.assertEqual(calls, [self.f['private'], self.f['public']])

    def test_interrupted_pair_write_resumes_from_verified_backup(self):
        f = self.f; before = f['public'].read_bytes()
        self.interrupt_public_write()
        self.assertEqual(f['public'].read_bytes(), before)
        result = module.refresh(f['config'], f['job_id'], apply=True)
        self.assertEqual(result['outcome'], 'refreshed')
        self.assertEqual(f['private'].read_bytes(), f['public'].read_bytes())
        self.assertEqual(module.refresh(f['config'], f['job_id'], apply=True)['outcome'], 'unchanged')

    def test_interrupted_transaction_never_overwrites_a_new_score(self):
        self.interrupt_public_write()
        changed = dict(self.f['result'], reward=0)
        self.f['public'].write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'refresh_transaction_result_conflict'):
            module.refresh(self.f['config'], self.f['job_id'], apply=True)
        self.assertEqual(json.loads(self.f['public'].read_text())['reward'], 0)

    def test_large_agent_files_are_hashed_by_streaming(self):
        original_read = Path.read_bytes
        def read(path):
            if 'agent' in path.parts:
                raise AssertionError('Do not load large agent evidence into memory')
            return original_read(path)
        with patch.object(Path, 'read_bytes', read):
            self.assertEqual(module.refresh(self.f['config'], self.f['job_id'])['outcome'], 'would_refresh')

    def test_cli_defaults_to_preview_and_refuses_ambiguous_result_queues(self):
        f = self.f; config = self.root / 'queue-config.json'
        config.write_text(json.dumps(dict(f['config'], role_queues=[{'results': f['config']['results']}])))
        argv = ['refresh_evidence_receipt', '--queue-config', str(config), '--job-id', f['job_id']]
        old = f['private'].read_bytes(); output = io.StringIO()
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(output):
            self.assertEqual(module.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['outcome'], 'would_refresh')
        self.assertEqual(f['private'].read_bytes(), old)
        config.write_text(json.dumps(dict(f['config'], role_queues=[{'results': f['config']['results']}] * 2)))
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(output):
            self.assertEqual(module.main(), 1)
        self.assertEqual(json.loads(output.getvalue())['reason'], 'require_one_existing_result_queue')


if __name__ == '__main__':
    unittest.main()
