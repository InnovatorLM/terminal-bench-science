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


if __name__=='__main__':unittest.main()
