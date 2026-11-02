import asyncio
import base64
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from evaluation_queue import W
from universe_ack import UniverseACK


class VerifierExec:
    """Execute the actual transport Python locally, mapping only its remote root."""
    def __init__(self, root, payload=None, interrupt_chunk=None):
        self.root=Path(root);self.chunks=0;self.interrupt_chunk=interrupt_chunk
        self.override=None
        if payload is not None:
            self.root.mkdir(parents=True,exist_ok=True)
            invalid=any(Path(name).is_absolute() or '..' in Path(name).parts or str(Path(name))!=name
                        or any(str(parent) in payload for parent in Path(name).parents) for name in payload)
            if invalid:
                self.override={name:dict(source='/logs/verifier/'+name,size=len(data),mtime_ns=1,inode=1)
                               for name,data in payload.items()}
            else:
                for name,data in payload.items():
                    path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)

    async def exec(self, command, **kwargs):
        if not command.startswith('python3 -I -c '):
            return SimpleNamespace(return_code=1,stdout='',stderr='')
        script=shlex.split(command)[3]
        if script.startswith('# verifier inventory') and self.override is not None:
            return SimpleNamespace(return_code=0,stdout=json.dumps(self.override),stderr='')
        if 'base64.b64encode' in script:
            self.chunks+=1
            if self.chunks==self.interrupt_chunk:raise asyncio.CancelledError()
        script=script.replace('/logs/verifier',str(self.root))
        proc=await asyncio.create_subprocess_exec(sys.executable,'-I','-c',script,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stdout,stderr=await proc.communicate()
        output=stdout.decode().replace(str(self.root),'/logs/verifier')
        return SimpleNamespace(return_code=proc.returncode,stdout=output,stderr=stderr.decode())


class EvidenceTests(unittest.TestCase):
    def collect(self, payload, target, **kwargs):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            client=VerifierExec(Path(raw)/'remote',payload)
            with patch.object(UniverseACK,'exec',AsyncMock(side_effect=client.exec)):
                return asyncio.run(obj.download_dir_filtered('/logs/verifier',target,**kwargs))

    def test_full_verifier_output_and_reward_collected(self):
        payload={'reward.txt':b'1\n','test-stdout.txt':b'All tests passed\n','ctrf.json':b'{"passed":true}'}
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            self.collect(payload,raw)
            self.assertEqual((Path(raw)/'test-stdout.txt').read_text(),'All tests passed\n')
            self.assertEqual((Path(raw)/'reward.txt').read_text(),'1\n')
            self.assertEqual((Path(raw)/'ctrf.json').stat().st_mode&0o777,0o600)
            record=json.loads((Path(raw)/'acs-collection.json').read_text())
            self.assertTrue(record['complete']);self.assertEqual(record['files'],3)

    def test_unsafe_verifier_path_refused(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            with self.assertRaises(ValueError):
                self.collect({'reward.txt':b'1','../escape':b'bad'},raw)

    def test_timeout_preserves_diagnostics_without_creating_a_reward(self):
        from acs_result_diagnostics import classify_trial
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            payload={'test-stdout.txt':b'Collected tests; waiting for result\n',
                     'test-stderr.txt':b'No result before timeout\n',
                     'details/progress.txt':b'phase: verify\n'}
            self.collect(payload,root)
            for name,value in payload.items():
                self.assertEqual((root/name).read_bytes(),value)
                self.assertEqual((root/name).stat().st_mode&0o777,0o600)
            self.assertFalse((root/'reward.txt').exists())
            diagnostic=classify_trial({'verifier_result':None,'exception_info':{'exception_type':'VerifierTimeoutError'}})
            self.assertFalse(diagnostic['scored']);self.assertIsNone(diagnostic['reward'])

    def test_invalid_reward_retained_verbatim_with_logs_but_still_fails(self):
        for reward in (b'',b'NaN\n',b'0.75',b'not a score\n',b'\xff'):
            with self.subTest(reward=reward),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                root=Path(raw)
                with self.assertRaisesRegex(ValueError,'Invalid verifier reward; available diagnostics retained'):
                    self.collect({'test-stdout.txt':b'failed while writing result\n','reward.txt':reward},root)
                self.assertEqual((root/'test-stdout.txt').read_bytes(),b'failed while writing result\n')
                self.assertEqual((root/'reward.txt').read_bytes(),reward)

    def test_missing_reward_respects_log_filters_and_retains_protected_diagnostics(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            self.collect({'test-stdout.txt':b'output','test-stderr.txt':b'error','debug.txt':b'debug'},root,
                         include=['test-stdout.txt'],protect=['test-stderr.txt'])
            self.assertEqual((root/'test-stdout.txt').read_bytes(),b'output')
            self.assertEqual((root/'test-stderr.txt').read_bytes(),b'error')
            self.assertFalse((root/'debug.txt').exists())

    def test_late_unsafe_path_or_collision_cannot_partially_write_any_files(self):
        malicious=[{'../escape':b'bad'},{'/absolute':b'bad'},{'nested/../escape':b'bad'},
                   {'./ambiguous':b'bad'},{'a':b'file','a/child':b'bad'}]
        for extra in malicious:
            with self.subTest(extra=list(extra)),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                root=Path(raw);target=root/'evidence'
                with self.assertRaises(ValueError):
                    self.collect({'test-stdout.txt':b'must not be partially written','reward.txt':b'1',**extra},target)
                self.assertFalse(target.exists())
                self.assertEqual(list(root.iterdir()),[])

    def test_symlink_destination_refused_before_any_other_diagnostic_is_written(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);target=root/'evidence';target.mkdir();outside=root/'outside';outside.mkdir()
            (target/'linked').symlink_to(outside,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'Symlink'):
                self.collect({'test-stdout.txt':b'not yet','reward.txt':b'1','linked/escape':b'bad'},target)
            self.assertFalse((target/'test-stdout.txt').exists());self.assertEqual(list(outside.iterdir()),[])

    def test_over_16mib_logs_are_retained_and_hashed(self):
        import hashlib
        payload={'test-stdout.txt':b'Complete verifier output\n','reward.txt':b'1',
                 'huge.log':b'x'*(17*1024*1024)}
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)/'target';self.collect(payload,root)
            receipt=json.loads((root/'acs-collection.json').read_text())
            self.assertTrue(receipt['complete']);self.assertGreater(receipt['bytes'],16*1024*1024)
            for name,data in payload.items():
                from evidence_files import sha256_stream
                with (root/name).open('rb') as stream:digest=sha256_stream(stream)
                self.assertEqual(digest,hashlib.sha256(data).hexdigest())
                self.assertEqual(digest,receipt['manifest'][name]['sha256'])

    def test_corrupt_chunk_cannot_replace_previous_file(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);target=root/'target';target.mkdir();(target/'test-stdout.txt').write_text('previous')
            client=VerifierExec(root/'remote',{'test-stdout.txt':b'changed'})
            original=client.exec
            async def corrupted(command,**kwargs):
                if 'base64.b64encode' in command:
                    return SimpleNamespace(return_code=0,stdout=json.dumps('not-base64!'),stderr='')
                return await original(command,**kwargs)
            obj=object.__new__(UniverseACK)
            with patch.object(UniverseACK,'exec',AsyncMock(side_effect=corrupted)):
                with self.assertRaises(ValueError):asyncio.run(obj.download_dir('/logs/verifier',target))
            self.assertEqual((target/'test-stdout.txt').read_text(),'previous')
            self.assertFalse((target/'acs-collection.json').exists())

    def test_digest_execution_does_not_require_unused_tests_dockerfile(self):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            obj.environment_dir=Path(raw)
            obj._universe_image='registry/task@sha256:'+'a'*64
            obj._validate_definition()
            obj.environment_dir=Path(raw)/'absent'
            with self.assertRaises(FileNotFoundError):obj._validate_definition()

    def test_real_harbor_verifier_reports_missing_reward_not_download_failure(self):
        from harbor.models.task.task import Task
        from harbor.models.trial.paths import TrialPaths
        from harbor.verifier.verifier import Verifier, RewardFileNotFoundError
        from acs_result_diagnostics import classify_trial
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);task_dir=root/'task';(task_dir/'tests').mkdir(parents=True)
            (task_dir/'task.toml').write_text('schema_version="1.0"\n')
            (task_dir/'instruction.md').write_text('Write a result.')
            (task_dir/'tests/test.sh').write_text('#!/bin/sh\nset -e\ntest -f /app/result.json\n')
            task=Task(task_dir);paths=TrialPaths(root/'trial');paths.mkdir()
            obj=object.__new__(UniverseACK);obj.task_env_config=task.config.environment
            client=VerifierExec(root/'remote',{'test-stdout.txt':b''})
            verifier=Verifier(task=task,trial_paths=paths,environment=obj,skip_tests_upload=True)
            with patch.object(UniverseACK,'exec',AsyncMock(side_effect=client.exec)):
                with self.assertRaises(RewardFileNotFoundError) as caught:
                    asyncio.run(verifier.verify())
            self.assertTrue(paths.test_stdout_path.is_file())
            self.assertFalse(paths.reward_text_path.exists())
            diagnostic=classify_trial({'verifier_result':None,'exception_info':{
                'exception_type':type(caught.exception).__name__,'exception_message':str(caught.exception)}})
            self.assertFalse(diagnostic['scored']);self.assertIsNone(diagnostic['reward'])

    def test_missing_remote_file_cannot_claim_success_or_reuse_old_file(self):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            target=Path(raw)/'result.json';target.write_text('stale result')
            response=SimpleNamespace(return_code=0,stdout='{"missing":true}')
            with patch.object(UniverseACK,'exec',AsyncMock(return_value=response)):
                with self.assertRaisesRegex(FileNotFoundError,'source file is missing'):
                    asyncio.run(obj.download_file('/app/missing.json',target))
            self.assertEqual(target.read_text(),'stale result')
            self.assertEqual(list(Path(raw).iterdir()),[target])

    def test_file_download_requires_new_regular_file_and_rejects_symlinks(self):
        obj=object.__new__(UniverseACK)
        for kind in ('file','directory','symlink'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                root=Path(raw);target=root/'output';outside=root/'outside';outside.write_text('original')
                async def transfer(environment,source,downloaded):
                    if kind=='file':downloaded.write_bytes(b'new\x00bytes')
                    elif kind=='directory':downloaded.mkdir()
                    else:downloaded.symlink_to(outside)
                with patch('acs_evidence_transport.download_file',AsyncMock(side_effect=transfer)):
                    if kind=='file':
                        asyncio.run(obj.download_file('/app/output',target))
                        self.assertEqual(target.read_bytes(),b'new\x00bytes')
                        self.assertEqual(target.stat().st_mode&0o777,0o600)
                    else:
                        with self.assertRaisesRegex(ValueError,'regular file'):
                            asyncio.run(obj.download_file('/app/output',target))
                        self.assertFalse(target.exists())
                self.assertEqual(outside.read_text(),'original')
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);outside=root/'outside';outside.write_text('original')
            target=root/'linked';target.symlink_to(outside)
            with patch('acs_evidence_transport.download_file',AsyncMock()) as transfer:
                with self.assertRaisesRegex(ValueError,'Symlink'):
                    asyncio.run(obj.download_file('/app/output',target))
                transfer.assert_not_awaited()
            self.assertEqual(outside.read_text(),'original')

    def test_capture_failure_does_not_block_cleanup(self):
        obj=object.__new__(UniverseACK)
        with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock(side_effect=ValueError('fixture'))),patch('universe_ack.ACKEnvironment.stop',AsyncMock()) as stop:
            with self.assertRaises(ValueError):asyncio.run(obj.stop(delete=True))
            stop.assert_awaited_once_with(delete=True)

    def test_stop_recovers_timeout_stdout_before_deleting_pod_without_reward(self):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            target=Path(raw)/'verifier';obj.trial_paths=SimpleNamespace(verifier_dir=target);obj._core_api=object()
            client=VerifierExec(Path(raw)/'remote',{'test-stdout.txt':b'Waiting on package download\n'})
            async def stopped(delete):
                self.assertTrue(delete)
                self.assertEqual((target/'test-stdout.txt').read_text(),'Waiting on package download\n')
                self.assertFalse((target/'reward.txt').exists())
            with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()),patch.object(UniverseACK,'exec',AsyncMock(side_effect=client.exec)) as execute,patch('universe_ack.ACKEnvironment.stop',AsyncMock(side_effect=stopped)) as stop:
                asyncio.run(obj.stop(delete=True))
                self.assertEqual(execute.call_args.kwargs['timeout_sec'],30)
                stop.assert_awaited_once_with(delete=True)

    def test_failed_timeout_log_transfer_still_deletes_pod(self):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            obj.trial_paths=SimpleNamespace(verifier_dir=Path(raw));obj._core_api=object()
            with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()),patch.object(UniverseACK,'download_dir_filtered',AsyncMock(side_effect=TimeoutError)),patch('universe_ack.ACKEnvironment.stop',AsyncMock()) as stop:
                asyncio.run(obj.stop(delete=True));stop.assert_awaited_once_with(delete=True)

    def test_stop_does_not_overwrite_already_retained_verifier_output(self):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            target=Path(raw);obj.trial_paths=SimpleNamespace(verifier_dir=target);obj._core_api=object()
            (target/'test-stdout.txt').write_text('Original evidence')
            with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()),patch.object(UniverseACK,'download_dir_filtered',AsyncMock()) as collect,patch('universe_ack.ACKEnvironment.stop',AsyncMock()):
                asyncio.run(obj.stop(delete=True));collect.assert_not_awaited()
            self.assertEqual((target/'test-stdout.txt').read_text(),'Original evidence')

    def test_source_adapters_identical_and_wrapper_prefers_source(self):
        directory=Path(__file__).parent
        self.assertEqual((directory/'universe_ack.py').read_bytes(),(directory/'harbor-extensions/universe_ack.py').read_bytes())
        wrapper=(directory/'run-acs-evaluation').read_text()
        self.assertIn('PYTHONPATH="$W/gewu-multiverse/deploy/science/harbor-extensions:',wrapper)


if __name__=='__main__':unittest.main()
