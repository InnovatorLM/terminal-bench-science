import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from evaluation_queue import W
from iteration_observation_recovery import proposal
from selftest_budget import verify,BudgetEvidenceRequired


class RecoveryTests(unittest.TestCase):
    def fixture(self,root):
        old={'role':'author-01','observed_at':15,'ideas':{'idea':{
            'first_observed_at':10,'status':'active','events':[{'action':'start','at':0}]}}}
        latest=copy.deepcopy(old);latest['observed_at']=95
        latest['ideas']['idea'].update(first_observed_at=80,events=[{'action':'start','at':0},{'action':'platform-wait','at':85}])
        current=root/'current.json';historical=root/'history.json'
        current.write_text(json.dumps(latest));historical.write_text(json.dumps(old))
        state=root/'selftest';job=state/('a'*32);job.mkdir(parents=True)
        (job/'status.json').write_text(json.dumps(dict(role='author-01',idea_id='idea',job_id=job.name,received_at=20,trials=[])))
        return current,historical,state

    def test_matching_history_recovers_only_first_observed_and_preserves_full_budget(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);current,historical,state=self.fixture(root);original=current.read_bytes()
            result=proposal(current,historical,'author-01','idea')
            self.assertEqual(current.read_bytes(),original)
            self.assertFalse(result['live_state_modified'])
            candidate=root/'candidate.json';candidate.write_text(json.dumps(result['candidate_observation']))
            evidence=verify(state,'author-01','idea',candidate,100)
            self.assertEqual(evidence['recorded_idea_started_at'],0)
            self.assertEqual(evidence['active_iteration_seconds_upper_bound'],100)
            self.assertEqual(evidence['total_excluded_seconds'],0)
            self.assertFalse(verify(state,'author-01','idea',candidate,20000)['within_budget'])
            historical.write_text('{}')
            with self.assertRaisesRegex(ValueError,'digest mismatch'):verify(state,'author-01','idea',candidate,100)

    def test_reset_history_is_not_stitched_or_backdated(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);current,historical,state=self.fixture(root)
            value=json.loads(current.read_text());value['ideas']['idea']['events'][0]['at']=75;current.write_text(json.dumps(value))
            with self.assertRaises(BudgetEvidenceRequired) as caught:proposal(current,historical,'author-01','idea')
            self.assertEqual(caught.exception.code,'iteration_historical_prefix_conflict')
            self.assertEqual(json.loads(current.read_text())['ideas']['idea']['events'][0]['at'],75)

    def test_role_owned_or_abandoned_history_never_recovers(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);current,historical,state=self.fixture(root)
            os.chown(historical,21001,21001)
            with self.assertRaises(ValueError):proposal(current,historical,'author-01','idea')
            os.chown(historical,0,0);value=json.loads(historical.read_text())
            value['ideas']['idea']['status']='abandoned';historical.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'Abandoned'):proposal(current,historical,'author-01','idea')


if __name__=='__main__':unittest.main()
