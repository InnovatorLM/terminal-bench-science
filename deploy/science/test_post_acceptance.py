"""Public retrospectives and accepted-version delivery; no network/cloud calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import post_acceptance as p
import review_dispatcher as d
from selftest_feedback import record_current_request, publish_handoff, release_handoff
from test_review_dispatcher import FakeApi


class PostAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir=d.W/'.tmp');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.config={'roles':{}}
        for role in ['author-10','evaluator-01']:
            root=self.root/role;(root/'home/role').mkdir(parents=True)
            (root/'selftest').mkdir()
            self.config['roles'][role]=dict(root=str(root),uid=0,gid=0)
        self.author=self.root/'author-10'
        self.row=dict(key='a'*40+':tasks/science/example',commit='a'*40,
                      task='tasks/science/example',snapshot_sha256='b'*64,
                      role='evaluator-01',author_role='author-10',evaluation_id='accepted',
                      generation=5,state='completed',formal_decision='accepted',scientific_acceptance=True,
                      job_id='c'*32,idea_id='old',reason='Accepted with disclosed evidence limitations.')
        self.api=FakeApi();self.api.pulls[0].update(number=42,head={'sha':'d'*40},
                         user={'login':'gewu-author-10'},html_url='http://localhost/team/terminal-bench-science/pulls/42')
        self.ledger={self.row['key']:self.row}
        for mocked in [patch.object(d,'STATE',self.root),
                       patch('selftest_review_dispatch.consume',return_value={}),
                       patch('author_submission_gate.approved',return_value=False)]:
            mocked.start();self.addCleanup(mocked.stop)
        self.clock=self.author/'home/role/.state/author-iteration/ledger.json'
        self.clock.parent.mkdir(parents=True);self.clock.write_text(json.dumps({'ideas':{'old':{'status':'finished'}}}))
        self.status=dict(role='author-10',job_id='e'*32,idea_id='old',received_at=100,
                         source_commit=self.row['commit'],source_task_path=self.row['task'])
        record_current_request(self.author,self.status)

    def feedback(self):
        return json.loads((self.author/'review-feedback.json').read_text())

    def comment(self,row=None):
        row=dict(row or self.row,proposal_thread=42)
        text=p.template(row).replace('<填写已核实的具体经验；未知或缺失证据如实说明>',
                 '真实来源有固定版本。有效迭代与失败尝试均有记录；缺失历史轨迹如实说明，下一题迁移到新数据。')
        return dict(id=701,body=text,user={'login':'gewu-author-10'},
                    html_url='http://localhost/team/terminal-bench-science/pulls/42#issuecomment-701',
                    created_at='2026-11-02T10:00:00Z')

    def test_only_public_exact_author_version_thread_comment_counts(self):
        row=dict(self.row,proposal_thread=42);comment=self.comment()
        self.assertIn('Proposal thread: `42`', comment['body'])
        self.assertIn('Evaluation ID: `accepted`', comment['body'])
        self.assertIsNotNone(p.public_lessons_comment([comment],row,'author-10',42))
        for changed in [dict(comment,user={'login':'gewu-evaluator-01'}),
                        dict(comment,body=comment['body'].replace('a'*40,'f'*40)),
                        dict(comment,body=comment['body'].replace('b'*64,'f'*64)),
                        dict(comment,html_url='http://localhost/team/terminal-bench-science/issues/25'),
                        dict(comment,body=p.template(row)),
                        dict(comment,body=comment['body'].split('## 迁移思路与下一题')[0])]:
            self.assertIsNone(p.public_lessons_comment([changed],row,'author-10',42))
        self.assertIsNone(p.public_lessons_comment([comment],row,'author-10',25))
        self.assertIsNone(p.public_lessons_comment([comment],dict(row,scientific_acceptance=False),'author-10',42))
        (self.author/'lessons.md').write_text(comment['body'])
        self.assertIsNone(p.public_lessons_comment([],row,'author-10',42))

    def test_historical_comment_without_optional_metadata_remains_valid(self):
        row=dict(self.row,proposal_thread=42)
        comment=self.comment()
        body='\n'.join(line for line in comment['body'].splitlines()
                        if not line.startswith(('Proposal thread:', 'Evaluation ID:')))
        legacy=dict(comment,body=body)
        self.assertIsNotNone(p.public_lessons_comment([legacy],row,'author-10',42))

    def test_direct_acceptance_without_thread_is_explicitly_blocked(self):
        class EmptyApi:
            base_url='http://localhost:3000'
            def pages(self, path):
                return []
        row=dict(self.row)
        key=row.pop('key')
        d.sync_post_acceptance(EmptyApi(),self.config,{key:row},[])
        feedback=self.feedback()
        self.assertEqual(feedback['post_acceptance_state'],'proposal_thread_required')
        self.assertEqual(feedback['post_acceptance_blocker'],
                         'accepted version has no bound proposal thread')
        self.assertIn('no bound proposal thread',feedback['next_action'])
        self.assertTrue(p.blocks_new_idea(feedback))

    def test_comments_support_paginated_and_legacy_unpaginated_servers(self):
        api=d.Api('test-token')
        comments=[{'id':i} for i in range(56)]
        with patch.object(api,'call',return_value=comments) as call:
            self.assertEqual(api.pages('/issues/42/comments'),comments)
            self.assertEqual(call.call_count,2)
        with patch.object(api,'call',side_effect=[comments[:5],comments[5:10],comments[10:12]]) as call:
            self.assertEqual(api.pages('/issues/42/comments'),comments[:12])
            self.assertEqual(call.call_count,3)

    def test_reused_finished_idea_gets_original_thread_and_stale_pr_cannot_overwrite(self):
        original=dict(self.row)
        d.reconcile(self.api,self.config,self.ledger)
        feedback=self.feedback()
        self.assertEqual(feedback['formal_decision'],'accepted')
        self.assertEqual(feedback['commit'],'a'*40)
        self.assertEqual(feedback['proposal_thread'],42)
        self.assertEqual(feedback['job_id'],'e'*32)
        self.assertFalse(feedback['author_must_pause'])
        self.assertTrue(p.blocks_new_idea(feedback))
        self.assertIn('Accepted commit: `'+'a'*40+'`',feedback['lessons_template'])
        d.publish_author_feedback(self.api.pulls[0],dict(self.row,state='waiting_author',
                                 scientific_acceptance=False,commit='d'*40),self.config)
        self.assertEqual(self.feedback(),feedback)
        for field in ('job_id','commit','snapshot_sha256','reason','generation'):
            self.assertEqual(self.row[field],original[field])
        self.assertEqual(json.loads((self.root/'pending-submissions.json').read_text()),{})
        self.assertEqual(self.api.issues,[])

    def test_publication_completes_once_and_next_idea_is_not_overwritten(self):
        d.reconcile(self.api,self.config,self.ledger)
        self.api.comments=[self.comment()]
        d.reconcile(self.api,self.config,self.ledger)
        self.assertFalse(p.blocks_new_idea(self.feedback()))
        self.assertEqual(self.feedback()['lessons_comment']['id'],701)
        self.assertEqual(self.row['post_acceptance_state'],'completed')
        self.api.comments=[]  # Later scans/reused handoffs retain verified publication.
        d.reconcile(self.api,self.config,self.ledger)
        self.assertEqual(self.feedback()['post_acceptance_state'],'completed')
        self.clock.write_text(json.dumps({'ideas':{'old':{'status':'finished'},'new':{'status':'active'}}}))
        before=(self.author/'review-feedback.json').read_bytes()
        d.reconcile(self.api,self.config,self.ledger)
        self.assertEqual((self.author/'review-feedback.json').read_bytes(),before)
        newer=dict(self.status,job_id='f'*32,idea_id='new',received_at=200,
                   source_commit='f'*40,source_task_path='tasks/science/new')
        record_current_request(self.author,newer)
        newrow=dict(author_role='author-10',state='reviewing',commit='f'*40,task='tasks/science/new',
                    key='new',role='evaluator-01',evaluation_id='new')
        d.publish_author_feedback({},newrow,self.config)
        d.sync_direct_assignments(self.config,self.ledger)
        self.assertEqual(self.feedback()['task'],'tasks/science/new')

    def test_reused_acceptance_is_not_deleted_by_finished_idea_cleanup(self):
        state=self.root/'jobs';work=state/self.status['job_id'];work.mkdir(parents=True)
        (work/'status.json').write_text(json.dumps(self.status))
        result=dict(self.row,stage='previously_accepted_version',review_key=self.row['key'],
                    review_generation=5,evaluator='evaluator-01')
        publish_handoff(work,state,self.config['roles']['author-10'],self.status,result)
        self.assertEqual(self.feedback()['formal_decision'],'accepted')
        from selftest_feedback import current_request
        release_handoff(self.author,current_request(self.author))
        self.assertEqual(self.feedback()['formal_decision'],'accepted')

    def test_accepted_retrospective_survives_newer_failed_revision_without_accepting_it(self):
        newer=dict(self.status,job_id='f'*32,received_at=200,source_commit='f'*40)
        record_current_request(self.author,newer)
        d.publish_author_feedback(self.api.pulls[0],dict(author_role='author-10',
              commit='f'*40,task=self.row['task'],state='waiting_author',stage='completeness'),self.config)
        before=(self.author/'review-feedback.json').read_bytes()
        d.sync_post_acceptance(self.api,self.config,self.ledger,self.api.pulls)
        self.assertEqual((self.author/'review-feedback.json').read_bytes(),before)
        pending=json.loads((self.author/'post-acceptance.json').read_text())['pending']
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]['commit'],'a'*40)
        self.assertEqual(pending[0]['proposal_thread'],42)
        self.assertEqual(pending[0]['formal_decision'],'accepted')

    def test_iteration_cli_waits_for_publication_then_allows_next_idea(self):
        import author_iteration_clock as clock
        import contextlib,io
        d.reconcile(self.api,self.config,self.ledger)
        with patch.object(clock,'Path',side_effect=lambda path:self.author/path.lstrip('/')):
            with patch('sys.argv',['role-iteration','start','next-idea']):
                with self.assertRaisesRegex(SystemExit,'Markdown lessons reply'):
                    clock.main()
            self.api.comments=[self.comment()]
            d.reconcile(self.api,self.config,self.ledger)
            with patch('sys.argv',['role-iteration','start','next-idea']),contextlib.redirect_stdout(io.StringIO()):
                clock.main()
        self.assertEqual(json.loads(self.clock.read_text())['ideas']['next-idea']['status'],'active')

    def test_finished_retrospective_retires_old_failed_revision_prompt(self):
        # The most recent self-test may be a failed revision of the accepted
        # task. Closing that idea and publishing lessons must still allow moving on.
        newer=dict(self.status,job_id='f'*32,received_at=200,source_commit='f'*40)
        record_current_request(self.author,newer)
        self.row.update(proposal_thread=42,post_acceptance_state='completed',
                        lessons_comment=p.comment_summary(self.comment(),42))
        self.clock.write_text(json.dumps({'ideas':{'old':{'status':'finished'},
                                                 'new':{'status':'active'}}}))
        d.publish_author_feedback(self.api.pulls[0],dict(author_role='author-10',
              commit='d'*40,task=self.row['task'],state='waiting_author',
              stage='version_reconciliation',pr=42),self.config)
        old=self.feedback()
        d.reconcile(self.api,self.config,self.ledger)
        self.assertFalse((self.author/'review-feedback.json').exists())
        self.assertFalse((self.author/'review-current-assignment.json').exists())
        archives=list((self.root/'retired-author-feedback').glob('*.json'))
        self.assertEqual(len(archives),1)
        archived=json.loads(archives[0].read_text())
        self.assertEqual(archived['feedback'],old)
        self.assertEqual(archived['accepted_key'],self.row['key'])
        self.assertEqual(json.loads((self.root/'pending-submissions.json').read_text()),{})
        d.reconcile(self.api,self.config,self.ledger)
        self.assertFalse((self.author/'review-feedback.json').exists())
        self.assertEqual(self.api.issues,[])
        # Deliberately revisiting the same task is new work, not an obsolete PR.
        self.clock.write_text(json.dumps({'ideas':{'old':{'status':'finished'},
                                                 'revision':{'status':'active'}}}))
        record_current_request(self.author,dict(newer,job_id='1'*32,idea_id='revision',received_at=300))
        d.reconcile(self.api,self.config,self.ledger)
        self.assertEqual(self.feedback()['stage'],'version_reconciliation')
        self.assertEqual(self.feedback()['commit'],'d'*40)

    def test_retired_proposal_cannot_clear_feedback_for_a_different_task(self):
        self.row.update(proposal_thread=42,post_acceptance_state='completed',
                        lessons_comment=p.comment_summary(self.comment(),42))
        self.clock.write_text(json.dumps({'ideas':{'old':{'status':'finished'},
                                                 'new':{'status':'active'}}}))
        newer=dict(self.status,job_id='f'*32,idea_id='new',received_at=200,
                   source_commit='f'*40,source_task_path='tasks/science/new')
        record_current_request(self.author,newer)
        d.publish_author_feedback({},dict(author_role='author-10',commit='f'*40,
              task='tasks/science/new',key='new',state='waiting_author',
              stage='completeness'),self.config)
        before=(self.author/'review-feedback.json').read_bytes()
        d.reconcile(self.api,self.config,self.ledger)
        self.assertEqual((self.author/'review-feedback.json').read_bytes(),before)
        self.assertFalse((self.root/'retired-author-feedback').exists())
        self.assertEqual(json.loads((self.root/'pending-submissions.json').read_text()),{})

    def test_policy_installs_idempotently_without_losing_skills_or_reset_history(self):
        from author_post_acceptance_policy import install, BEGIN
        original='Skill instructions\n<!-- operator-reset:start -->\nHistorical reset record\n'
        for role,entry in self.config['roles'].items():
            (Path(entry['root'])/'instruction.md').write_text(original)
            path=self.root/'.config/gewu-multiverse/agents'/role/'instruction.md'
            path.parent.mkdir(parents=True);path.write_text(original)
        install(self.config,self.root)
        first=(self.author/'instruction.md').read_text()
        install(self.config,self.root)
        self.assertEqual((self.author/'instruction.md').read_text(),first)
        self.assertEqual(first.count(BEGIN),1)
        self.assertIn(original,first)
        for word in ['原帖','Markdown','迁移','下一题','JSON']:
            self.assertIn(word,first)
        self.assertEqual((self.root/'evaluator-01/instruction.md').read_text(),original)


if __name__=='__main__':unittest.main()
