import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from role_review_status import report

W=Path('/mnt/data/zhanglinfeng/workspace')

class ReviewStatusTests(unittest.TestCase):
    def test_wait_for_rollouts_requires_submission_and_normalizes_stage(self):
        from role_evaluation_batch import submit
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);home=root/'home/role';home.mkdir(parents=True)
            workspace=root/'workspace';workspace.mkdir()
            evaluation=root/'evaluation';(evaluation/'inbox').mkdir(parents=True);(evaluation/'results').mkdir()
            assignment=dict(key='key',generation=0,evaluation_id='one',snapshot_sha256='a'*64)
            (home/'review-assignment.json').write_text(json.dumps(assignment))
            with self.assertRaisesRegex(ValueError,'has not been submitted'):
                report(home,workspace,'waiting_platform','rollouts','Waiting')
            self.assertFalse((home/'review-terminal-status.json').exists())
            submit(assignment,evaluation/'inbox')
            result=report(home,workspace,'waiting_platform','rollouts','Waiting')
            self.assertEqual(result['stage'],'five_rollouts')
            self.assertEqual(report(home,workspace,'reviewing','start','retry'),result)

    def test_terminal_handoff_survives_restart_but_new_generation_can_run(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);home=root/'home';workspace=root/'workspace';home.mkdir();workspace.mkdir()
            assignment=home/'review-assignment.json'
            assignment.write_text(json.dumps({'key':'commit:task','generation':0}))
            report(home,workspace,'reviewing','reading','read assignment')
            terminal=report(home,workspace,'waiting_author','findings','contract contradicts verifier')
            again=report(home,workspace,'reviewing','reading','restarted before dispatcher poll')
            self.assertEqual(again,terminal)
            self.assertEqual(json.loads((workspace/'.gewu-review-status.json').read_text()),terminal)
            self.assertEqual(json.loads((home/'activity-receipt.json').read_text())['updated_at'],terminal['updated_at'])
            assignment.write_text(json.dumps({'key':'commit:task','generation':1}))
            self.assertEqual(report(home,workspace,'reviewing','reading','new evidence')['state'],'reviewing')

    def test_completed_recommendation_is_versioned_and_not_formal_acceptance(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);home=root/'home';workspace=root/'workspace';home.mkdir();workspace.mkdir()
            assignment=home/'review-assignment.json'
            assignment.write_text(json.dumps({'key':'old:task','generation':5}))
            with patch('role_evaluation_batch.summary',return_value={'complete':True,'difficulty_threshold_met':True}):
                completed=report(home,workspace,'completed','final_review','3/5 valid runs',
                                 recommendation='accept')
            self.assertEqual(completed['review_recommendation'],'accept')
            self.assertEqual(report(home,workspace,'completed','final_review','retry',
                                    recommendation='reject'),completed)
            assignment.write_text(json.dumps({'key':'new:task','generation':0}))
            self.assertNotIn('review_recommendation',report(home,workspace,'reviewing','start',''))
            self.assertEqual(report(home,workspace,'completed','final_review','legacy')['review_recommendation'],
                             'unrecorded')
            with self.assertRaises(ValueError):
                report(home,workspace,'reviewing','start','',recommendation='accept')

if __name__=='__main__':unittest.main()
