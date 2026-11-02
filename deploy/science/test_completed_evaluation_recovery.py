"""Recover completed trial publication from fixture evidence; no API/cloud calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation_queue import W,digest_tree
from evidence_files import digest_file
from recover_completed_evaluation import recover
import test_execution_evidence as fixtures


class CompletedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=W/'.tmp');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'jobs';self.state.mkdir()
        self.job_id='a'*32;self.job=self.state/self.job_id;self.job.mkdir()
        self.results=self.root/'role/evaluation/results';self.results.mkdir(parents=True)
        snapshot=self.root/'snapshot';snapshot.mkdir();(snapshot/'instruction.md').write_text('Frozen task')
        self.snapshot=snapshot
        self.request={'job_id':self.job_id,'evaluation_id':'task-v1','snapshot_sha256':digest_tree(snapshot)}
        fixture=fixtures.EvidenceTests();self.actual=fixture.config()
        self.actual['job_name']='tbs-universe-linfeng-queue-'+self.job_id
        self.result_path,self.trial=fixture.fixture(self.job/'runs',self.actual)
        session=self.trial/'agent/sessions/run.jsonl';session.parent.mkdir();session.write_text('{"event":"SECRET-CANARY"}\n')
        (self.job/'config.json').write_text(json.dumps(self.actual))
        self.report=fixture.report(self.actual);(self.job/'report.json').write_text(json.dumps(self.report))
        self.claim=dict(self.request,mock=False);(self.job/'claim.json').write_text(json.dumps(self.claim))
        registry=self.root/'registry.json';registry.write_text(json.dumps({'task-v1':dict(snapshot=str(snapshot),snapshot_sha256=self.request['snapshot_sha256'])}))
        self.config=dict(state=str(self.state),results=str(self.results),evaluator_uid=os.getgid(),registry=str(registry))
        self.original={str(p):digest_file(p,self.job) for p in self.job.rglob('*') if p.is_file()}
        for patcher in [patch('evaluation_queue.validated_template',return_value=self.actual),
                        patch('selftest_logs.credential_values',return_value=['SECRET-CANARY']),
                        patch('subprocess.Popen',side_effect=AssertionError('Cloud process must not start'))]:
            patcher.start();self.addCleanup(patcher.stop)

    def test_recover_retained_result_and_packet_without_changing_originals(self):
        public=self.results/(self.job_id+'.json');public.write_text(json.dumps(dict(self.request,status='interrupted')))
        answer=recover(self.config,self.request)
        self.assertEqual(answer['status'],'solver_complete');self.assertEqual(answer['reward'],1)
        self.assertTrue(answer['evidence_complete']);self.assertTrue(answer['cleanup_confirmed'])
        self.assertEqual(json.loads(public.read_text()),json.loads((self.job/'result.json').read_text()))
        for name,digest in self.original.items():self.assertEqual(digest_file(Path(name),self.job),digest)
        proof=json.loads((self.job/'publication-recovery.json').read_text());self.assertEqual(proof['model_calls'],0)
        packet=self.results.parent/'evidence'/self.job_id
        self.assertNotIn('SECRET-CANARY',(packet/'agent/trajectory.json').read_text())
        self.assertTrue((packet/'execution-binding.json').is_file())
        self.assertEqual(recover(self.config,self.request),answer)

    def test_live_controller_or_unknown_cleanup_cannot_be_recovered(self):
        (self.job/'claim.json').write_text(json.dumps(dict(self.claim,controller_pid=os.getpid())))
        self.assertIsNone(recover(self.config,self.request))
        (self.job/'claim.json').write_text(json.dumps(self.claim))
        self.report['cleanup']['confirmed']=False;(self.job/'report.json').write_text(json.dumps(self.report))
        self.assertIsNone(recover(self.config,self.request));self.assertFalse((self.job/'result.json').exists())

    def test_changed_source_and_missing_native_trace_do_not_get_a_score(self):
        session=self.trial/'agent/sessions/run.jsonl';session.unlink()
        self.assertIsNone(recover(self.config,self.request))
        (self.snapshot/'instruction.md').write_text('Changed task')
        with self.assertRaises(ValueError):recover(self.config,self.request)
        self.assertFalse((self.job/'result.json').exists())

    def test_publication_retry_preserves_partial_packet(self):
        packet=self.results.parent/'evidence'/self.job_id;packet.mkdir(parents=True)
        (packet/'partial.txt').write_text('original interrupted publication')
        answer=recover(self.config,self.request)
        self.assertEqual(answer['status'],'solver_complete')
        preserved=list(self.job.glob('publication-before-recovery-*/partial.txt'))
        self.assertEqual(len(preserved),1);self.assertEqual(preserved[0].read_text(),'original interrupted publication')

    def test_recovery_refreshes_review_once_with_original_batch_and_preserved_defects(self):
        import review_dispatcher as d
        from role_evaluation_batch import requests
        home=self.root/'role/home/role';home.mkdir(parents=True)
        cfg={'roles':{'evaluator-01':{'root':str(self.root/'role'),'uid':os.getuid(),'gid':os.getgid()}}}
        row=dict(key='commit:tasks/example',commit='a'*40,task='tasks/example',role='evaluator-01',
                 generation=0,state='waiting_platform',stage='five_rollouts',assigned_at=100,
                 evaluation_id='task-v1',snapshot_sha256='a'*64)
        batch=requests(row)
        for index,request in enumerate(batch):
            result=dict(request,status='solver_complete',reward=1,n_errors=0,
                        evidence_complete=True,source_version_bound=True,cleanup_confirmed=True)
            if index==0:result.update(status='interrupted',reward=None)
            (self.results/(request['job_id']+'.json')).write_text(json.dumps(result))
        d.advance_review(row['key'],row,cfg,now=200)
        self.assertEqual(row['generation'],1)
        status=dict(key=row['key'],generation=1,state='waiting_author',stage='completeness',
                    reason='Existing verifier defect; one result unavailable.',updated_at=201)
        (home/'review-status.json').write_text(json.dumps(status))
        (home/'activity-receipt.json').write_text(json.dumps(dict(status,receipt_type='evaluator-activity-v1')))
        result=dict(batch[0],status='solver_complete',reward=1,n_errors=0,
                    evidence_complete=True,source_version_bound=True,cleanup_confirmed=True)
        (self.results/(batch[0]['job_id']+'.json')).write_text(json.dumps(result))
        updates=[dict(job_id=batch[0]['job_id'],recovery_sha256='b'*64)]
        with patch('recover_completed_evaluation.pending_review_updates',return_value=updates):
            d.advance_review(row['key'],row,cfg,now=202,role_available=False)
            self.assertEqual(row['state'],'waiting_author');self.assertEqual(row['reason'],status['reason'])
            d.advance_review(row['key'],row,cfg,now=203)
            self.assertEqual(row['generation'],2);self.assertEqual(row['stage'],'rollout_results_ready')
            self.assertEqual(requests(row),batch)
            self.assertEqual(row['reviews_before_publication_recovery'][0]['reason'],status['reason'])
            d.advance_review(row['key'],row,cfg,now=204)
            self.assertEqual(row['generation'],2)

    def test_recovery_notification_requires_exact_operator_proof_and_is_deduplicated(self):
        from recover_completed_evaluation import pending_review_updates
        from role_evaluation_batch import requests
        row=dict(key='commit:tasks/example',commit='a'*40,task='tasks/example',role='evaluator-01',
                 generation=2,batch_generation=0,evaluation_id='task-v1',snapshot_sha256='a'*64)
        batch=requests(row)
        state=self.root/'.state/gewu-multiverse/evaluation-queue';state.mkdir(parents=True)
        proofs=[dict(job_id=batch[0]['job_id'],kind='completed_execution_publication_recovery',
                     recovered_status='solver_complete'),
                dict(job_id='wrong',kind='completed_execution_publication_recovery',recovered_status='solver_complete'),
                dict(job_id=batch[2]['job_id'],kind='untrusted',recovered_status='solver_complete'),[],
                dict(job_id=batch[4]['job_id'],kind='completed_execution_publication_recovery',recovered_status='interrupted')]
        for request,proof in zip(batch,proofs):
            path=state/request['job_id'];path.mkdir();(path/'publication-recovery.json').write_text(json.dumps(proof))
        with patch('deployment_paths.universe_root',return_value=self.root):
            updates=pending_review_updates(row)
            self.assertEqual(len(updates),1);self.assertEqual(updates[0]['job_id'],batch[0]['job_id'])
            row['applied_publication_updates']=updates
            self.assertEqual(pending_review_updates(row),[])


if __name__=='__main__':unittest.main()
