"""Real transport protocol on temporary local files; no cloud/model calls."""
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import acs_evidence_transport as transport
from execution_evidence import digest_file
from publish_evidence import publish
import test_execution_evidence as evidence_fixtures
from universe_ack import UniverseACK

W = Path('/mnt/data/zhanglinfeng/workspace')


class LocalExec:
    def __init__(self, interrupt_chunk=None, rewrite=None):
        self.chunks = 0
        self.interrupt_chunk = interrupt_chunk
        self.rewrite = rewrite

    async def exec(self, command, timeout_sec):
        script = shlex.split(command)[3]
        if 'base64.b64encode' in script:
            self.chunks += 1
            if self.chunks == self.interrupt_chunk:
                raise asyncio.CancelledError()
            if self.rewrite is not None:
                self.rewrite(); self.rewrite = None
        proc = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        return SimpleNamespace(return_code=proc.returncode, stdout=stdout.decode(), stderr=stderr.decode())


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=W/'.tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.target = self.root/'source', self.root/'target'
        self.content = bytes(range(256)) * 10000
        self.source.write_bytes(self.content)

    def test_cancel_resume_keeps_checkpoint_and_does_not_download_prefix(self):
        self.target.write_bytes(b'old complete checkpoint')
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(transport.download_file(LocalExec(interrupt_chunk=2), self.source, self.target))
        self.assertEqual(self.target.read_bytes(), b'old complete checkpoint')
        partial = next((self.root/'.acs-transfer').glob('*.part'))
        self.assertEqual(partial.stat().st_size, transport.CHUNK_BYTES)
        result = asyncio.run(transport.download_file(LocalExec(), self.source, self.target))
        self.assertEqual(result['resumed_bytes'], transport.CHUNK_BYTES)
        self.assertEqual(result['downloaded_bytes'], len(self.content)-transport.CHUNK_BYTES)
        self.assertEqual(self.target.read_bytes(), self.content)
        self.assertFalse(partial.exists())

    def test_unchanged_checkpoint_transfers_nothing_and_append_only_transfers_tail(self):
        asyncio.run(transport.download_file(LocalExec(), self.source, self.target))
        client = LocalExec()
        result = asyncio.run(transport.download_file(client, self.source, self.target))
        self.assertEqual((client.chunks,result['downloaded_bytes']), (0,0))
        with self.source.open('ab') as out: out.write(b'new final event\n')
        result = asyncio.run(transport.download_file(LocalExec(), self.source, self.target))
        self.assertEqual(result['downloaded_bytes'], len(b'new final event\n'))
        self.assertEqual(self.target.read_bytes(), self.content+b'new final event\n')

    def test_rewrite_and_truncation_do_not_splice_generations(self):
        for content in (b'z'*len(self.content), b'short replacement'):
            self.source.write_bytes(self.content)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(transport.download_file(LocalExec(interrupt_chunk=2), self.source, self.target))
            self.source.write_bytes(content)
            result = asyncio.run(transport.download_file(LocalExec(), self.source, self.target))
            self.assertEqual(result['resumed_bytes'], 0)
            self.assertEqual(self.target.read_bytes(), content)

    def test_source_mutation_during_download_cannot_replace_checkpoint(self):
        self.target.write_bytes(b'previous')
        client = LocalExec(rewrite=lambda:self.source.write_bytes(b'x'*len(self.content)))
        with self.assertRaisesRegex(ValueError,'checksum'):
            asyncio.run(transport.download_file(client,self.source,self.target))
        self.assertEqual(self.target.read_bytes(),b'previous')

    def test_symlink_partial_and_source_are_refused(self):
        asyncio.run(transport.download_file(LocalExec(),self.source,self.target))
        key=hashlib.sha256((str(self.source)+'\0'+str(self.target.absolute())).encode()).hexdigest()
        partial=self.root/'.acs-transfer'/(key+'.part');partial.symlink_to(self.source)
        with self.assertRaises(ValueError):
            asyncio.run(transport.download_file(LocalExec(),self.source,self.target))
        partial.unlink();self.source.unlink();self.source.symlink_to(self.target)
        with self.assertRaises(OSError):
            asyncio.run(transport.download_file(LocalExec(),self.source,self.target))

    def test_over_100mib_session_transfers_hashes_and_publishes_without_truncation(self):
        session=self.root/'trial/agent/sessions/run.jsonl';session.parent.mkdir(parents=True)
        line=json.dumps({'type':'message','text':'q'*8192,'api_key':'SECRET-CANARY'}).encode()+b'\n'
        count=101*1024*1024//len(line)+1
        with self.source.open('wb') as out:
            for _ in range(count):out.write(line)
        result=asyncio.run(transport.download_file(LocalExec(),self.source,session))
        self.assertGreater(result['bytes'],100*1024*1024)
        expected=digest_file(self.source,self.root)
        self.assertEqual(digest_file(session,self.root),expected)
        # No whole-file native-log read is allowed during publication.
        with patch('publish_evidence.read_file',side_effect=AssertionError('whole-file JSONL read')):
            manifest=publish(self.root/'trial',self.root/'packet',['SECRET-CANARY'])
        record=manifest['files']['agent/sessions/run.jsonl']
        self.assertEqual(record['source_sha256'],expected)
        published=self.root/'packet/agent/sessions/run.jsonl'
        self.assertEqual(record['sha256'],digest_file(published,self.root))
        with published.open() as inp:
            total=0
            for row in inp:
                self.assertEqual(json.loads(row)['api_key'],'[REDACTED]');total+=1
        self.assertEqual(total,count)

    def test_final_collection_retries_growing_snapshot(self):
        obj=object.__new__(UniverseACK)
        obj._download_agent_evidence=AsyncMock(side_effect=[{'complete':False},{'complete':True}])
        with patch('universe_ack.asyncio.sleep',AsyncMock()):
            result=asyncio.run(obj._final_agent_evidence(self.root))
        self.assertTrue(result['complete']);self.assertEqual(obj._download_agent_evidence.await_count,2)

    def test_started_run_is_incomplete_until_final_collection(self):
        obj=object.__new__(UniverseACK);obj._universe_role='runtime'
        obj.trial_paths=SimpleNamespace(agent_dir=self.root/'agent')
        async def run():
            with patch('universe_ack.ACKEnvironment.start',AsyncMock()), \
                 patch.object(UniverseACK,'_capture_pod_evidence',AsyncMock()):
                await obj.start()
                receipt=json.loads((self.root/'agent/acs-collection.json').read_text())
                self.assertEqual(receipt['schema_version'],2)
                self.assertFalse(receipt['complete']);self.assertEqual(receipt['status'],'pending')
                obj._agent_checkpoint_task.cancel()
                try:await obj._agent_checkpoint_task
                except asyncio.CancelledError:pass
        asyncio.run(run())

    def test_failed_or_changed_final_receipt_cannot_certify_complete_evidence(self):
        from execution_evidence import inspect
        fixture=evidence_fixtures.EvidenceTests();c=fixture.config();report=fixture.report(c)
        path,trial=fixture.fixture(self.root,c)
        session=trial/'agent/sessions/run.jsonl';session.parent.mkdir();session.write_text('{"event":1}\n')
        receipt=trial/'agent/acs-collection.json'
        record={'schema_version':2,'status':'collected','complete':True,'files':1,
                'manifest':{'sessions/run.jsonl':{'sha256':digest_file(session,self.root)}}}
        receipt.write_text(json.dumps(record))
        self.assertTrue(inspect(report,path,c,'a'*64,True)['complete'])
        for status in ('checkpoint','collection_failed'):
            receipt.write_text(json.dumps(dict(record,status=status)))
            self.assertFalse(inspect(report,path,c,'a'*64,True)['complete'])
        receipt.write_text(json.dumps(record));session.write_text('{"changed":true}\n')
        self.assertFalse(inspect(report,path,c,'a'*64,True)['complete'])

    def test_legacy_checkpoint_is_not_a_v2_attestation_and_failure_remains_incomplete(self):
        from execution_evidence import inspect
        fixture=evidence_fixtures.EvidenceTests();c=fixture.config();report=fixture.report(c)
        path,trial=fixture.fixture(self.root,c);receipt=trial/'agent/acs-collection.json'
        receipt.write_text(json.dumps({'status':'checkpoint','files':3,'bytes':1000}))
        evidence=inspect(report,path,c,'a'*64,True)
        self.assertTrue(evidence['complete'])
        self.assertNotIn('agent_collection_complete',evidence['checks'])
        receipt.write_text(json.dumps({'status':'collection_failed'}))
        self.assertFalse(inspect(report,path,c,'a'*64,True)['complete'])


if __name__=='__main__':unittest.main()
