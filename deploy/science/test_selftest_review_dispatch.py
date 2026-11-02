import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from evaluation_queue import W
from selftest_review_dispatch import consume
import review_dispatcher


class DirectDispatchTests(unittest.TestCase):
    def test_full_request_delivery_exclusivity_recovery_and_no_completed_replay(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);directory=root/'.state/gewu-multiverse/review-requests';directory.mkdir(parents=True)
            cfg={'roles':{}}
            for name in ('author-01','author-02','evaluator-01'):
                home=root/name/'home/role';home.mkdir(parents=True)
                cfg['roles'][name]={'root':str(root/name),'uid':os.getuid(),'gid':os.getgid()}
            row=dict(state='ready_for_independent_review',commit='a'*40,task='tasks/proof',
                     author_role='author-01',evaluation_id='proof',snapshot_sha256='b'*64,job_id='c'*32)
            (directory/'one.json').write_text(json.dumps(row))
            (directory/'two.json').write_text(json.dumps(dict(row,commit='d'*40,author_role='author-02')))
            ledger={}
            with patch('author_submission_gate.approved',return_value=True),patch.object(review_dispatcher,'registered_execution',return_value={'evaluation_id':'proof','snapshot_sha256':'b'*64}):
                pending=consume(cfg,ledger,root,root)
                self.assertEqual(len(ledger),1)
                self.assertEqual(next(iter(pending.values()))['stage'],'evaluator_capacity')
                assignment=root/'evaluator-01/home/role/review-assignment.json'
                self.assertEqual(json.loads(assignment.read_text())['commit'],'a'*40)
                self.assertEqual(assignment.stat().st_mode&0o777,0o600)
                assignment.unlink()
                consume(cfg,ledger,root,root)
                self.assertTrue(assignment.exists())
                key=next(iter(ledger));ledger[key]['state']='completed'
                (directory/'two.json').unlink()
                consume(cfg,ledger,root,root)
                self.assertEqual(ledger[key]['state'],'completed')
                self.assertEqual(len(ledger),1)

    def test_missing_preflight_cannot_allocate_evaluator(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);directory=root/'.state/gewu-multiverse/review-requests';directory.mkdir(parents=True)
            (directory/'one.json').write_text(json.dumps(dict(state='ready_for_independent_review',commit='a'*40,task='tasks/proof',author_role='author-01')))
            with patch('author_submission_gate.approved',return_value=False):
                ledger={};pending=consume({'roles':{'author-01':{}}},ledger,root,root)
            self.assertFalse(ledger)
            self.assertEqual(pending['one']['state'],'waiting_author')

    def test_forgejo_pagination_does_not_stop_at_five(self):
        api=review_dispatcher.Api('fixture')
        with patch.object(api,'call',side_effect=[list(range(5)),list(range(5,10)),list(range(10,12))]) as call:
            self.assertEqual(api.pages('/pulls?state=open'),list(range(12)))
            self.assertEqual(call.call_count,3)

    def test_queued_current_version_survives_old_pr_and_handoff_replay_until_assigned(self):
        from selftest_feedback import record_current_request,publish_handoff
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);directory=root/'.state/gewu-multiverse/review-requests';directory.mkdir(parents=True)
            cfg={'roles':{}}
            for role in ('author-01','evaluator-01'):
                home=root/role/'home/role';home.mkdir(parents=True)
                cfg['roles'][role]={'root':str(root/role),'uid':os.getuid(),'gid':os.getgid()}
            author=root/'author-01';(author/'selftest').mkdir()
            clock=author/'home/role/.state/author-iteration';clock.mkdir(parents=True)
            (clock/'ledger.json').write_text(json.dumps({'ideas':{'current-idea':{'status':'active'}}}))
            status=dict(role='author-01',idea_id='current-idea',received_at=100,job_id='c'*32,
                        source_commit='a'*40,source_task_path='tasks/proof')
            record_current_request(author,status)
            request=dict(state='ready_for_independent_review',commit='a'*40,task='tasks/proof',
                         author_role='author-01',evaluation_id='proof',snapshot_sha256='b'*64,
                         job_id='c'*32,idea_id='current-idea',received_at=100)
            (directory/'one.json').write_text(json.dumps(request))
            ledger={'busy':{'role':'evaluator-01','state':'reviewing','commit':'d'*40,'task':'tasks/other'}}
            with patch('author_submission_gate.approved',return_value=True),patch.object(
                    review_dispatcher,'registered_execution',return_value={'evaluation_id':'proof','snapshot_sha256':'b'*64}):
                pending=consume(cfg,ledger,root,root)
                self.assertEqual(len(ledger),1)
                feedback=json.loads((author/'review-feedback.json').read_text())
                self.assertEqual(feedback['stage'],'evaluator_capacity');self.assertTrue(feedback['author_must_pause'])
                self.assertIn('dispatch resumes automatically',feedback['next_action'])
                # An obsolete PR is still scanned, but cannot ask this author
                # to rerun a passed self-test while its version waits in queue.
                review_dispatcher.publish_author_feedback({},dict(author_role='author-01',pr=36,
                    commit='e'*40,task='tasks/proof',state='waiting_author',stage='author_container_preflight'),cfg)
                self.assertEqual(json.loads((author/'review-feedback.json').read_text()),feedback)
                state=root/'selftests';work=state/status['job_id'];work.mkdir(parents=True)
                publish_handoff(work,state,cfg['roles']['author-01'],status,{'state':'ready_for_independent_review'})
                self.assertEqual(json.loads((author/'review-feedback.json').read_text()),feedback)
                ledger['busy']['state']='completed'
                consume(cfg,ledger,root,root)
                new=json.loads((author/'review-feedback.json').read_text())
                self.assertEqual(new['state'],'reviewing');self.assertEqual(new['job_id'],status['job_id'])
                self.assertEqual(new['evaluator'],'evaluator-01')
                # A newer accepted job supersedes the wait; an old queued
                # request cannot pause that different author selection.
                record_current_request(author,dict(status,job_id='f'*32,received_at=101,source_commit='f'*40))
                review_dispatcher.publish_author_feedback({},dict(next(iter(pending.values())),
                    state='waiting_platform',feedback_source='selftest_handoff'),cfg)
                self.assertNotEqual(json.loads((author/'review-feedback.json').read_text()).get('stage'),'evaluator_capacity')


if __name__=='__main__':unittest.main()
