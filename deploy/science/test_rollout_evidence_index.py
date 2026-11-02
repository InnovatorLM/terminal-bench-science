"""Evidence delivery through real local packets; no cloud or LLM requests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation_queue import W, aggregate
from publish_evidence import publish
from role_evaluation_batch import requests, summary, install_clients
from rollout_evidence_index import entry
import test_execution_evidence as fixtures


class IndexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=W / '.tmp')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.results = self.root / 'evaluation/results'
        self.results.mkdir(parents=True)
        self.assignment = dict(key='a'*40+':tasks/example', generation=0,
                               evaluation_id='example', snapshot_sha256='b'*64)
        self.requests = requests(self.assignment)

    def packet(self, index=0, native=True, shared=False):
        request = self.requests[index]
        fixture = fixtures.EvidenceTests()
        config = fixture.config()
        config['job_name'] = 'tbs-universe-linfeng-queue-' + request['job_id']
        report = fixture.report(config)
        if shared:
            report['execution_identity']['verifier_mode'] = 'shared'
            report['pod_observations'] = {k:v for k,v in report['pod_observations'].items()
                                          if k.startswith('runtime:')}
        result_path, trial = fixture.fixture(self.root / 'private', config)
        if native:
            session = trial / 'agent/sessions/run.jsonl'
            session.parent.mkdir(parents=True)
            session.write_text('{"type":"message","text":"SECRET-CANARY"}\n')
        # Shared-mode config is built from a real task.toml in production;
        # this fixture supplies its already validated Pod receipt separately.
        result = dict(request, **aggregate(report, result_path, config,
                                           request['snapshot_sha256'], True))
        if shared:
            result['pod_evidence_complete'] = True
        packet = self.results.parent / 'evidence' / request['job_id']
        manifest = publish(trial, packet, ['SECRET-CANARY'])
        result['published_evidence_manifest_sha256'] = hashlib.sha256((packet/'manifest.json').read_bytes()).hexdigest()
        binding = dict(run_id=config['job_name'], snapshot_sha256=request['snapshot_sha256'],
                       evidence_manifest_sha256=result['evidence_manifest_sha256'],
                       execution_identity=report['execution_identity'],
                       pod_evidence=report['pod_observations'], cleanup_confirmed=True)
        (packet/'execution-binding.json').write_text(json.dumps(binding))
        result['execution_binding_sha256'] = hashlib.sha256((packet/'execution-binding.json').read_bytes()).hexdigest()
        (self.results/(request['job_id']+'.json')).write_text(json.dumps(result))
        return result, packet, manifest

    def test_five_runs_deliver_distinct_identity_and_actual_resources(self):
        for i in range(5):
            self.packet(i)
        batch = summary(self.assignment, self.results)
        index = batch['evidence_index']
        self.assertTrue(batch['independent_runs_verified'])
        self.assertEqual(len(index), 5)
        self.assertEqual(len({r['run_id'] for r in index}), 5)
        self.assertEqual(len({r['evidence_packet'] for r in index}), 5)
        for row in index:
            for field in ('packet_index_verified', 'raw_trajectory_retained', 'native_session_retained',
                          'trajectory_available', 'sanitized_trajectory_exported', 'sanitized_native_session_exported',
                          'verifier_available', 'pod_evidence_complete', 'cleanup_confirmed'):
                self.assertTrue(row[field], (field, row))
            self.assertEqual(row['runtime_digest'], 'sha256:'+'a'*64)
            self.assertEqual(row['verifier_digest'], 'sha256:'+'b'*64)
            self.assertEqual(len(row['observed_containers']), 2)
            for pod in row['observed_containers']:
                self.assertEqual(pod['resources']['requests'], {'cpu':'4','memory':'8Gi'})
                self.assertTrue(pod['node']); self.assertTrue(pod['image_id'])
        self.assertNotIn('SECRET-CANARY', json.dumps(batch))
        self.assertNotIn(str(self.root), json.dumps(batch))

    def test_retained_is_not_exported_and_query_does_not_read_trajectory(self):
        result, packet, manifest = self.packet()
        self.assertTrue(result['raw_trajectory_retained'])
        self.assertTrue(result['native_session_retained'])
        self.assertFalse(result['raw_trajectory_exported'])
        self.assertFalse(result['sanitized_trajectory_exported'])  # aggregate precedes publication
        self.assertTrue(manifest['sanitized_trajectory_exported'])
        self.assertTrue(manifest['sanitized_native_session_exported'])
        original = Path.read_bytes
        def read(path):
            self.assertNotIn('agent', path.parts, 'Status must not reload a multi-hour trace')
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            self.assertTrue(entry(self.requests[0], result, self.results)['packet_index_verified'])

    def test_missing_packet_native_or_verifier_is_explicit(self):
        result, packet, manifest = self.packet(native=False)
        first = entry(self.requests[0], result, self.results)
        self.assertFalse(first['native_session_retained'])
        self.assertFalse(first['sanitized_native_session_exported'])
        (packet/'verifier/test-stdout.txt').unlink()
        missing = entry(self.requests[0], result, self.results)
        self.assertFalse(missing['verifier_available'])
        self.assertFalse(missing['packet_index_verified'])
        self.assertTrue(missing['trajectory_available'])
        absent = entry(self.requests[1], {'raw_trajectory_retained':True}, self.results)
        self.assertTrue(absent['raw_trajectory_retained'])
        self.assertFalse(absent['trajectory_available'])
        self.assertEqual(absent['evidence_errors'], ['published_packet_missing'])
        batch = summary(self.assignment, self.results)
        self.assertEqual(len(batch['evidence_index']), 5)
        self.assertFalse(batch['evidence_index'][1]['packet_index_verified'])

    def test_changed_manifest_or_binding_cannot_claim_a_complete_packet(self):
        result, packet, _ = self.packet()
        manifest = packet/'manifest.json';saved = manifest.read_bytes()
        manifest.write_text('{}')
        self.assertEqual(entry(self.requests[0], result, self.results)['evidence_errors'], ['published_manifest_mismatch'])
        manifest.write_bytes(saved)
        binding = packet/'execution-binding.json'
        data = json.loads(binding.read_text());data['run_id'] = 'foreign'
        binding.write_text(json.dumps(data))
        self.assertEqual(entry(self.requests[0], result, self.results)['evidence_errors'], ['execution_binding_hash_mismatch'])
        result.pop('execution_binding_sha256')  # Historical packets still check version/run binding.
        row = entry(self.requests[0], result, self.results)
        self.assertFalse(row['trajectory_available'])
        self.assertIn('published_execution_identity_mismatch', row['evidence_errors'])

    def test_legacy_packet_derives_native_retention_from_source_hash(self):
        result, packet, _ = self.packet()
        for key in ('raw_trajectory_retained', 'native_session_retained', 'run_id', 'execution_binding_sha256'):
            result.pop(key, None)
        manifest = json.loads((packet/'manifest.json').read_text())
        for key in ('raw_trajectory_retained', 'native_session_retained', 'sanitized_trajectory_exported', 'sanitized_native_session_exported'):
            manifest.pop(key, None)
        (packet/'manifest.json').write_text(json.dumps(manifest))
        result['published_evidence_manifest_sha256'] = hashlib.sha256((packet/'manifest.json').read_bytes()).hexdigest()
        row = entry(self.requests[0], result, self.results)
        self.assertTrue(row['packet_index_verified']);self.assertTrue(row['native_session_retained'])

    def test_shared_verifier_does_not_invent_a_verifier_pod(self):
        result, packet, _ = self.packet(shared=True)
        row = entry(self.requests[0], result, self.results)
        self.assertTrue(row['pod_evidence_complete'])
        self.assertEqual(row['verifier_mode'], 'shared')
        self.assertEqual([p['role'] for p in row['observed_containers']], ['runtime'])

    def test_historical_delivery_exposes_retained_traces_but_never_invents_pods(self):
        result, packet, _ = self.packet()
        result['historical_evidence_delivery'] = dict(source_snapshot_sha256=self.requests[0]['snapshot_sha256'],
            packet_manifest_sha256=result.pop('published_evidence_manifest_sha256'))
        for key in ('execution_binding_sha256', 'evidence_manifest_sha256', 'run_id', 'native_session_retained'):
            result.pop(key, None)
        result.update(pod_evidence_complete=False, evidence_complete=False,
                      missing_evidence=['historical_actual_pod_observations'])
        (packet/'execution-binding.json').write_text(json.dumps(dict(
            run_id='tbs-universe-linfeng-queue-'+self.requests[0]['job_id'],
            historical_job_id=self.requests[0]['job_id'], source_report_sha256='f'*64,
            cleanup_confirmed=True, actual_pod_evidence_available=False)))
        row = entry(self.requests[0], result, self.results)
        self.assertTrue(row['packet_index_verified']);self.assertTrue(row['historical_packet'])
        self.assertTrue(row['trajectory_available']);self.assertTrue(row['native_session_retained'])
        self.assertFalse(row['execution_identity_verified']);self.assertFalse(row['pod_evidence_complete'])
        self.assertIsNone(row['runtime_digest']);self.assertEqual(row['observed_containers'], [])
        self.assertEqual(row['missing_evidence'], ['historical_actual_pod_observations'])
        result['historical_evidence_delivery']['source_snapshot_sha256'] = 'e'*64
        self.assertFalse(entry(self.requests[0], result, self.results)['packet_index_verified'])

    def test_file_link_is_refused(self):
        result, packet, _ = self.packet()
        trace = packet/'agent/trajectory.json';trace.unlink();trace.symlink_to(packet/'result.json')
        row = entry(self.requests[0], result, self.results)
        self.assertFalse(row['packet_index_verified'])
        self.assertIn('published_file_link_refused', row['evidence_errors'])

    def test_copying_one_execution_into_five_job_packets_is_not_five_trials(self):
        import shutil
        result, packet, _ = self.packet()
        for request in self.requests[1:]:
            shutil.copytree(packet, packet.parent/request['job_id'])
            (self.results/(request['job_id']+'.json')).write_text(json.dumps(dict(result, **request)))
        foreign = entry(self.requests[1], dict(result, **self.requests[1]), self.results)
        self.assertFalse(foreign['execution_identity_verified'])
        self.assertIn('published_execution_identity_mismatch', foreign['evidence_errors'])
        batch = summary(self.assignment, self.results)
        self.assertFalse(batch['complete']);self.assertFalse(batch['independent_runs_verified'])
        self.assertEqual(batch['valid_trials'], 0)
        self.assertTrue(all(row['evidence_status']=='duplicate_run_identity' for row in batch['jobs']))

    def test_installed_role_client_can_build_status_with_only_its_bundle(self):
        import os, subprocess, sys
        role = self.root/'role'; (role/'usr/local/bin').mkdir(parents=True)
        install_clients({'roles':{'evaluator-01':{'root':str(role)}}})
        script = 'import role_evaluation_batch as b; import json; print(json.dumps(b.summary('+repr(self.assignment)+','+repr(str(self.results))+')))'
        result = subprocess.run([sys.executable, '-c', script], cwd=role/'usr/local/bin',
                                env={**os.environ, 'PYTHONPATH':'', 'PYTHONDONTWRITEBYTECODE':'1'},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['evidence_index']), 5)


if __name__ == '__main__':
    unittest.main()
