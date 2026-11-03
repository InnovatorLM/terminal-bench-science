"""Replacement failure-path/dispatch tests; no cloud or model calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation_queue import W
import rollout_replacement as m
from role_evaluation_batch import requests,summary
import review_dispatcher as dispatcher


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=W/'.tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'dispatcher';self.state.mkdir()
        self.queue=self.root/'queue';self.queue.mkdir()
        self.role=self.root/'evaluator'
        for p in ['home/role','evaluation/inbox','evaluation/results']:(self.role/p).mkdir(parents=True)
        self.config={'roles':{'evaluator-01':{'root':str(self.role),'uid':os.getuid(),'gid':os.getgid()}}}
        self.key='a'*40+':tasks/bee'
        self.row=dict(key=self.key,commit='a'*40,task='tasks/bee',role='evaluator-01',
                      evaluation_id='bee-v1',snapshot_sha256='b'*64,
                      generation=0,state='waiting_platform',stage='five_rollouts',assigned_at=100)
        self.ledger={self.key:self.row};self.save_ledger()
        self.jobs=requests(self.row)
        for request in self.jobs:
            work=self.queue/request['job_id'];work.mkdir()
            self.write(work/'result.json',dict(request,status='infrastructure_failed',reward=None,
                                             cleanup_confirmed=True,source_version_bound=True))
            self.write(work/'report.json',dict(cleanup={'confirmed':True}))
            self.write(work/'claim.json',dict(request,queue_worker_pid=1))
            self.write(work/'timeout-diagnosis.json',dict(job_id=request['job_id'],
                    snapshot_sha256=request['snapshot_sha256'],classification=m.CLASSIFICATION,
                    count_as_model_failure=False))
        self.smoke=self.root/'smoke.json'
        self.write(self.smoke,dict(mode='live-acs-evidence-transport-smoke',model_calls=0,
                                  checkpoint_retained=True,final_collection_retained=True,
                                  command_timeout_observed=True,cleanup={'confirmed':True}))

    def write(self,path,value):
        path.write_text(json.dumps(value));path.chmod(0o600)

    def save_ledger(self):self.write(self.state/'ledger.json',self.ledger)

    def record(self, jobs=None):
        return m.record(self.state,self.queue,self.key,jobs or [r['job_id'] for r in self.jobs],
                        self.smoke,'The verified Pod lifetime and evidence collection repair passed the real ACS smoke.')

    def test_idempotent_replacements_preserve_old_evidence_and_exact_identity(self):
        old={p:p.read_bytes() for p in self.queue.glob('*/*.json')}
        path=self.record();self.assertEqual(path,self.record())
        m.apply(self.state,self.ledger,self.config)
        batch=requests(self.row)
        self.assertEqual(len({r['job_id'] for r in batch}),5)
        self.assertFalse(set(r['job_id'] for r in batch)&set(r['job_id'] for r in self.jobs))
        self.assertEqual(self.row['generation'],0)
        self.assertEqual(self.row['state'],'waiting_platform')
        self.assertEqual(self.row['role'],'evaluator-01')
        self.assertEqual(old,{p:p.read_bytes() for p in old})
        before={p:p.read_bytes() for p in (self.role/'evaluation/inbox').glob('*.json')}
        m.apply(self.state,self.ledger,self.config)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        for r in batch:
            self.assertEqual(r['snapshot_sha256'],'b'*64)
            self.assertEqual(r['evaluation_id'],'bee-v1')
            p=self.role/'evaluation/inbox'/(r['job_id']+'.json')
            self.assertEqual(json.loads(p.read_text()),r)
            self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_only_selected_invalid_slots_replaced(self):
        self.record([self.jobs[2]['job_id']]);m.apply(self.state,self.ledger,self.config)
        actual=requests(self.row)
        for i in range(5):
            self.assertEqual(actual[i]==self.jobs[i],i!=2)
        self.assertEqual(len(list((self.role/'evaluation/inbox').glob('*.json'))),1)

    def test_valid_model_failure_unknown_cleanup_or_live_worker_cannot_be_retried(self):
        work=self.queue/self.jobs[0]['job_id'];p=work/'result.json';old=json.loads(p.read_text())
        for bad in [dict(old,status='solver_complete',reward=0,evidence_complete=True,n_errors=0),
                    dict(old,cleanup_confirmed=False),dict(old,source_version_bound=False),
                    dict(old,reward=0)]:
            self.write(p,bad)
            with self.assertRaises(ValueError):self.record()
        self.write(p,old)
        with patch.object(m,'active_queue_worker',return_value=True):
            with self.assertRaises(ValueError):self.record()
        self.write(work/'report.json',dict(cleanup={'confirmed':False}))
        with self.assertRaises(ValueError):self.record()

    def test_wrong_snapshot_changed_evidence_and_stale_review_are_rejected(self):
        path=self.record()
        with self.assertRaises(ValueError):m.plans(self.state,dict(self.row,snapshot_sha256='c'*64))
        self.row['state']='completed'
        m.apply(self.state,self.ledger,self.config)
        self.assertEqual(self.row['state'],'completed')
        self.assertIn('rollout_replacement_error',self.row)
        self.row['state']='waiting_platform'
        item=json.loads(path.read_text())['evidence_files'][0]
        Path(item['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'evidence changed'):m.plans(self.state,self.row)
        m.apply(self.state,self.ledger,self.config)
        self.assertEqual(self.row['stage'],'five_rollouts')
        self.assertEqual(list((self.role/'evaluation/inbox').glob('*.json')),[])

    def test_mutable_sources_do_not_change_frozen_evidence(self):
        path=self.record();raw=path.read_bytes()
        value=json.loads(raw)
        self.assertEqual(value['schema_version'],2)
        snapshots={Path(item['path']):Path(item['path']).read_bytes() for item in value['evidence_files']}
        for item in value['evidence_sources']:
            Path(item['path']).write_text('{"status":"cancelled"}')
        m.apply(self.state,self.ledger,self.config)
        self.assertNotIn('rollout_replacement_error',self.row)
        self.assertEqual(path.read_bytes(),raw)
        for snapshot,original in snapshots.items():
            self.assertEqual(snapshot.read_bytes(),original)
            self.assertEqual(snapshot.stat().st_mode&0o777,0o400)
        self.assertEqual(len(list((self.role/'evaluation/inbox').glob('*.json'))),5)

    def legacy_plan(self):
        path=self.record()
        value=json.loads(path.read_text())
        value['schema_version']=1
        value['evidence_files']=value.pop('evidence_sources')
        self.write(path,value)
        return path

    def test_legacy_recovery_uses_exact_bytes_without_rewriting_plan_or_results(self):
        path=self.legacy_plan();plan_raw=path.read_bytes()
        m.apply(self.state,self.ledger,self.config)
        batch=list(self.row['batch_job_ids'])
        self.row.update(state='completed',review_recommendation='reject')
        changed=self.queue/self.jobs[0]['job_id']/'result.json'
        backup=self.root/'result.before.json';backup.write_bytes(changed.read_bytes())
        changed.write_text('{"status":"cancelled","reward":null}')
        m.apply(self.state,self.ledger,self.config)
        self.assertIn('rollout_replacement_error',self.row)
        self.assertEqual(self.row['state'],'completed')
        receipt=m.recover_evidence(path,[backup],'Exact original result retained before evidence enrichment; restore verified bytes alongside the immutable plan.')
        self.assertEqual(receipt,m.recover_evidence(path,[backup],'Exact original result retained before evidence enrichment; restore verified bytes alongside the immutable plan.'))
        self.assertEqual(path.read_bytes(),plan_raw)
        self.assertEqual(changed.read_text(),'{"status":"cancelled","reward":null}')
        with patch.object(m,'publish_request',side_effect=AssertionError('duplicate submission')):
            m.apply(self.state,self.ledger,self.config)
        self.assertNotIn('rollout_replacement_error',self.row)
        self.assertEqual(self.row['state'],'completed')
        self.assertEqual(self.row['review_recommendation'],'reject')
        self.assertEqual(self.row['batch_job_ids'],batch)
        self.assertEqual(len(m.plans(self.state,self.row)[1]),1)
        recovered=json.loads(receipt.read_text())['evidence_files'][0]
        Path(recovered['path']).write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'evidence changed'):m.load_checked(path)

    def test_missing_original_cannot_be_reconstructed_from_new_result(self):
        path=self.legacy_plan()
        changed=self.queue/self.jobs[0]['job_id']/'result.json'
        changed.write_text('{}')
        with self.assertRaisesRegex(ValueError,'unavailable'):
            m.recover_evidence(path,[changed],'The current altered result is not a substitute for the exact original evidence.')
        self.assertFalse(path.with_suffix('.recovered-evidence.json').exists())

    def test_one_bad_plan_does_not_block_other_assignment_status(self):
        path=self.record()
        item=json.loads(path.read_text())['evidence_files'][0]
        Path(item['path']).write_text('corrupt')
        other=dict(self.row,key='other',role='evaluator-02')
        self.ledger['other']=other
        config={'roles':dict(self.config['roles'],**{'evaluator-02':dict(self.config['roles']['evaluator-01'])})}
        advanced=[]
        with patch.object(dispatcher,'STATE',self.state),patch.object(dispatcher,'deliver_selftest_evidence',return_value=True), \
             patch.object(dispatcher,'advance_review',side_effect=lambda key,*args,**kwargs:advanced.append(key)):
            dispatcher.sync_direct_assignments(config,self.ledger)
        self.assertIn('other',advanced)
        self.assertEqual(self.row['state'],'waiting_platform')
        self.assertEqual(self.row['stage'],'five_rollouts')
        self.assertNotIn('rollout_replacement_error',other)

    def test_crash_replay_and_new_results_wake_review_without_duplicate_jobs(self):
        self.record();m.apply(self.state,self.ledger,self.config)
        batch=requests(self.row)
        # Simulate process exit after publication but before ledger save.
        restored=json.loads((self.state/'ledger.json').read_text())
        m.apply(self.state,restored,self.config)
        self.assertEqual(requests(restored[self.key]),batch)
        self.assertEqual(len(list((self.role/'evaluation/inbox').glob('*.json'))),5)
        with patch.object(dispatcher,'STATE',self.state):
            dispatcher.advance_review(self.key,self.row,self.config,now=200,role_available=False)
            self.assertEqual(self.row['state'],'waiting_platform')
            for i,request in enumerate(batch):
                self.write(self.role/'evaluation/results'/(request['job_id']+'.json'),
                           dict(request,status='solver_complete',cleanup_confirmed=True,
                                evidence_complete=True,source_version_bound=True,n_errors=0,reward=int(i<3)))
            # Result availability cannot steal the evaluator from another task.
            dispatcher.advance_review(self.key,self.row,self.config,now=201,role_available=False)
            self.assertEqual(self.row['state'],'waiting_platform')
            dispatcher.advance_review(self.key,self.row,self.config,now=202,role_available=True)
        self.assertEqual(self.row['state'],'reviewing')
        self.assertEqual(self.row['generation'],1)
        self.assertEqual(self.row['batch_generation'],0)
        self.assertEqual(requests(self.row),batch)
        self.assertEqual(summary(self.row,self.role/'evaluation/results')['pass_rate'],.6)
        m.apply(self.state,self.ledger,self.config)

    def test_bad_smoke_cannot_authorize_replacements(self):
        self.write(self.smoke,dict(mode='offline',model_calls=0))
        with self.assertRaisesRegex(ValueError,'live ACS'):self.record()

    def runtime_trust_fixture(self):
        smoke_dir=self.root/'runtime-smoke';smoke_dir.mkdir()
        smoke_job='f'*32
        config=smoke_dir/'config.json'
        self.write(config,dict(agents=[dict(name=None,import_path='universe_codex:UniverseCodex')]))
        trust=smoke_dir/'trust.json'
        self.write(trust,dict(tls_verified=True,endpoint='https://dawnfm.gravarc.cn/v1/models'))
        manifest=smoke_dir/'manifest.json'
        self.write(manifest,dict(files={'agent/runtime-trust.json':{'sha256':m.evidence(trust)['sha256']}}))
        report=smoke_dir/'report.json'
        self.write(report,dict(run_id='tbs-universe-linfeng-queue-'+smoke_job,cleanup={'confirmed':True},
                               harbor_exit_code=0,execution_identity={'config_sha256':m.digest(json.dumps(json.loads(config.read_text()),sort_keys=True).encode())}))
        result=smoke_dir/'result.json'
        self.write(result,dict(job_id=smoke_job,status='solver_complete',reward=1,n_errors=0,
                               provider='dawnfm',model='deepseek-v4.1-flash-1017',
                               source_version_bound=True,cleanup_confirmed=True,evidence_complete=True,
                               trajectory_retained=True,pod_evidence_complete=True,
                               published_evidence_manifest_sha256=m.evidence(manifest)['sha256']))
        smoke=dict(mode='live-acs-runtime-trust-smoke',job_id=smoke_job,
                   artifacts={k:m.evidence(v) for k,v in dict(result=result,config=config,report=report,
                                                             runtime_trust=trust,manifest=manifest).items()})
        self.write(self.smoke,smoke)
        for request in self.jobs:
            work=self.queue/request['job_id']
            value=json.loads((work/'result.json').read_text());value['status']='cancelled'
            self.write(work/'result.json',value)
            cancellation=work/'cancellation.json'
            self.write(cancellation,dict(job_id=request['job_id'],reason='Verified issuer failure; no model response.'))
            self.write(work/'platform-diagnosis.json',dict(request,classification=m.RUNTIME_TRUST,
                    count_as_model_failure=False,model_response_observed=False,
                    evidence_files=[m.evidence(cancellation)]))
        return smoke

    def test_cancelled_ca_failures_replace_only_with_actual_adapter_smoke(self):
        self.runtime_trust_fixture()
        original={p:p.read_bytes() for p in self.queue.glob('*/*.json')}
        self.record();m.apply(self.state,self.ledger,self.config)
        self.assertEqual(len(list((self.role/'evaluation/inbox').glob('*.json'))),5)
        self.assertEqual(original,{p:p.read_bytes() for p in original})
        self.assertTrue(all(r['classification']==m.RUNTIME_TRUST for r in
                            m.plans(self.state,self.row)[1][0]['plan']['replacements']))

    def test_adapter_bypass_or_unverified_tls_or_missing_trajectory_cannot_prove_fix(self):
        smoke=self.runtime_trust_fixture()
        for artifact,field,value in [('config','agents',[dict(name='codex',import_path='universe_codex:UniverseCodex')]),
                                     ('runtime_trust','tls_verified',False),
                                     ('result','trajectory_retained',False)]:
            with self.subTest(artifact=artifact):
                path=Path(smoke['artifacts'][artifact]['path']);original=path.read_bytes()
                changed=json.loads(original);changed[field]=value;self.write(path,changed)
                updated=json.loads(json.dumps(smoke));updated['artifacts'][artifact]=m.evidence(path)
                self.write(self.smoke,updated)
                with self.assertRaises(ValueError):self.record()
                path.write_bytes(original)
        self.write(self.smoke,smoke)
        work=self.queue/self.jobs[0]['job_id'];path=work/'platform-diagnosis.json'
        diagnosis=json.loads(path.read_text());diagnosis['model_response_observed']=True
        self.write(path,diagnosis)
        with self.assertRaisesRegex(ValueError,'no-response'):self.record()


if __name__=='__main__':unittest.main()
