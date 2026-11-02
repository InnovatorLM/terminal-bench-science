import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from evaluation_queue import W
from universe_ack import UniverseACK


class EvidenceTests(unittest.TestCase):
    def collect(self, payload, target, **kwargs):
        encoded={name:base64.b64encode(value).decode() for name,value in payload.items()}
        obj=object.__new__(UniverseACK)
        with patch.object(UniverseACK,'exec',AsyncMock(return_value=SimpleNamespace(return_code=0,stdout=json.dumps(encoded)))):
            return asyncio.run(obj.download_dir_filtered('/logs/verifier',target,**kwargs))

    def test_full_verifier_output_and_reward_collected(self):
        data={k:base64.b64encode(v).decode() for k,v in {'reward.txt':b'1\n','test-stdout.txt':b'All tests passed\n','ctrf.json':b'{"passed":true}'}.items()}
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            with patch.object(UniverseACK,'exec',AsyncMock(return_value=SimpleNamespace(return_code=0,stdout=json.dumps(data)))):
                asyncio.run(obj.download_dir('/logs/verifier',raw))
            self.assertEqual((Path(raw)/'test-stdout.txt').read_text(),'All tests passed\n')
            self.assertEqual((Path(raw)/'reward.txt').read_text(),'1\n')
            self.assertEqual((Path(raw)/'ctrf.json').stat().st_mode&0o777,0o600)

    def test_unsafe_verifier_path_and_missing_reward_refused(self):
        obj=object.__new__(UniverseACK)
        for data in [{'test-stdout.txt':base64.b64encode(b'pass').decode()},
                     {'reward.txt':base64.b64encode(b'1').decode(),'../escape':base64.b64encode(b'bad').decode()}]:
            with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                with patch.object(UniverseACK,'exec',AsyncMock(return_value=SimpleNamespace(return_code=0,stdout=json.dumps(data)))):
                    with self.assertRaises(ValueError):asyncio.run(obj.download_dir('/logs/verifier',raw))

    def test_timeout_preserves_diagnostics_without_creating_a_reward(self):
        from acs_result_diagnostics import classify_trial
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            payload={'test-stdout.txt':b'Collected tests; waiting for result\n',
                     'test-stderr.txt':b'No result before timeout\n',
                     'details/progress.txt':b'phase: verify\n'}
            with self.assertRaisesRegex(ValueError,'Missing verifier reward; available diagnostics retained'):
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
            with self.assertRaises(ValueError):
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

    def test_oversized_or_corrupt_payload_refused_before_any_write(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            with self.assertRaisesRegex(ValueError,'transfer limit'):
                self.collect({'test-stdout.txt':b'not yet','reward.txt':b'1','huge.log':b'x'*(16*1024*1024)},root/'large')
            self.assertFalse((root/'large').exists())
            obj=object.__new__(UniverseACK)
            data={'test-stdout.txt':base64.b64encode(b'not yet').decode(),'reward.txt':base64.b64encode(b'1').decode(),'corrupt':'not-base64!'}
            with patch.object(UniverseACK,'exec',AsyncMock(return_value=SimpleNamespace(return_code=0,stdout=json.dumps(data)))):
                with self.assertRaises(ValueError):asyncio.run(obj.download_dir('/logs/verifier',root/'bad'))
            self.assertFalse((root/'bad').exists())

    def test_digest_execution_does_not_require_unused_tests_dockerfile(self):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            obj.environment_dir=Path(raw)
            obj._universe_image='registry/task@sha256:'+'a'*64
            obj._validate_definition()
            obj.environment_dir=Path(raw)/'absent'
            with self.assertRaises(FileNotFoundError):obj._validate_definition()

    def test_capture_failure_does_not_block_cleanup(self):
        obj=object.__new__(UniverseACK)
        with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock(side_effect=ValueError('fixture'))),patch('universe_ack.ACKEnvironment.stop',AsyncMock()) as stop:
            with self.assertRaises(ValueError):asyncio.run(obj.stop(delete=True))
            stop.assert_awaited_once_with(delete=True)

    def test_stop_recovers_timeout_stdout_before_deleting_pod_without_reward(self):
        obj=object.__new__(UniverseACK)
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            target=Path(raw)/'verifier';obj.trial_paths=SimpleNamespace(verifier_dir=target);obj._core_api=object()
            data={'test-stdout.txt':base64.b64encode(b'Waiting on package download\n').decode()}
            async def stopped(delete):
                self.assertTrue(delete)
                self.assertEqual((target/'test-stdout.txt').read_text(),'Waiting on package download\n')
                self.assertFalse((target/'reward.txt').exists())
            with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()),patch.object(UniverseACK,'exec',AsyncMock(return_value=SimpleNamespace(return_code=0,stdout=json.dumps(data)))) as execute,patch('universe_ack.ACKEnvironment.stop',AsyncMock(side_effect=stopped)) as stop:
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
