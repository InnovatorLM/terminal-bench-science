import unittest
from unittest.mock import patch
from review_dispatcher import fingerprint
class WakeTests(unittest.TestCase):
    def test_self_comments_do_not_wake(self):
        pr={'head':{'sha':'abc'}}
        own={'id':1,'updated_at':'now','user':{'login':'gewu-evaluator-01'}}
        self.assertEqual(fingerprint(pr,[]),fingerprint(pr,[own]))
        author={'id':2,'updated_at':'now','user':{'login':'gewu-author-10'}}
        self.assertNotEqual(fingerprint(pr,[]),fingerprint(pr,[author]))
        self.assertNotEqual(fingerprint(pr,[]),fingerprint({'head':{'sha':'def'}},[]))

class FakeApi:
    def __init__(self):
        self.pulls=[{'number':30,'title':'Candidate','body':'tasks/science/example','head':{'sha':'abc'}}]
        self.comments=[];self.issues=[]
    def pages(self,path):
        if path.startswith('/pulls'):return self.pulls
        if '/comments' in path:return self.comments
        return self.issues
    def call(self,method,path,data=None):
        if '/contents/' in path:return [{'name':'tests','sha':'tree-id','type':'dir'}]
        if method=='POST':
            issue=dict(data,number=len(self.issues)+1,state='open');self.issues.append(issue);return issue
        number=int(path.rsplit('/',1)[1]);issue=self.issues[number-1]
        if method=='PATCH':issue.update(data)
        return issue

