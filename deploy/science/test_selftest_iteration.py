import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from evaluation_queue import W, digest_tree
from selftest_iteration import IterationEvidenceError, validate


class IterationTests(unittest.TestCase):
    def fixture(self, state, **changes):
        job='b'*32;task=state/job/'source/example'
        task.mkdir(parents=True);(task/'data').write_text('before')
        prior=dict(schema_version=2,role='author-01',job_id=job,status='selftest_passed',
                   source_git_tree_verified=True,source_repository='repo',source_task_path='tasks/example',
                   idea_id='stable-idea',completed_at=1,execution_task_path='example',source_commit='b'*40,
                   snapshot_sha256=digest_tree(task),trials=[{'cleanup_confirmed':True},{'cleanup_confirmed':True}])
        prior.update(changes)
        path=state/job/'status.json';path.write_text(json.dumps(prior))
        current=dict(source_repository='repo',source_task_path='tasks/example',received_at=2,
                     idea_id='stable-idea',execution_task_path='example',source_commit='c'*40,
                     snapshot_sha256='d'*64)
        link=dict(previous_job=job,note='Fixed verifier after observing the prior execution result.')
        return path,prior,current,link

    def test_idea_mismatch_preserves_receipt_and_explains_metadata_only_recovery(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            path,prior,current,link=self.fixture(Path(raw))
            before=path.read_bytes();current['idea_id']='stable-idea-r14'
            with self.assertRaises(IterationEvidenceError) as caught:
                validate(link,current,Path(raw),'author-01')
            report=caught.exception.report
            self.assertEqual(report['iteration_error_code'],'idea_identity_mismatch')
            self.assertEqual(report['failure_owner'],'request')
            self.assertEqual(report['previous_job'],prior['job_id'])
            self.assertEqual(report['previous_idea_id'],'stable-idea')
            self.assertEqual(report['current_idea_id'],'stable-idea-r14')
            self.assertFalse(report['retry_requires_task_change'])
            self.assertFalse(report['prior_execution_rerun_required'])
            self.assertIn('request-review CURRENT_JOB',report['remediation'])
            self.assertIn('platform reconciliation',report['remediation'])
            self.assertEqual(path.read_bytes(),before)

    def test_ineligible_or_missing_prior_is_not_an_acs_failure_or_cross_role_disclosure(self):
        for changes in ({'status':'interrupted'},{'role':'author-02','idea_id':'other-authors-idea'}):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                path,prior,current,link=self.fixture(Path(raw),**changes)
                with self.assertRaises(IterationEvidenceError) as caught:
                    validate(link,current,Path(raw),'author-01')
                report=caught.exception.report
                self.assertEqual(report['error_class'],'author_iteration_link_invalid')
                self.assertEqual(report['iteration_error_code'],'prior_execution_not_eligible')
                self.assertFalse(report['cloud_execution_started'])
                self.assertNotIn('other-authors-idea',json.dumps(report))
                path.unlink()
                with self.assertRaises(IterationEvidenceError) as missing:
                    validate(link,current,Path(raw),'author-01')
                self.assertEqual(missing.exception.report['iteration_error_code'],'prior_receipt_unavailable')

    def test_corrupt_immutable_evidence_requires_platform_recovery(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            path,prior,current,link=self.fixture(Path(raw))
            for damage in ('receipt','snapshot','cleanup'):
                with self.subTest(damage=damage):
                    path.write_text(json.dumps(prior))
                    task=path.parent/'source/example/data';task.write_text('before')
                    if damage=='receipt':path.write_text('{invalid')
                    elif damage=='snapshot':task.write_text('changed')
                    else:path.write_text(json.dumps(dict(prior,trials=[{'cleanup_confirmed':False}]*2)))
                    with self.assertRaises(IterationEvidenceError) as caught:
                        validate(link,current,Path(raw),'author-01')
                    self.assertEqual(caught.exception.report['failure_owner'],'platform')
                    self.assertEqual(caught.exception.report['error_class'],'platform_iteration_evidence_invalid')

    def test_worker_rejects_iteration_before_build_queue_without_cloud_or_source_changes(self):
        # Exercise a real committed upload and operator receipt, with cloud/build
        # boundaries replaced by mocks that must never be called.
        from test_acs_network_preflight import NetworkPreflightTests
        import author_selftest_worker as worker
        for kind in ('idea_mismatch','interrupted_prior','missing_link'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                root=Path(raw)
                source,role,job,request,entry,state,commit=NetworkPreflightTests().queued_upload(root,'schema_version="1.3"\n')
                prior_path,prior,_,link=self.fixture(state)
                q=json.loads(request.read_text());q.update(idea_id='stable-idea',iteration=link)
                if kind=='idea_mismatch':q['idea_id']='stable-idea-r14'
                elif kind=='interrupted_prior':
                    prior['status']='interrupted';prior_path.write_text(json.dumps(prior))
                else:q.pop('iteration');q['request_review']=True
                request.write_text(json.dumps(q));source_hash=digest_tree(source);prior_bytes=prior_path.read_bytes()
                observed=[];original_publish=worker.atomic_json
                def capture(path,payload,*args,**kwargs):
                    if Path(path).name=='status.json':observed.append(payload.get('status'))
                    return original_publish(path,payload,*args,**kwargs)
                with patch.object(worker,'W',root),patch.object(worker,'STATE',state), \
                     patch.object(worker,'credential_values',return_value=[]), \
                     patch.object(worker,'atomic_json',side_effect=capture), \
                     patch.object(worker,'builder_slot') as builder,patch.object(worker,'owned_run') as run:
                    worker.process('author-01',entry,request)
                builder.assert_not_called();run.assert_not_called()
                receipt=json.loads((role/'selftest/results'/(job+'.json')).read_text())
                self.assertEqual(receipt['error_class'],'author_iteration_link_invalid')
                self.assertEqual(receipt['stage'],'iteration_preflight')
                self.assertEqual(receipt['failure_owner'],'request')
                self.assertFalse(receipt['cloud_execution_started'])
                self.assertFalse(receipt['cloud_execution_attempted'])
                self.assertNotIn('waiting_builder',observed)
                self.assertFalse((state/job/'source/.git').exists())
                self.assertEqual(digest_tree(source),source_hash)
                self.assertEqual(prior_path.read_bytes(),prior_bytes)
                self.assertEqual(worker.failure_classification(receipt),'author_iteration_link_invalid')

    def test_prior_execution_and_changed_task_required(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state=Path(raw);job='a'*32;task=state/job/'source/proof'
            task.mkdir(parents=True);(task/'data').write_text('before')
            prior=dict(schema_version=2,role='author-01',job_id=job,status='selftest_failed',
                       source_git_tree_verified=True,source_repository='repo',source_task_path='tasks/proof',
                       completed_at=10,execution_task_path='proof',source_commit='b'*40,snapshot_sha256=digest_tree(task),
                       trials=[{'cleanup_confirmed':True},{'cleanup_confirmed':True}])
            current=dict(source_repository='repo',source_task_path='tasks/proof',received_at=11,
                         execution_task_path='proof-stage',source_commit='c'*40,snapshot_sha256='d'*64)
            link=dict(previous_job=job,note='Fixed verifier after observing the prior oracle failure.')
            def save(p): (state/job/'status.json').write_text(json.dumps(p))
            save(prior);result=validate(link,current,state,'author-01')
            self.assertTrue(result['linked_execution_verified'])
            self.assertFalse(result['author_note_independently_reviewed'])
            for field,value in [('role','author-02'),('status','building'),('completed_at',12),('source_repository','other-repo'),('source_task_path','tasks/other'),('source_git_tree_verified',False),('trials',[{'cleanup_confirmed':False}])]:
                bad=copy.deepcopy(prior);bad[field]=value;save(bad)
                with self.subTest(field=field),self.assertRaises(ValueError):validate(link,current,state,'author-01')
            save(prior)
            for field in ('source_commit','snapshot_sha256'):
                bad=dict(current);bad[field]=prior[field]
                with self.subTest(field=field),self.assertRaises(ValueError):validate(link,bad,state,'author-01')
            with self.assertRaisesRegex(ValueError,'idea ID'):
                validate(link,dict(current,idea_id='new-unbound-idea'),state,'author-01')
            for path in (None, '', '.', '..', '../proof', '/proof', 'a/b', 'a\\b'):
                save(dict(prior,execution_task_path=path))
                with self.subTest(side='prior',path=path),self.assertRaisesRegex(ValueError,'execution task path'):
                    validate(link,current,state,'author-01')
                save(prior)
                with self.subTest(side='current',path=path),self.assertRaisesRegex(ValueError,'execution task path'):
                    validate(link,dict(current,execution_task_path=path),state,'author-01')
            save(prior)
            (task/'data').write_text('tampered')
            with self.assertRaises(ValueError):validate(link,current,state,'author-01')


if __name__=='__main__':unittest.main()
