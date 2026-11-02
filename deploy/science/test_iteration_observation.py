import copy
import json
from pathlib import Path
import tempfile
import unittest
from evaluation_queue import W
from author_iteration_clock import transition
from iteration_observation import observe


class ObservationTests(unittest.TestCase):
    def test_preserves_observed_prefix_and_rejects_budget_reset(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            directory=Path(raw)/'private';state={}
            transition(state,'start','idea-a',1)
            first=observe(directory,'author-01',state,True,2)
            transition(state,'platform-wait','idea-a',3,reason='PRIVATE ARBITRARY NOTE')
            second=observe(directory,'author-01',state,True,4)
            archives=list((directory/'history/author-01').glob('*.json'))
            self.assertEqual(len(archives),1)
            self.assertEqual(json.loads(archives[0].read_text()),first)
            second=observe(directory,'author-01',state,True,4)
            self.assertEqual(len(list((directory/'history/author-01').glob('*.json'))),1)
            self.assertEqual(second['ideas']['idea-a']['first_observed_at'],2)
            self.assertTrue(second['previous_observation_sha256'])
            self.assertNotIn('PRIVATE ARBITRARY NOTE',(directory/'author-01.json').read_text())
            for bad in ({'ideas':{}},copy.deepcopy(state)):
                if bad['ideas']:bad['ideas']['idea-a']['events'][0]['at']=2
                with self.assertRaises(ValueError):observe(directory,'author-01',bad,True,5)
            self.assertEqual(json.loads((directory/'author-01.json').read_text()),second)
            self.assertEqual((directory/'author-01.json').stat().st_mode&0o777,0o600)

    def test_future_event_and_unsafe_destination_rejected(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            directory=Path(raw)/'private';state={};transition(state,'start','idea',10)
            with self.assertRaises(ValueError):observe(directory,'author-01',state,True,9)
            directory.chmod(0o755)
            with self.assertRaises(ValueError):observe(directory,'author-01',state,True,11)


if __name__=='__main__':unittest.main()
