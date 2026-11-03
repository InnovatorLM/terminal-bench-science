"""Failure diagnostics use real subprocesses, no cloud or model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from build_command_logs import run_logged
from build_task_images import Docker
from evaluation_queue import W, digest_tree
from harbor_public_artifacts import publish_artifacts
from harbor_public_worker import execute


class BuildDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=W/'.tmp', prefix='public-build-diagnostic-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_stdout_stderr_and_exit_code_survive_failure(self):
        code = 'import sys; print("build output"*4000); print("first real error", file=sys.stderr); sys.exit(7)'
        result = run_logged([sys.executable, '-c', code], self.root/'build', timeout=5, cwd=self.root)
        self.assertEqual(result.returncode, 7)
        self.assertEqual((self.root/'build.stdout.log').read_text(), 'build output'*4000+'\n')
        self.assertEqual((self.root/'build.stderr.log').read_text(), 'first real error\n')
        record = json.loads((self.root/'build.json').read_text())
        self.assertEqual(record['returncode'], 7)
        self.assertEqual(record['cwd'], str(self.root))
        self.assertNotIn('env', record)
        for path in self.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_timeout_keeps_output_and_records_distinct_status(self):
        code = 'import time; print("download started", flush=True); time.sleep(5)'
        with self.assertRaises(subprocess.TimeoutExpired):
            run_logged([sys.executable, '-c', code], self.root/'build', timeout=.5)
        self.assertIn('download started', (self.root/'build.stdout.log').read_text())
        record = json.loads((self.root/'build.json').read_text())
        self.assertEqual(record['status'], 'timed_out')
        self.assertIsNone(record['returncode'])

    def test_docker_failure_records_exact_dockerfile_and_context(self):
        fake=self.root/'docker';fake.write_text('#!/bin/sh\nprintf "Step 1/3 : FROM example\\n"\nprintf "ERROR: context file missing\\n" >&2\nexit 17\n');fake.chmod(0o700)
        context=self.root/'context';context.mkdir();dockerfile=context/'Dockerfile';dockerfile.write_text('FROM scratch\n')
        docker=Docker(self.root);docker.env['PATH']=str(self.root)
        args=['build','-f',str(dockerfile),str(context)]
        with self.assertRaisesRegex(ValueError,'Docker step 1 failed'):docker.run(args)
        record=json.loads((self.root/'docker-01.json').read_text())
        self.assertEqual(record['command'],['docker',*args])
        self.assertEqual(record['returncode'],17)
        self.assertEqual(record['dockerfile'],str(dockerfile))
        self.assertEqual(record['context'],str(context))
        self.assertIn('Step 1/3',(self.root/'docker-01.stdout.log').read_text())
        self.assertIn('context file missing',(self.root/'docker-01.stderr.log').read_text())

    def test_failed_worker_publishes_diagnostics_without_changing_task(self):
        rid = 'harbor-'+'a'*32
        task = self.root/'source'/rid
        for name, value in {'task.toml':'schema_version="1.3"\n', 'instruction.md':'diagnostic test',
            'environment/Dockerfile':'FROM scratch\n', 'tests/test.sh':'#!/bin/sh\nexit 0\n',
            'solution/solve.sh':'#!/bin/sh\nexit 0\n'}.items():
            path=task/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
        original=digest_tree(task)
        (self.root/'request.json').write_text(json.dumps(dict(run_id=rid,task_name=rid,
            task_sha256=original,uploaded_task_sha256=original,reference_placeholder=False,attempts=1,created_at=1)))
        base=self.root/'base.json';base.write_text('{"environment":{}}')
        code='import sys; print("ERROR: fixture-build-failure password=hidden-test-secret",file=sys.stderr); sys.exit(3)'
        with patch('harbor_public_worker.credential_values',return_value=['hidden-test-secret']), \
             patch('harbor_public_worker.inspect_task',return_value={'schema_parsed':True}), \
             patch('harbor_public_worker.build_command',return_value=[sys.executable,'-c',code]):
            with self.assertRaises(subprocess.CalledProcessError):
                execute(self.root,{'base_template':str(base)})
        result=json.loads((self.root/'public.json').read_text())
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['trials'],[])
        self.assertIn('artifacts_url',result)
        self.assertEqual(result['build_diagnostics']['invocation']['returncode'],3)
        self.assertIn('fixture-build-failure',result['build_diagnostics']['first_error']['message'])
        self.assertEqual(digest_tree(task),original)
        self.assertIn('hidden-test-secret',(self.root/'build.stderr.log').read_text())
        with zipfile.ZipFile(self.root/'artifacts.zip') as archive:
            self.assertIn('diagnostics/build.stderr.log',archive.namelist())
            for name in archive.namelist():self.assertNotIn(b'hidden-test-secret',archive.read(name))
        self.assertNotIn('hidden-test-secret',json.dumps(result))

    def test_legacy_logs_and_separate_dockerfiles_are_preserved(self):
        build=self.root/'build'
        for context in ['environment-effective','task/tests']:
            p=build/context;p.mkdir(parents=True)
            (p/'Dockerfile').write_text('FROM scratch\n# '+context+'\n')
            (p/'.dockerignore').write_text('*\n!Dockerfile\n')
        (build/'docker-01.log').write_text('Error response from daemon: unknown:\n')
        (self.root/'build.log').write_text('legacy combined output\n')
        (self.root/'model-lease.json').write_text('{"token":"private-lease"}')
        (self.root/'template.json').write_text('{"key":"private-key"}')
        result={'run_id':'harbor-'+'a'*32,'status':'failed','trials':[]}
        publish_artifacts(self.root,result)
        self.assertFalse(result['build_diagnostics']['stdout_stderr_separate'])
        self.assertEqual(result['build_diagnostics']['command_capture'],'unavailable_for_legacy_run')
        with zipfile.ZipFile(self.root/'artifacts.zip') as archive:
            self.assertIn('diagnostics/build/task/tests/Dockerfile',archive.namelist())
            self.assertIn('diagnostics/build/environment-effective/.dockerignore',archive.namelist())
            self.assertEqual(archive.read('diagnostics/build/docker-01.log'),b'Error response from daemon: unknown:\n')
            self.assertFalse(any('template.json' in x or 'model-lease.json' in x for x in archive.namelist()))

    def test_secret_in_evidence_never_publishes_partial_zip(self):
        (self.root/'evidence').mkdir();(self.root/'evidence/trajectory.json').write_text('leaked-evidence-secret')
        with self.assertRaisesRegex(ValueError,'Credential detected'):
            publish_artifacts(self.root,{'run_id':'harbor-'+'a'*32},['leaked-evidence-secret'])
        self.assertFalse((self.root/'artifacts.zip').exists())
        self.assertFalse((self.root/'artifacts.zip.writing').exists())


if __name__ == '__main__':
    unittest.main()
