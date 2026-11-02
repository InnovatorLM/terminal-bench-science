"""Offline failure-path tests; no model or cloud credentials are used."""
import asyncio
import base64
import json
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from acs_evidence_transport import download_file, download_agent
from acs_helpers.execution_budget import apply, budgets
from universe_ack import UniverseACK

W = Path('/mnt/data/zhanglinfeng/workspace')


class TimeoutTests(unittest.TestCase):
    def test_solver_budget_preserved_and_pod_survives_all_phases(self):
        c = {'agents':[{'override_timeout_sec':14400}],
             'verifier':{'override_timeout_sec':14400},
             'environment':{'kwargs':{'pod_overrides':{'spec':{'activeDeadlineSeconds':14400}}}}}
        result = apply(c)
        self.assertEqual(c['agents'][0]['override_timeout_sec'], 14400)
        self.assertGreater(result['pod_seconds'], 14400 * 2)
        self.assertGreater(result['worker_seconds'], result['controller_seconds'])
        self.assertGreater(result['controller_seconds'], result['pod_seconds'])
        c['agents'][0]['override_timeout_sec'] = 14401
        with self.assertRaises(ValueError): budgets(c)

    def test_verifier_budget_comes_from_frozen_task(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            task = Path(raw)/'task'; task.mkdir()
            (task/'task.toml').write_text('[verifier]\ntimeout_sec=1200\n')
            c = {'agents':[{'name':'oracle','override_timeout_sec':14400}],
                 'datasets':[{'path':raw,'task_names':['task']}]}
            self.assertEqual(budgets(c)['verifier_seconds'],1200)

    def test_binary_transfer_and_missing_file_preserve_previous_evidence(self):
        class LocalExec:
            async def exec(self, command, timeout_sec):
                # Execute the real remote Python protocol in a disposable local
                # directory; only the transport is substituted, not its bytes.
                script=shlex.split(command)[3]
                proc=await asyncio.create_subprocess_exec(sys.executable,'-I','-c',script,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                stdout,stderr=await proc.communicate()
                return SimpleNamespace(return_code=proc.returncode,stdout=stdout.decode(),stderr=stderr.decode())
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw); source=root/'source'; target=root/'target'
            content=bytes(range(256))*5000; source.write_bytes(content)
            asyncio.run(download_file(LocalExec(),source,target))
            self.assertEqual(target.read_bytes(),content)
            self.assertEqual(target.stat().st_mode&0o777,0o600)
            source.unlink()
            with self.assertRaises(FileNotFoundError):asyncio.run(download_file(LocalExec(),source,target))
            self.assertEqual(target.read_bytes(),content)
            self.assertEqual(list(root.glob('.acs-download-*')),[])

    def test_checkpoint_collects_live_codex_sessions_without_auth_files(self):
        obj=SimpleNamespace()
        manifest={'codex.txt':{'source':'/logs/agent/codex.txt','size':4},
                  'sessions/day/run.jsonl':{'source':'/tmp/codex-home/sessions/day/run.jsonl','size':7}}
        async def transfer(environment,source,target,**kwargs):
            target.parent.mkdir(parents=True,exist_ok=True)
            content=b'log\n' if source.endswith('codex.txt') else b'{"x":1}'
            target.write_bytes(content)
            return {'bytes':len(content),'downloaded_bytes':len(content),'resumed_bytes':0,'sha256':'a'*64}
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            with patch('acs_evidence_transport.remote_json',AsyncMock(return_value=manifest)), \
                 patch('acs_evidence_transport.download_file',AsyncMock(side_effect=transfer)):
                result=asyncio.run(download_agent(obj,Path(raw)))
            self.assertEqual((result['files'],result['bytes'],result['complete']),(2,11,True))
            self.assertEqual((Path(raw)/'sessions/day/run.jsonl').read_text(),'{"x":1}')
            self.assertFalse((Path(raw)/'auth.json').exists())

    def test_stop_collects_partial_trace_before_pod_deletion(self):
        obj=object.__new__(UniverseACK); obj._core_api=object()
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw); obj.trial_paths=SimpleNamespace(agent_dir=root/'agent',verifier_dir=root/'verifier')
            async def collect(target):
                target.mkdir();(target/'codex.txt').write_text('partial reasoning')
                return {'files':1,'bytes':17}
            async def delete(delete):
                self.assertEqual((root/'agent/codex.txt').read_text(),'partial reasoning')
                self.assertEqual(json.loads((root/'agent/acs-collection.json').read_text())['status'],'collected')
            with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()), \
                 patch.object(UniverseACK,'_download_agent_evidence',AsyncMock(side_effect=collect)), \
                 patch.object(UniverseACK,'_retain_incomplete_verifier_evidence',AsyncMock()), \
                 patch('universe_ack.ACKEnvironment.stop',AsyncMock(side_effect=delete)) as stopped:
                asyncio.run(obj.stop(delete=True)); stopped.assert_awaited_once()

    def test_failed_recollection_preserves_checkpoint_and_still_cleans_up(self):
        obj=object.__new__(UniverseACK); obj._core_api=object()
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);(root/'codex.txt').write_text('earlier checkpoint')
            obj.trial_paths=SimpleNamespace(agent_dir=root)
            with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()), \
                 patch.object(UniverseACK,'_download_agent_evidence',AsyncMock(side_effect=TimeoutError)), \
                 patch.object(UniverseACK,'_retain_incomplete_verifier_evidence',AsyncMock()), \
                 patch('universe_ack.ACKEnvironment.stop',AsyncMock()) as stopped:
                asyncio.run(obj.stop(delete=True)); stopped.assert_awaited_once()
            self.assertEqual((root/'codex.txt').read_text(),'earlier checkpoint')
            self.assertEqual(json.loads((root/'acs-collection.json').read_text())['status'],'collection_failed')

    def test_cancelling_checkpoint_does_not_skip_final_collection(self):
        obj=object.__new__(UniverseACK)
        async def run():
            obj._agent_checkpoint_task=asyncio.create_task(asyncio.sleep(100))
            with patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()), \
                 patch.object(UniverseACK,'_retain_incomplete_agent_evidence',AsyncMock()) as collect, \
                 patch.object(UniverseACK,'_retain_incomplete_verifier_evidence',AsyncMock()), \
                 patch('universe_ack.ACKEnvironment.stop',AsyncMock()) as stopped:
                await obj.stop(); collect.assert_awaited_once(); stopped.assert_awaited_once()
        asyncio.run(run())


if __name__=='__main__':unittest.main()
