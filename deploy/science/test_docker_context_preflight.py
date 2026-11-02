"""Detect omitted frozen build inputs before acquiring a builder or running ACS."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation_queue import W, digest_tree
from docker_context_preflight import require_contexts, DockerContextError
from check_harbor_task import inspect_task


class DockerContextTests(unittest.TestCase):
    def task(self, root):
        values = {'instruction.md': 'Produce result.json.', 'task.toml': 'schema_version="1.0"\n',
                  'environment/Dockerfile': 'FROM scratch\n', 'tests/Dockerfile': 'FROM scratch\n',
                  'tests/test.sh': '#!/bin/bash\nexit 0\n', 'solution/solve.sh': '#!/bin/bash\nexit 0\n'}
        for name, value in values.items():
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)

    def test_missing_verifier_wheels_is_found_before_running_any_candidate_code(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root = Path(raw); self.task(root)
            (root / 'tests/wheels.py').write_text('raise SystemExit("never run")')
            (root / 'tests/wheel-digests.json').write_text('{}')
            (root / 'tests/Dockerfile').write_text('FROM scratch\nCOPY wheel-digests.json wheels.py /opt/wheels/\nCOPY wheels /opt/wheels/wheels\nRUN python3 /opt/wheels/wheels.py\n')
            before = digest_tree(root)
            with patch('subprocess.run', side_effect=AssertionError('must fail before execution')), self.assertRaises(DockerContextError) as caught:
                inspect_task(root)
            self.assertEqual(caught.exception.report['missing_sources'], [dict(dockerfile='tests/Dockerfile', line=3, source='wheels')])
            self.assertEqual(digest_tree(root), before)
            (root / 'tests/wheels').mkdir(); (root / 'tests/wheels/numpy.whl').write_text('fixture')
            self.assertFalse(inspect_task(root)['docker_context_preflight']['missing_sources'])

    def test_json_spaces_flags_multiline_and_context_roots(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root = Path(raw); self.task(root)
            (root / 'environment/first file').write_text('x'); (root / 'environment/second').write_text('y')
            (root / 'environment/Dockerfile').write_text('FROM scratch\nCOPY --chown=0:0 ["first file", "second", "/app/"]\nCOPY --chmod=0755 \\\n  second /app/second\nCOPY --from=builder /out/not-a-local-file /app/\nADD https://example.invalid/data /data\n')
            report = require_contexts(root)
            self.assertEqual(len(report['contexts'][0]['checked_sources']), 3)
            self.assertFalse(report['missing_sources'])
            # Having wheels in the agent context cannot satisfy the verifier.
            (root / 'environment/wheels').mkdir()
            (root / 'tests/Dockerfile').write_text('FROM scratch\nCOPY wheels /wheels\n')
            with self.assertRaises(DockerContextError): require_contexts(root)

    def test_dynamic_paths_and_frontend_syntax_are_explicitly_deferred(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root = Path(raw); self.task(root)
            (root / 'environment/Dockerfile').write_text('FROM scratch\nARG DATA=data\nCOPY ${DATA} /app/\nCOPY *.whl /app/\nCOPY --unknown-option=true maybe /app/\n')
            (root / 'environment/.dockerignore').write_text('data\n')
            (root / 'tests/Dockerfile').write_text('FROM scratch\nRUN <<EOF\nCOPY this-is-shell-data /tmp/file\nEOF\n')
            report = require_contexts(root)
            self.assertEqual(report['contexts'][0]['checked_sources'], [])
            self.assertEqual(len(report['contexts'][0]['deferred']), 4)
            self.assertEqual(report['contexts'][1]['deferred'][0]['reason'], 'frontend_escape_or_heredoc')
            self.assertFalse(report['runtime_verified'])

    def test_missing_or_empty_verifier_entry_fails_without_task_execution(self):
        for content in (None, ''):
            with self.subTest(content=content), tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
                root=Path(raw);self.task(root);entry=root/'tests/test.sh'
                if content is None:entry.unlink()
                else:entry.write_text(content)
                with patch('subprocess.run',side_effect=AssertionError('must not execute')), self.assertRaises(DockerContextError) as caught:
                    require_contexts(root)
                self.assertEqual(caught.exception.report['missing_files'],[
                    {'path':'tests/test.sh','reason':'missing_or_empty_required_file'}])
                self.assertEqual(caught.exception.report['failure_owner'],'task')

    def test_declared_verifier_environment_requires_its_definition(self):
        for content in (None, ''):
            with self.subTest(content=content), tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
                root=Path(raw);self.task(root)
                (root/'task.toml').write_text('schema_version="1.0"\n[verifier.environment]\ncpus=2\n')
                if content is None:(root/'tests/Dockerfile').unlink()
                else:(root/'tests/Dockerfile').write_text(content)
                with self.assertRaises(DockerContextError) as caught:require_contexts(root)
                self.assertEqual(caught.exception.report['missing_files'][0]['path'],'tests/Dockerfile')

    def test_shared_and_inherited_separate_keep_no_dockerfile_fallback(self):
        for mode in ('shared','separate'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
                root=Path(raw);self.task(root);(root/'tests/Dockerfile').unlink()
                (root/'task.toml').write_text(f'schema_version="1.0"\n[verifier]\nenvironment_mode="{mode}"\n')
                report=require_contexts(root)
                self.assertEqual(report['verifier_build']['source'],'inherited_agent_image')
                self.assertEqual(report['verifier_build']['mode'],mode)
                self.assertFalse(report['missing_files'])

    def test_declared_image_wins_over_unused_dockerfile_and_its_copy_inputs(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root=Path(raw);self.task(root)
            (root/'task.toml').write_text('schema_version="1.0"\n[verifier.environment]\ndocker_image="registry.example/verifier:1"\n')
            (root/'tests/Dockerfile').write_text('FROM scratch\nCOPY not-in-this-snapshot /missing\n')
            report=require_contexts(root)
            self.assertEqual(report['verifier_build']['source'],'declared_image')
            self.assertEqual(report['verifier_build']['image'],'registry.example/verifier:1')
            self.assertFalse(report['verifier_build']['dockerfile_used'])
            self.assertFalse(report['missing_sources'])
            (root/'tests/Dockerfile').unlink()
            self.assertEqual(require_contexts(root)['verifier_build']['source'],'declared_image')

    def test_dockerfile_is_selected_without_a_declared_image(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root=Path(raw);self.task(root)
            (root/'task.toml').write_text('schema_version="1.0"\n[verifier.environment]\ncpus=2\n')
            plan=require_contexts(root)['verifier_build']
            self.assertEqual(plan['source'],'dockerfile')
            self.assertTrue(plan['dockerfile_used'])


if __name__ == '__main__': unittest.main()
