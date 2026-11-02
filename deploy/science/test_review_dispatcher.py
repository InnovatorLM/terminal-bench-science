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
    def setUp(self):
        # These tests cover PR dispatch; the handoff consumer has its own
        # suite. Never scan a running universe's queue from unit tests.
        consumer=patch('selftest_review_dispatch.consume',return_value={})
        consumer.start();self.addCleanup(consumer.stop)

    def test_formal_acceptance_is_bound_to_completed_version_and_survives_dispatch(self):
        import json, os, tempfile
        from pathlib import Path
        import review_dispatcher as m
        from formal_review_decision import apply, record
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw)
            key='a'*40+':tasks/science/example'
            row=dict(key=key,commit='a'*40,task='tasks/science/example',
                     snapshot_sha256='b'*64,evaluation_id='old-evaluation',
                     role='evaluator-01',author_role='author-10',generation=5,
                     state='completed',stage='final_review',reason='Five valid runs: 3/5.',
                     scientific_acceptance=False)
            ledger={key:row}
            (root/'ledger.json').write_text(json.dumps(ledger))
            path=record(root,key,'workspace-operator','User authorized formal acceptance of this frozen reviewed version.')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            apply(root,ledger)
            self.assertTrue(row['scientific_acceptance'])
            self.assertEqual(row['formal_decision'],'accepted')
            cfg={'roles':{'author-10':{'root':str(root/'author-10'),
                                        'uid':os.getuid(),'gid':os.getgid()}}}
            (root/'author-10').mkdir()
            with patch.object(m,'STATE',root):
                m.publish_author_feedback({},row,cfg)
            feedback=json.loads((root/'author-10/review-feedback.json').read_text())
            self.assertEqual(feedback['formal_decision'],'accepted')
            self.assertEqual(m.completed_versions(ledger,'author-10',row['task'],'new')[0]
                             ['formal_decision'],'accepted')
            with self.assertRaisesRegex(ValueError,'frozen review'):
                apply(root,{key:dict(row,snapshot_sha256='c'*64)})

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

    def test_five_rollouts_wait_and_resume_once_for_valid_or_invalid_results(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        from role_evaluation_batch import requests
        for invalid in (False,True):
            with self.subTest(invalid=invalid),tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
                root=Path(raw);home=root/'evaluator-01/home/role';home.mkdir(parents=True)
                (root/'evaluator-01/evaluation/results').mkdir(parents=True)
                (root/'author-07/selftest').mkdir(parents=True)
                cfg={'roles':{'evaluator-01':{'root':str(root/'evaluator-01'),
                                             'uid':os.getuid(),'gid':os.getgid()},
                              'author-07':{'root':str(root/'author-07'),
                                           'uid':os.getuid(),'gid':os.getgid()}}}
                key='abc:tasks/example';row=dict(key=key,role='evaluator-01',
                    author_role='author-07',task='tasks/example',commit='abc',
                    job_id='selftest',idea_id='idea',received_at=100,
                    evaluation_id='frozen-task',snapshot_sha256='a'*64,
                    generation=0,state='waiting_platform',stage='five_rollouts',
                    assigned_at=100,evaluator_assigned=True)
                (root/'author-07/selftest/current-request.json').write_text(json.dumps(row))
                jobs=requests(row);results=root/'evaluator-01/evaluation/results'
                with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=200):
                    m.sync_direct_assignments(cfg,{key:row})
                    self.assertEqual(row['state'],'waiting_platform')
                    self.assertEqual(row['generation'],0)
                    feedback=json.loads((root/'author-07/review-feedback.json').read_text())
                    self.assertTrue(feedback['author_must_pause'])
                    m.publish_author_feedback({'number':49},dict(author_role='author-07',
                        commit='old-pr-head',task='tasks/example',state='waiting_author',
                        stage='author_container_preflight'),cfg)
                    self.assertEqual(json.loads((root/'author-07/review-feedback.json').read_text()),feedback)
                    for index,job in enumerate(jobs):
                        value=dict(job,status='solver_complete',cleanup_confirmed=True,
                            evidence_complete=True,source_version_bound=True,n_errors=0,reward=0)
                        if invalid and index==4:value['snapshot_sha256']='b'*64
                        (results/(job['job_id']+'.json')).write_text(json.dumps(value))
                        if index<4:
                            m.sync_direct_assignments(cfg,{key:row})
                            self.assertEqual(row['state'],'waiting_platform')
                    m.sync_direct_assignments(cfg,{key:row})
                    self.assertEqual(row['state'],'reviewing')
                    self.assertEqual(row['generation'],1)
                    self.assertEqual(row['batch_generation'],0)
                    self.assertEqual(row['stage'],'rollout_results_invalid' if invalid else 'rollout_results_ready')
                    assignment=json.loads((home/'review-assignment.json').read_text())
                    self.assertEqual(assignment['generation'],1)
                    self.assertEqual(requests(assignment),jobs)
                    m.sync_direct_assignments(cfg,{key:row})
                    self.assertEqual(row['generation'],1)
                    # After reading the results, the evaluator may park again
                    # for an infrastructure repair. Identical results do not wake it.
                    status=dict(key=key,generation=1,state='waiting_platform',
                                stage='five_rollouts',reason='Diagnosed; waiting for platform repair.',updated_at=201)
                    (home/'review-status.json').write_text(json.dumps(status))
                    (home/'activity-receipt.json').write_text(json.dumps(dict(status,receipt_type='evaluator-activity-v1')))
                    for _ in range(2):
                        m.sync_direct_assignments(cfg,{key:row})
                        self.assertEqual(row['generation'],1)
                        self.assertEqual(row['state'],'waiting_platform')
                    path=results/(jobs[0]['job_id']+'.json')
                    changed=json.loads(path.read_text());changed['new_evidence']='newly collected diagnostic'
                    path.write_text(json.dumps(changed))
                    m.sync_direct_assignments(cfg,{key:row})
                    self.assertEqual(row['generation'],2)
                    self.assertEqual(row['state'],'reviewing')

    def test_parked_pr_cannot_overwrite_another_active_direct_review(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        for qualified in (True,False):
            with self.subTest(qualified=qualified),tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
                root=Path(raw);home=root/'evaluator-01/home/role';home.mkdir(parents=True)
                cfg={'roles':{'evaluator-01':dict(root=str(root/'evaluator-01'),uid=os.getuid(),gid=os.getgid())}}
                api=FakeApi();api.issues=[dict(number=1,state='open')]
                busy_key='frozen:tasks/busy'
                busy=dict(key=busy_key,commit='frozen',task='tasks/busy',role='evaluator-01',
                          state='reviewing',stage='completeness',generation=0,assigned_at=100)
                old=dict(pr=30,commit='abc',task='tasks/science/example',role='evaluator-01',
                         state='waiting_platform',stage='evaluator_runner_startup',generation=0,
                         event=m.fingerprint(api.pulls[0],[]),issue=1,
                         evaluation_id='old',snapshot_sha256='a'*64)
                ledger={busy_key:busy,'30:abc':old}
                with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=200),\
                     patch('author_submission_gate.approved',return_value=qualified),\
                     patch('author_submission_gate.review_budget_summary',return_value=None),\
                     patch.object(m,'registered_execution',return_value=dict(evaluation_id='old',snapshot_sha256='a'*64)):
                    m.reconcile(api,cfg,ledger)
                self.assertEqual(json.loads((home/'review-assignment.json').read_text())['key'],busy_key)
                wake=json.loads((root/'evaluator-01/review-dispatch.json').read_text())
                self.assertEqual(wake['key'],busy_key)
                self.assertEqual(wake['state'],'reviewing')

    def test_waiting_rollouts_do_not_replace_busy_evaluator_assignment(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        from role_evaluation_batch import requests
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);home=root/'evaluator-01/home/role';home.mkdir(parents=True)
            results=root/'evaluator-01/evaluation/results';results.mkdir(parents=True)
            author=root/'author-07';(author/'selftest').mkdir(parents=True)
            cfg={'roles':{'evaluator-01':{'root':str(root/'evaluator-01'),
                                         'uid':os.getuid(),'gid':os.getgid()},
                          'author-07':{'root':str(author),'uid':os.getuid(),'gid':os.getgid()}}}
            waiting_key='old:tasks/bee';busy_key='new:tasks/other'
            waiting=dict(key=waiting_key,role='evaluator-01',author_role='author-07',
                         task='tasks/bee',commit='old',job_id='selftest',idea_id='idea',
                         received_at=100,evaluation_id='frozen-task',snapshot_sha256='a'*64,
                         generation=0,state='waiting_platform',stage='five_rollouts',
                         assigned_at=100,evaluator_assigned=True)
            busy=dict(key=busy_key,role='evaluator-01',task='tasks/other',commit='new',
                      generation=0,state='reviewing',stage='completeness',assigned_at=100)
            (author/'selftest/current-request.json').write_text(json.dumps(waiting))
            for job in requests(waiting):
                (results/(job['job_id']+'.json')).write_text(json.dumps(dict(
                    job,status='failed',cleanup_confirmed=True,evidence_complete=False)))
            ledger={waiting_key:waiting,busy_key:busy}
            with patch.object(m,'STATE',root),patch.object(m.time,'time',return_value=200):
                m.sync_direct_assignments(cfg,ledger)
                self.assertEqual(waiting['state'],'waiting_platform')
                self.assertEqual(waiting['rollout_result_status'],'rollout_results_invalid')
                self.assertIn('results are available',waiting['reason'])
                self.assertEqual(json.loads((home/'review-assignment.json').read_text())['key'],busy_key)
                self.assertEqual(json.loads((root/'evaluator-01/review-dispatch.json').read_text())['key'],busy_key)
                self.assertTrue(json.loads((author/'review-feedback.json').read_text())['author_must_pause'])
                busy['state']='completed'
                m.sync_direct_assignments(cfg,ledger)
                self.assertEqual(waiting['state'],'reviewing')
                self.assertEqual(waiting['stage'],'rollout_results_invalid')
                self.assertEqual(json.loads((home/'review-assignment.json').read_text())['key'],waiting_key)

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
                wake=json.loads((Path(cfg['roles'][row['role']]['root'])/'review-dispatch.json').read_text())
                for field in ('commit','task','evaluation_id','snapshot_sha256'):
                    self.assertEqual(wake[field],assignment[field])
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

    def test_legacy_pr_startup_failure_is_recovered_once_with_complete_wake(self):
        import tempfile,json,os
        from pathlib import Path
        import review_dispatcher as m
        with tempfile.TemporaryDirectory(dir=m.W/'.tmp') as raw:
            root=Path(raw);role=root/'evaluator-01';(role/'home/role').mkdir(parents=True)
            cfg={'roles':{'evaluator-01':dict(root=str(role),uid=os.getuid(),gid=os.getgid())}}
            api=FakeApi();api.issues=[dict(number=1,state='closed')];key='30:abc'
            row=dict(pr=30,commit='abc',task='tasks/science/example',role='evaluator-01',
                     state='waiting_platform',stage='evaluator_runner_startup',generation=1,
                     assigned_at=100,event=m.fingerprint(api.pulls[0],[]),issue=1,
                     evaluation_id='example-v1',snapshot_sha256='a'*64)
            ledger={key:row}
            with patch.object(m,'STATE',root),patch('author_submission_gate.approved',return_value=True),\
                 patch('author_submission_gate.review_budget_summary',return_value=None),\
                 patch.object(m,'registered_execution',return_value=dict(evaluation_id='example-v1',snapshot_sha256='a'*64)):
                with patch.object(m.time,'time',return_value=500):m.reconcile(api,cfg,ledger)
                self.assertEqual(row['generation'],2)
                self.assertEqual(row['state'],'reviewing')
                self.assertEqual(row['stage'],'completeness')
                wake=json.loads((role/'review-dispatch.json').read_text())
                for field in ('commit','task','evaluation_id','snapshot_sha256'):
                    self.assertEqual(wake[field],row[field])
                with patch.object(m.time,'time',return_value=900):m.reconcile(api,cfg,ledger)
                self.assertEqual(row['state'],'waiting_platform')
                self.assertEqual(row['generation'],2)
                with patch.object(m.time,'time',return_value=1000):m.reconcile(api,cfg,ledger)
                self.assertEqual(row['state'],'waiting_platform')
                self.assertEqual(row['generation'],2)

    def test_selftest_delivery_failure_parks_review_and_recovers_once(self):
        import review_dispatcher as m
        row=dict(commit='a'*40,task='tasks/proof',evaluation_id='proof',snapshot_sha256='b'*64,
                 role='evaluator-01',generation=1,state='reviewing',stage='completeness')
        cfg={'roles':{'evaluator-01':{}}}
        with patch('selftest_review_evidence.publish',side_effect=ValueError('Missing receipt')):
            self.assertFalse(m.deliver_selftest_evidence(row,cfg))
        self.assertEqual(row['state'],'waiting_platform')
        self.assertEqual(row['stage'],'selftest_evidence_delivery')
        with patch('selftest_review_evidence.publish',return_value={'job_id':'c'*32}):
            self.assertTrue(m.deliver_selftest_evidence(row,cfg,role_available=False))
            self.assertEqual(row['state'],'waiting_platform')
            self.assertTrue(m.deliver_selftest_evidence(row,cfg))
            self.assertEqual(row['state'],'reviewing')
            self.assertEqual(row['generation'],2)
            m.deliver_selftest_evidence(row,cfg)
            self.assertEqual(row['generation'],2)

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
