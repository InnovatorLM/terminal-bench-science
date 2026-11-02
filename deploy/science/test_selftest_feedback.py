"""Scheduling integration with local receipts only; no model or ACS calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation_queue import W
from selftest_feedback import publish_handoff,record_current_request,current_request
from selftest_handoff import reconcile
from selftest_budget import BudgetEvidenceRequired
from review_dispatcher import publish_author_feedback


class SelftestFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=W/'.tmp')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.state=self.base/'state';self.work=self.state/('a'*32);self.work.mkdir(parents=True)
        self.root=self.base/'role';(self.root/'selftest/results').mkdir(parents=True)
        self.entry=dict(root=str(self.root),gid=0,uid=0)
        self.config={'roles':{'author-01':self.entry,'evaluator-01':dict(root=str(self.base/'evaluator'))}}
        self.clock=self.root/'home/role/.state/author-iteration/ledger.json'
        self.clock.parent.mkdir(parents=True)
        self.clock.write_text(json.dumps({'ideas':{'idea':{'status':'active'}}}))
        self.status=dict(schema_version=2,role='author-01',job_id=self.work.name,idea_id='idea',
                         source_commit='b'*40,source_task_path='tasks/example',received_at=100,
                         status='selftest_passed')
        (self.work/'status.json').write_text(json.dumps(self.status))
        (self.work/'request.json').write_text(json.dumps(dict(request_review=True,idea_id='idea')))
        self.result=dict(state='waiting_platform',stage='verify_iteration_and_budget',
                         reason='Recover authentic history.',selftest_rerun_required=False)

    def publish(self,result=None,status=None,work=None):
        publish_handoff(work or self.work,self.state,self.entry,status or self.status,result or self.result)

    def feedback(self):
        return json.loads((self.root/'review-feedback.json').read_text())

    def test_budget_gap_is_formal_exact_platform_pause_without_clock_or_execution_changes(self):
        original=(self.work/'status.json').read_bytes();clock=self.clock.read_bytes()
        error=BudgetEvidenceRequired('iteration_observation_history_gap','Recover authentic history.')
        with patch('selftest_handoff.handoff',side_effect=error) as handoff:
            reconcile(self.work,self.state,self.entry,self.base/'universe')
        handoff.assert_called_once()
        feedback=self.feedback()
        self.assertEqual(feedback['state'],'waiting_platform')
        self.assertTrue(feedback['author_must_pause'])
        for key,expected in [('job_id',self.work.name),('idea_id','idea'),('commit','b'*40),('task','tasks/example')]:
            self.assertEqual(feedback[key],expected)
            self.assertEqual(current_request(self.root)[key],expected)
        self.assertFalse(feedback['selftest_rerun_required'])
        self.assertEqual((self.work/'status.json').read_bytes(),original)
        self.assertEqual(self.clock.read_bytes(),clock)

    def test_old_pr_and_same_commit_unqualified_scan_cannot_overwrite_platform_wait(self):
        self.publish();before=(self.root/'review-feedback.json').read_bytes()
        row=dict(author_role='author-01',pr=30,task='tasks/example',state='waiting_author',stage='author_container_preflight')
        for commit in ('c'*40,'b'*40):
            publish_author_feedback({'number':30},dict(row,commit=commit),self.config)
            self.assertEqual((self.root/'review-feedback.json').read_bytes(),before)
        # A real assignment of this exact version may take over scheduling.
        publish_author_feedback({},dict(row,commit='b'*40,state='reviewing',
            evaluation_id='execution',role='evaluator-01',evaluator_activity_confirmed=True),self.config)
        self.assertEqual(self.feedback()['state'],'reviewing')
        self.assertTrue(self.feedback()['author_must_pause'])

    def test_newer_failed_job_blocks_old_success_even_before_marker_migration(self):
        for kind in ('failed','validating'):
            with self.subTest(kind=kind):
                newer=self.state/('c'*32);newer.mkdir(exist_ok=True)
                status=dict(self.status,job_id=newer.name,source_commit='d'*40,received_at=110,status=kind)
                if kind=='validating':status.pop('source_commit')
                (newer/'status.json').write_text(json.dumps(status))
                self.publish()
                self.assertFalse((self.root/'review-feedback.json').exists())

    def test_new_accepted_request_invalidates_binding_and_cannot_be_rewound(self):
        self.publish();old=current_request(self.root)
        newer=dict(self.status,job_id='c'*32,source_commit='d'*40,received_at=110)
        self.assertTrue(record_current_request(self.root,newer))
        self.assertFalse(record_current_request(self.root,self.status))
        self.publish()
        self.assertNotEqual(old['job_id'],current_request(self.root)['job_id'])
        self.assertEqual(current_request(self.root)['commit'],'d'*40)
        self.assertFalse((self.root/'review-feedback.json').exists())
        old_review=dict(author_role='author-01',job_id=self.work.name,idea_id='idea',
            task='tasks/example',commit='b'*40,state='reviewing',evaluation_id='old',
            role='evaluator-01',evaluator_activity_confirmed=True)
        publish_author_feedback({},old_review,self.config)
        self.assertFalse((self.root/'review-feedback.json').exists())

    def test_switched_idea_does_not_get_parked_by_old_result(self):
        self.publish()
        self.clock.write_text(json.dumps({'ideas':{'idea':{'status':'finished'},'new':{'status':'active'}}}))
        self.publish()
        self.assertFalse((self.root/'review-feedback.json').exists())

    def test_same_job_recovery_releases_pause_and_cached_success_still_releases(self):
        self.publish()
        ready=dict(state='ready_for_independent_review',scientific_acceptance=False)
        with patch('selftest_handoff.handoff',return_value=ready) as handoff:
            reconcile(self.work,self.state,self.entry,self.base/'universe')
        handoff.assert_called_once()
        self.assertFalse((self.root/'review-feedback.json').exists())
        # Crash recovery: handoff was written but feedback release was not.
        self.publish()
        (self.work/'handoff-qualified.json').write_text('{}')
        with patch('selftest_handoff.validate_receipt'),patch('selftest_handoff.handoff') as handoff:
            reconcile(self.work,self.state,self.entry,self.base/'universe')
        handoff.assert_not_called()
        self.assertFalse((self.root/'review-feedback.json').exists())

    def test_recovery_never_clears_real_evaluator_ownership(self):
        self.publish()
        row=dict(author_role='author-01',task='tasks/example',commit='b'*40,state='reviewing',
                 evaluation_id='execution',role='evaluator-01',evaluator_activity_confirmed=True)
        publish_author_feedback({},row,self.config)
        before=(self.root/'review-feedback.json').read_bytes()
        self.publish(dict(state='ready_for_independent_review'))
        self.assertEqual((self.root/'review-feedback.json').read_bytes(),before)

    def test_same_accepted_version_releases_new_job_without_qualification_or_cloud(self):
        from formal_review_decision import record,apply
        from completed_review_reuse import lookup
        universe=self.base/'universe';state=universe/'.state/gewu-multiverse/review-dispatcher'
        state.mkdir(parents=True)
        self.status.update(source_git_tree_verified=True,snapshot_sha256='c'*64)
        (self.work/'status.json').write_text(json.dumps(self.status))
        key='b'*40+':tasks/example'
        row=dict(key=key,commit='b'*40,task='tasks/example',snapshot_sha256='c'*64,
                 author_role='author-01',role='evaluator-01',evaluation_id='accepted-v1',
                 state='completed',stage='final',generation=5,reason='Five valid runs; accepted.',
                 job_id='d'*32,review_recommendation='accept')
        ledger={key:row};path=state/'ledger.json';path.write_text(json.dumps(ledger))
        record(state,key,'operator','User authorized acceptance of this exact frozen task version.')
        apply(state,ledger);path.write_text(json.dumps(ledger))
        self.publish()
        self.assertTrue(self.feedback()['author_must_pause'])
        old_status=(self.work/'status.json').read_bytes();clock=self.clock.read_bytes()
        with patch('selftest_handoff.handoff') as handoff:
            reconcile(self.work,self.state,self.entry,universe)
            reconcile(self.work,self.state,self.entry,universe)
            handoff.assert_not_called()
        result=self.feedback()
        self.assertEqual(result['state'],'completed')
        self.assertEqual(result['formal_decision'],'accepted')
        self.assertFalse(result['author_must_pause'])
        self.assertEqual(result['job_id'],self.work.name)
        self.assertEqual(result['key'],key)
        self.assertEqual(result['generation'],5)
        self.assertEqual((self.work/'status.json').read_bytes(),old_status)
        self.assertEqual(self.clock.read_bytes(),clock)
        self.assertEqual(json.loads(path.read_text()),ledger)
        self.assertFalse((universe/'.state/gewu-multiverse/review-requests').exists())
        for wrong in [dict(self.status,source_commit='e'*40),dict(self.status,snapshot_sha256='f'*64),
                      dict(self.status,role='author-02'),dict(self.status,source_git_tree_verified=False)]:
            self.assertIsNone(lookup(wrong,universe))

    def test_same_time_different_job_cannot_guess_latest(self):
        self.assertTrue(record_current_request(self.root,self.status))
        self.assertFalse(record_current_request(self.root,dict(self.status,job_id='c'*32)))
        self.assertEqual(current_request(self.root)['job_id'],self.work.name)

    def test_new_plain_selftest_invalidates_old_pause_without_claiming_idea(self):
        self.publish()
        newer=dict(self.status,job_id='c'*32,source_commit='d'*40,received_at=110)
        newer.pop('idea_id')
        self.assertTrue(record_current_request(self.root,newer))
        self.assertIsNone(current_request(self.root)['idea_id'])
        self.assertFalse(record_current_request(self.root,self.status))
