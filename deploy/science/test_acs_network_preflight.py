"""Real Harbor resolver/capabilities; no images, model calls or cloud clients."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from harbor.models.task.config import TaskConfig
from harbor.models.trial.config import VerifierConfig as RuntimeVerifierConfig
from acs_network_preflight import inspect_network, require_compatible, NetworkPreflightError
from check_harbor_task import inspect_task
from evaluation_queue import W, digest_tree
from universe_ack import UniverseACK
import author_selftest_worker as worker


class NetworkPreflightTests(unittest.TestCase):
    def task(self, text=''):
        return TaskConfig.model_validate_toml('schema_version="1.3"\n' + text)

    def fixture(self, root, text):
        files = {'task.toml': text, 'instruction.md': 'Write the result.',
                 'environment/Dockerfile': 'FROM scratch\n',
                 'tests/test.sh': '#!/bin/sh\nexit 0\n',
                 'solution/solve.sh': '#!/bin/sh\nexit 0\n'}
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return root

    def test_public_default_shared_passes_without_adapter_initialization(self):
        task = self.task()
        before = task.model_dump_json()
        with patch.object(UniverseACK, '__init__', side_effect=AssertionError('No cloud constructor')) as constructor:
            report = require_compatible(task)
        constructor.assert_not_called()
        self.assertTrue(report['compatible'])
        self.assertFalse(report['cloud_clients_created'])
        self.assertEqual(report['plans'][0]['verifier_mode'], 'shared')
        self.assertEqual(report['plans'][0]['agent_env_baseline']['network_mode'], 'public')
        self.assertEqual(task.model_dump_json(), before)

    def test_actual_runtime_schema_ignores_mode_but_source_task_controls_it(self):
        runtime = RuntimeVerifierConfig.model_validate({'environment_mode': 'separate', 'environment': {}})
        self.assertNotIn('environment_mode', runtime.model_dump())
        self.assertNotIn('environment', runtime.model_dump())
        self.assertEqual(require_compatible(self.task())['plans'][0]['verifier_mode'], 'shared')
        for text in ('artifacts=["/app/result.json"]\n[verifier]\nenvironment_mode="separate"\n',
                     'artifacts=["/app/result.json"]\n[verifier.environment]\nnetwork_mode="public"\n'):
            with self.subTest(text=text):
                report = require_compatible(self.task(text))
                self.assertEqual(report['plans'][0]['verifier_mode'], 'separate')
                self.assertEqual(report['plans'][0]['artifact_transfer']['declared_entries'], 1)
                self.assertFalse(report['plans'][0]['artifact_transfer']['contents_validated'])

    def test_shared_phase_switch_reports_both_unsupported_policy_and_switch(self):
        task = self.task('[verifier]\nnetwork_mode="no-network"\n')
        report = inspect_network(task)
        self.assertFalse(report['compatible'])
        self.assertEqual({i['code'] for i in report['issues']},
                         {'acs_static_network_policy_unsupported', 'acs_dynamic_network_policy_unsupported'})
        self.assertTrue(all('[verifier]' in i['location'] for i in report['issues']))
        with self.assertRaises(NetworkPreflightError) as caught:
            require_compatible(task)
        self.assertEqual(caught.exception.report, report)
        self.assertIn('task.toml', str(caught.exception))

    def test_separate_is_not_a_workaround_for_unsupported_static_isolation(self):
        report = inspect_network(self.task('[verifier]\nenvironment_mode="separate"\nnetwork_mode="no-network"\n'
                                           '[verifier.environment]\nnetwork_mode="no-network"\n'))
        self.assertEqual(report['plans'][0]['verifier_mode'], 'separate')
        self.assertFalse(report['compatible'])
        self.assertEqual({i['code'] for i in report['issues']}, {'acs_static_network_policy_unsupported'})

    def test_supported_job_allowlists_are_resolved_but_host_values_not_exported(self):
        report = inspect_network(self.task('[environment]\nnetwork_mode="no-network"\n'),
                                 environment={'type': 'ack', 'extra_allowed_hosts': ['internal.example']})
        self.assertEqual(report['plans'][0]['agent_env_baseline'],
                         {'network_mode': 'allowlist', 'allowed_hosts_count': 1})
        self.assertFalse(report['compatible'])
        self.assertNotIn('internal.example', json.dumps(report))

    def test_actual_legacy_flag_and_step_inheritance_are_checked(self):
        with self.assertWarns(DeprecationWarning):
            legacy = self.task('[environment]\nallow_internet=false\n')
        self.assertFalse(inspect_network(legacy)['compatible'])
        task = self.task('[verifier]\nenvironment_mode="separate"\n'
                         '[[steps]]\nname="first"\n'
                         '[[steps]]\nname="second"\n[steps.verifier]\nenvironment_mode="shared"\nnetwork_mode="no-network"\n')
        report = inspect_network(task)
        self.assertEqual([p['verifier_mode'] for p in report['plans']], ['separate', 'shared'])
        self.assertTrue(all('second' in item['location'] for item in report['issues']))

    def test_mechanical_preflight_does_not_modify_any_frozen_task_bytes(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            task = self.fixture(Path(raw) / 'task', '[verifier]\nenvironment_mode="separate"\n')
            before = digest_tree(task)
            report = inspect_task(task, acs_environment={'type': 'ack'})
            self.assertTrue(report['acs_network_preflight']['compatible'])
            self.assertEqual(report['snapshot_sha256'], before)
            self.assertEqual(digest_tree(task), before)
            (task / 'task.toml').write_text('[verifier]\nnetwork_mode="no-network"\n')
            before = digest_tree(task)
            with self.assertRaises(NetworkPreflightError):
                inspect_task(task, acs_environment={'type': 'ack'})
            self.assertEqual(digest_tree(task), before)

    def test_real_upload_worker_rejects_before_builder_and_preserves_source(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root = Path(raw)
            template = root / '.config/harbor-api/template.json'
            template.parent.mkdir(parents=True)
            template.write_text(json.dumps({'environment': {'type': 'ack'}}))
            source = self.fixture(root / 'candidate', '[verifier]\nnetwork_mode="no-network"\n')
            source_hash = digest_tree(source)
            role = root / 'role'
            inbox = role / 'selftest/inbox'
            inbox.mkdir(parents=True)
            (role / 'selftest/results').mkdir()
            job = 'a' * 32
            archive = inbox / (job + '.tar')
            with tarfile.open(archive, 'w') as bundle:
                for path in source.rglob('*'):
                    bundle.add(path, arcname=str(path.relative_to(source)), recursive=False)
            request = inbox / (job + '.json')
            request.write_text(json.dumps({'job_id': job, 'task_name': 'example', 'source_commit': 'b' * 40,
                                           'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}))
            entry = {'root': str(role), 'uid': os.getuid(), 'gid': os.getgid()}
            state = root / 'state'
            state.mkdir()
            with patch.object(worker, 'W', root), patch.object(worker, 'STATE', state), \
                    patch.object(worker, 'credential_values', return_value=[]), \
                    patch.object(worker, 'builder_slot') as builder, patch.object(worker, 'owned_run') as runner:
                worker.process('author-01', entry, request)
            builder.assert_not_called()
            runner.assert_not_called()
            receipt = json.loads((role / 'selftest/results' / (job + '.json')).read_text())
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['error_class'], 'task_acs_network_incompatible')
            self.assertEqual(receipt['stage'], 'network_preflight')
            self.assertFalse(receipt['cloud_execution_started'])
            self.assertFalse(receipt['network_preflight']['compatible'])
            self.assertTrue((state / job / 'network-preflight.json').is_file())
            self.assertEqual(digest_tree(state / job / 'source/example'), source_hash)
            self.assertEqual(digest_tree(source), source_hash)


if __name__ == '__main__':
    unittest.main()
