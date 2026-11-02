import copy
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from evaluation_queue import W
from author_iteration_clock import transition
from iteration_observation import observe


def concurrent_observe(directory,state,at,entered,release,done,results):
    from author_submission_gate import read_operator_file
    def hold_first_read(path,*args):
        value=read_operator_file(path,*args)
        if Path(path).name=='author-01.json' and entered is not None:
            entered.set()
            if not release.wait(10):raise RuntimeError('Test observer release timed out')
        return value
    try:
        with patch('iteration_observation.read_operator_file',side_effect=hold_first_read):
            observe(directory,'author-01',state,True,at)
        results.put(('ok',at))
    except Exception as error:results.put((type(error).__name__,str(error)))
    finally:done.set()


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

    def test_stale_timestamp_and_unsafe_lock_never_rewrite_evidence(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            directory=Path(raw)/'private';state={};transition(state,'start','idea',1)
            observe(directory,'author-01',state,True,10);original=(directory/'author-01.json').read_bytes()
            with self.assertRaisesRegex(ValueError,'Stale observation'):observe(directory,'author-01',state,True,9)
            lock=directory/'author-01.lock';lock.chmod(0o666)
            with self.assertRaisesRegex(ValueError,'private lock'):observe(directory,'author-01',state,True,11)
            lock.chmod(0o600);os.chown(lock,21001,21001)
            with self.assertRaisesRegex(ValueError,'private lock'):observe(directory,'author-01',state,True,11)
            os.chown(lock,0,0)
            self.assertEqual((directory/'author-01.json').read_bytes(),original)

    def test_concurrent_stale_writer_cannot_drop_new_events_or_history(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            directory=Path(raw)/'private';state={};transition(state,'start','idea',1)
            first=observe(directory,'author-01',state,True,2)
            changed=copy.deepcopy(state);transition(changed,'platform-wait','idea',3,reason='waiting')
            ctx=multiprocessing.get_context('fork');entered=ctx.Event();release=ctx.Event()
            first_done=ctx.Event();second_done=ctx.Event();results=ctx.Queue()
            writers=[ctx.Process(target=concurrent_observe,args=(directory,changed,4,entered,release,first_done,results)),
                     ctx.Process(target=concurrent_observe,args=(directory,state,5,None,None,second_done,results))]
            try:
                writers[0].start();self.assertTrue(entered.wait(5))
                writers[1].start();self.assertFalse(second_done.wait(0.2),'Writer bypassed the observation transaction lock')
                release.set()
                for process in writers:process.join(5);self.assertFalse(process.is_alive())
                outcomes=[results.get(timeout=2),results.get(timeout=2)]
                self.assertIn(('ok',4),outcomes)
                self.assertTrue(any(kind=='ValueError' and 'events rewritten' in message for kind,message in outcomes))
                current=json.loads((directory/'author-01.json').read_text())
                self.assertEqual(current['ideas']['idea']['events'][-1],{'action':'platform-wait','at':3})
                archives=list((directory/'history/author-01').glob('*.json'))
                self.assertEqual(len(archives),1);self.assertEqual(json.loads(archives[0].read_text()),first)
            finally:
                release.set()
                for process in writers:
                    if process.pid and process.is_alive():process.terminate();process.join(5)
                results.close()


if __name__=='__main__':unittest.main()