class DispatchTests(unittest.TestCase):
    @patch('author_submission_gate.approved', return_value=False)
    def test_current_pr_keeps_completed_history_without_restarting_review(self, approved):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);cfg={'roles':{}}
            for role in ['evaluator-01','author-10','curator']:
                (root/role/'home/role').mkdir(parents=True)
                cfg['roles'][role]={'root':str(root/role),'uid':os.getuid(),'gid':os.getgid()}
            old='a'*40;new='b'*40;task='tasks/science/example';key=old+':'+task
            row=dict(key=key,role='evaluator-01',author_role='author-10',commit=old,
                     task=task,generation=5,state='completed',stage='final_review',
                     evaluation_id='old-execution',snapshot_sha256='c'*64,
                     reason='Completed review, historical Pod observations unavailable',scientific_acceptance=False)
            ledger={key:row};api=FakeApi()
            api.pulls[0].update(head={'sha':new},user={'login':'gewu-author-10'})
            with patch.object(m,'STATE',root):
                m.reconcile(api,cfg,ledger)
                current=json.loads((root/'author-10/review-feedback.json').read_text())
                self.assertEqual(current['commit'],new)
                self.assertEqual(current['stage'],'version_reconciliation')
                self.assertEqual(current['prior_reviews'][0]['commit'],old)
                self.assertFalse(current['author_must_pause'])
                self.assertEqual(current['prior_reviews'][0]['review_recommendation'],'unrecorded')
                self.assertEqual(current['prior_reviews'][0]['formal_decision'],'not_recorded')
                before=(root/'author-10/review-feedback.json').read_bytes()
                m.sync_direct_assignments(cfg,ledger)
                self.assertEqual((root/'author-10/review-feedback.json').read_bytes(),before)
                m.reconcile(api,cfg,ledger)
                handoffs=json.loads((root/'curator/review-version-handoffs.json').read_text())
                self.assertEqual(handoffs['handoffs']['30:'+new]['decision_owner'],'curator')
                self.assertEqual(handoffs['handoffs']['30:'+new]['owner'],'author-10')
                self.assertEqual(list(ledger),[key])
                self.assertEqual(row['state'],'completed')
                self.assertEqual(api.issues,[])
                # A closed PR no longer creates reconciliation work.
                api.pulls=[];m.reconcile(api,cfg,ledger)
                self.assertEqual(json.loads((root/'curator/review-version-handoffs.json').read_text())['handoffs'],{})

    def test_prior_reviews_do_not_cross_author_or_task_boundaries(self):
        import review_dispatcher as m
        base=dict(state='completed',commit='old',task='tasks/one',author_role='author-10')
        ledger={'valid':base,'other-author':dict(base,author_role='author-01'),
                'other-task':dict(base,task='tasks/two'),'same-commit':dict(base,commit='new'),
                'in-progress':dict(base,state='reviewing')}
        self.assertEqual([v['key'] for v in m.completed_versions(ledger,'author-10','tasks/one','new')],['valid'])
        self.assertEqual(m.completed_versions(ledger,None,'tasks/one','new'),[])

    def test_matching_completed_receipt_exposes_recommendation_without_accepting(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);home=root/'evaluator-01/home/role';home.mkdir(parents=True)
            (root/'author-10').mkdir()
            cfg={'roles':{'evaluator-01':{'root':str(root/'evaluator-01'),
                                         'uid':os.getuid(),'gid':os.getgid()},
                          'author-10':{'root':str(root/'author-10'),
                                       'uid':os.getuid(),'gid':os.getgid()}}}
            key='old:tasks/one'
            row=dict(key=key,role='evaluator-01',author_role='author-10',
                     commit='old',task='tasks/one',generation=5,state='reviewing',
                     scientific_acceptance=False)
            status=dict(key=key,generation=5,state='completed',stage='final_review',
                        reason='3/5 valid runs',updated_at=100,review_recommendation='accept')
            (home/'review-status.json').write_text(json.dumps(status))
            (home/'activity-receipt.json').write_text(json.dumps(dict(status,receipt_type='evaluator-activity-v1')))
            m.advance_review(key,row,cfg,now=101)
            self.assertEqual(row['review_recommendation'],'accept')
            self.assertFalse(row['scientific_acceptance'])
            m.publish_author_feedback({},row,cfg)
            feedback=json.loads((root/'author-10/review-feedback.json').read_text())
            self.assertEqual(feedback['review_recommendation'],'accept')
            self.assertEqual(feedback['formal_decision'],'not_recorded')
            self.assertEqual(m.completed_versions({key:row},'author-10','tasks/one','new')[0]
                             ['review_recommendation'],'accept')

    def test_old_generation_cannot_overwrite_current_author_pause(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);cfg={'roles':{'author-01':{'root':str(root),'uid':os.getuid(),'gid':os.getgid()}}}
            row=dict(key='exact:task',author_role='author-01',commit='a'*40,task='tasks/example',
                     generation=2,state='reviewing',evaluator_activity_confirmed=True)
            m.publish_author_feedback({},row,cfg)
            before=(root/'review-feedback.json').read_bytes()
            m.publish_author_feedback({},dict(row,generation=1,state='completed'),cfg)
            self.assertEqual((root/'review-feedback.json').read_bytes(),before)
            self.assertEqual(json.loads((root/'review-current-assignment.json').read_text())['generation'],2)
            m.publish_author_feedback({},dict(row,state='waiting_author'),cfg)
            self.assertFalse(json.loads((root/'review-feedback.json').read_text())['author_must_pause'])

    def test_restored_direct_handoff_consumes_activity_and_preserves_identity(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);cfg={'roles':{}}
            for role in ['evaluator-01','author-10']:
                home=root/role/'home/role';home.mkdir(parents=True)
                cfg['roles'][role]={'root':str(root/role),'uid':os.getuid(),'gid':os.getgid()}
            key='abc:tasks/science/example'
            row=dict(key=key,role='evaluator-01',author_role='author-10',task='tasks/science/example',
                     commit='abc',evaluation_id='frozen-task',snapshot_sha256='a'*64,
                     generation=0,state='reviewing',job_id='preserved-selftest')
            ledger={key:row};home=root/'evaluator-01/home/role'
            original=json.dumps(row);(home/'review-assignment.json').write_text(original)
            api=FakeApi()
            with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=100):
                m.reconcile(api,cfg,ledger)
            self.assertEqual(api.issues,[])
            self.assertEqual(row['assigned_at'],100)
            feedback=json.loads((root/'author-10/review-feedback.json').read_text())
            self.assertFalse(feedback['author_must_pause'])
            active=dict(key=key,generation=0,state='reviewing',stage='independent_review',
                        reason='',updated_at=110)
            (home/'review-status.json').write_text(json.dumps(active))
            (home/'activity-receipt.json').write_text(json.dumps(dict(active,receipt_type='evaluator-activity-v1')))
            with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=115):
                m.reconcile(api,cfg,ledger)
            feedback=json.loads((root/'author-10/review-feedback.json').read_text())
            self.assertTrue(feedback['author_must_pause'])
            status=dict(key=key,generation=0,state='waiting_author',stage='completeness',
                        reason='Real verifier defect',updated_at=120)
            (home/'review-status.json').write_text(json.dumps(status))
            (home/'activity-receipt.json').write_text(json.dumps(dict(status,receipt_type='evaluator-activity-v1')))
            with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=150):
                m.reconcile(api,cfg,ledger)
            self.assertEqual(row['state'],'waiting_author')
            self.assertEqual(row['updated_at'],120)
            self.assertEqual(row['job_id'],'preserved-selftest')
            self.assertEqual((home/'review-assignment.json').read_text(),original)
            feedback=json.loads((root/'author-10/review-feedback.json').read_text())
            self.assertEqual(feedback['key'],key)
            self.assertFalse(feedback['author_must_pause'])
            self.assertEqual(api.issues,[])

    def test_invalid_existing_receipts_cannot_suppress_startup_timeout(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);cfg={'roles':{}}
            for role in ['evaluator-01','author-10']:
                (root/role/'home/role').mkdir(parents=True)
                cfg['roles'][role]={'root':str(root/role),'uid':os.getuid(),'gid':os.getgid()}
            key='abc:tasks/science/example'
            for defect in ('wrong_generation','wrong_owner','symlink'):
                with self.subTest(defect=defect):
                    home=root/'evaluator-01/home/role'
                    for p in home.iterdir():p.unlink()
                    row=dict(key=key,role='evaluator-01',author_role='author-10',task='tasks/science/example',
                             commit='abc',generation=2,state='reviewing',assigned_at=100)
                    status=dict(key=key,generation=1 if defect=='wrong_generation' else 2,
                                state='completed',updated_at=120)
                    receipt=home/'review-status.json';receipt.write_text(json.dumps(status))
                    activity=home/'activity-receipt.json';activity.write_text(json.dumps(dict(status,receipt_type='evaluator-activity-v1')))
                    if defect=='wrong_owner':
                        cfg['roles']['evaluator-01']['uid']=os.getuid()+1
                    else:cfg['roles']['evaluator-01']['uid']=os.getuid()
                    if defect=='symlink':
                        other=root/'untrusted.json';other.write_text(receipt.read_text());receipt.unlink();receipt.symlink_to(other)
                    with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=500):
                        m.sync_direct_assignments(cfg,{key:row})
                    self.assertEqual(row['state'],'waiting_platform')
                    feedback=json.loads((root/'author-10/review-feedback.json').read_text())
                    self.assertFalse(feedback['author_must_pause'])
                    self.assertIn('do not rebuild',feedback['next_action'])

    @patch('review_dispatcher.registered_execution',
           return_value={'evaluation_id':'frozen-task','snapshot_sha256':'a'*64})
    def test_direct_platform_wait_resumes_after_registration(self, execution):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);cfg={'roles':{}}
            for role in ['evaluator-01','author-10']:
                (root/role/'home/role').mkdir(parents=True)
                cfg['roles'][role]={'root':str(root/role),'uid':os.getuid(),'gid':os.getgid()}
            key='abc:tasks/science/example'
            row=dict(key=key,role='evaluator-01',author_role='author-10',
                     task='tasks/science/example',commit='abc',
                     evaluation_id='frozen-task',snapshot_sha256='a'*64,
                     generation=2,state='waiting_platform',
                     stage='immutable_execution_registration',
                     evaluator_assigned=False)
            ledger={key:row}
            with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=200):
                m.sync_direct_assignments(cfg,ledger)
            self.assertEqual(row['state'],'reviewing')
            self.assertEqual(row['generation'],3)
            self.assertTrue(row['evaluator_assigned'])
            wake=json.loads((root/'evaluator-01/review-dispatch.json').read_text())
            self.assertEqual(wake['state'],'reviewing')
            self.assertEqual(wake['generation'],3)
            execution.assert_called_once_with('abc','tasks/science/example')

    def test_execution_lookup_matches_exact_version_and_rejects_changed_snapshot(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        from evaluation_queue import digest_tree
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);settings=root/'.config/science-evaluation';settings.mkdir(parents=True)
            task=root/'snapshot';task.mkdir();(task/'data').write_text('frozen')
            registry=settings/'registry.json'
            entry=dict(source_commit='a'*40,source_task_path='tasks/example',snapshot=str(task),snapshot_sha256=digest_tree(task))
            registry.write_text(json.dumps({'example-v1':entry}))
            (settings/'queue.json').write_text(json.dumps({'registry':str(registry)}))
            with patch.dict(os.environ,GEWU_UNIVERSE_ROOT=str(root)):
                self.assertEqual(m.registered_execution('a'*40,'tasks/example')['evaluation_id'],'example-v1')
                self.assertIsNone(m.registered_execution('b'*40,'tasks/example'))
                self.assertIsNone(m.registered_execution('a'*40,'tasks/other'))
                registry.write_text(json.dumps({'example-v1':entry,'ambiguous':entry}))
                with self.assertRaises(ValueError):m.registered_execution('a'*40,'tasks/example')
                registry.write_text(json.dumps({'example-v1':entry}))
                (task/'data').write_text('changed')
                with self.assertRaises(ValueError):m.registered_execution('a'*40,'tasks/example')

    @patch('author_submission_gate.approved',side_effect=[ValueError('private evidence details'),False])
    def test_invalid_receipt_does_not_block_other_prs(self,approved):
        import tempfile,json
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as d,patch.object(m,'STATE',Path(d)):
            api=FakeApi()
            api.pulls.append(dict(number=33,title='Second candidate',body='tasks/science/second',head={'sha':'def'}))
            m.reconcile(api,{'roles':{}},{})
            pending=json.loads((Path(d)/'pending-submissions.json').read_text())
            self.assertEqual(set(pending),{'30:abc','33:def'})
            self.assertEqual(pending['30:abc']['validation_error_type'],'ValueError')
            self.assertNotIn('private evidence details',json.dumps(pending))
            self.assertEqual(api.issues,[])

    @patch('review_dispatcher.registered_execution',return_value={'evaluation_id':'example-v1','snapshot_sha256':'a'*64})
    @patch('author_submission_gate.review_budget_summary',return_value={'budget_status':'legacy_budget_unknown','active_iteration_seconds':None,'iteration_cutoff_at':100,'scope':'completed_frozen_version_only'})
    @patch("author_submission_gate.approved", return_value=True)
    def test_exclusive_wait_resume_and_revision(self, approved, budget, execution):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as d:
            original=m.STATE;m.STATE=Path(d)/'control';m.STATE.mkdir()
            try:
                cfg={'roles':{}}
                for role in ['evaluator-01','evaluator-02']:
                    root=Path(d)/role;(root/'home/role').mkdir(parents=True)
                    cfg['roles'][role]={'root':str(root),'uid':os.getuid(),'gid':os.getgid()}
                api=FakeApi();ledger={}
                m.reconcile(api,cfg,ledger);m.reconcile(api,cfg,ledger)
                self.assertEqual(len(api.issues),1)
                row=ledger['30:abc'];self.assertEqual(row['role'],'evaluator-01')
                self.assertEqual(row['evaluation_id'],'example-v1')
                self.assertEqual(row['snapshot_sha256'],'a'*64)
                receipt=Path(cfg['roles'][row['role']]['root'])/'home/role/review-status.json'
                assignment=json.loads((receipt.parent/'review-assignment.json').read_text())
                self.assertEqual(assignment['budget_summary'],budget.return_value)
                receipt.write_text(json.dumps({'key':'30:abc','generation':0,'state':'waiting_author','stage':'completeness','updated_at':100}))
                (receipt.parent/'activity-receipt.json').write_text(json.dumps({'receipt_type':'evaluator-activity-v1','key':'30:abc','generation':0,'updated_at':100}))
                m.reconcile(api,cfg,ledger);self.assertEqual(api.issues[0]['state'],'closed')
                api.comments=[{'id':1,'updated_at':'now','user':{'login':'gewu-evaluator-01'}}]
                m.reconcile(api,cfg,ledger);self.assertEqual(len(api.issues),1)
                api.comments.append({'id':2,'updated_at':'later','user':{'login':'gewu-author-10'}})
                m.reconcile(api,cfg,ledger);self.assertEqual(len(api.issues),2);self.assertEqual(row['generation'],1)
                api.pulls[0]['head']['sha']='def';m.reconcile(api,cfg,ledger)
                self.assertNotIn('30:def',ledger) # No simultaneous revision ownership.
            finally:m.STATE=original

    @patch('review_dispatcher.registered_execution',return_value=None)
    @patch('author_submission_gate.approved',return_value=True)
    def test_missing_execution_waits_for_platform_without_assigning(self,approved,execution):
        import tempfile,json
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as d,patch.object(m,'STATE',Path(d)):
            api=FakeApi();ledger={}
            m.reconcile(api,{'roles':{}},ledger)
            self.assertEqual(api.issues,[])
            self.assertEqual(ledger,{})
            pending=json.loads((Path(d)/'pending-submissions.json').read_text())['30:abc']
            self.assertEqual(pending['state'],'waiting_platform')
            self.assertEqual(pending['stage'],'immutable_execution_registration')

    @patch("author_submission_gate.approved", return_value=False)
    def test_unverified_author_never_dispatched(self, approved):
        import tempfile
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as d:
            original=m.STATE;m.STATE=Path(d)
            try:
                api=FakeApi();ledger={}
                m.reconcile(api,{'roles':{}},ledger)
                self.assertEqual(api.issues,[])
                self.assertEqual(ledger,{})
                import json
                pending=json.loads((m.STATE/'pending-submissions.json').read_text())
                self.assertEqual(pending['30:abc']['stage'],'author_container_preflight')
                self.assertFalse(pending['30:abc']['evaluator_assigned'])
            finally:m.STATE=original
