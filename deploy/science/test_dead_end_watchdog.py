import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import dead_end_watchdog as m
from evaluation_queue import W

class DeadEndTests(unittest.TestCase):
    def observation(self, **changes):
        return dict(dict(source_signature='a', sessions=['run-1','run-2','run-3'],
                         shared_failure=['failure'], runner_live=True, waiting=[]), **changes)

    def test_one_hour_and_three_sessions_are_required(self):
        first=m.decision({},self.observation(),100000)
        self.assertEqual(first['action'],'observe')
        self.assertEqual(m.decision(first,self.observation(),103599)['action'],'observe')
        self.assertEqual(m.decision(first,self.observation(),103600)['action'],'reset')
        self.assertEqual(m.decision(first,self.observation(sessions=['one']),103600)['action'],'insufficient_evidence')
        self.assertEqual(m.decision(first,self.observation(shared_failure=[]),103600)['action'],'insufficient_evidence')

    def test_waits_and_progress_restart_stasis_window(self):
        first=m.decision({},self.observation(),100000)
        for waiting in ('selftest','review_or_platform','local_command','selftest_platform'):
            paused=m.decision(first,self.observation(waiting=[waiting]),103600)
            self.assertEqual(paused['action'],'waiting')
            self.assertEqual(m.decision(paused,self.observation(),103601)['action'],'observe')
        changed=m.decision(first,self.observation(source_signature='new'),103600)
        self.assertEqual(changed['stable_since'],103600)
        self.assertEqual(changed['action'],'observe')
        self.assertEqual(m.decision(first,self.observation(runner_live=False),103600)['action'],'waiting')

    def test_long_session_does_not_require_artificial_restarts(self):
        observation=self.observation(sessions=['run-long'],sustained_session_failure=True)
        first=m.decision({},observation,100000)
        self.assertEqual(m.decision(first,observation,103600)['action'],'reset')

    def test_waiting_does_not_walk_workspace(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);(root/'workspace').mkdir()
            (root/'review-feedback.json').write_text('{"state":"reviewing"}')
            with patch.object(m,'source_signature') as source:
                got=m.observe('author-01',{'root':str(root),'uid':99999999},{'controller_uid':0},root/'sessions')
            source.assert_not_called()
            self.assertIn('review_or_platform',got['waiting'])

    def test_observation_is_not_rescanned_every_watchdog_tick(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);config={'roles':{'author-01':{}},'controller_uid':0}
            with patch.object(m,'observe',return_value=self.observation()) as observe:
                m.scan(config,root/'sessions',root/'monitor',now=100000)
                m.scan(config,root/'sessions',root/'monitor',now=100030)
                m.scan(config,root/'sessions',root/'monitor',now=100300)
            self.assertEqual(observe.call_count,2)

    def test_no_repeated_reset_for_unchanged_source(self):
        prior=dict(source_signature='a',stable_since=100000,last_attempt=104000,attempted_signature='a')
        self.assertEqual(m.decision(prior,self.observation(),200000)['action'],'already_attempted')

    def test_expected_controls_are_not_failure_evidence(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            run=Path(raw);path=run/'transcript.jsonl'
            events=[dict(event='tool_result',exit_code=1,output='negative control failed as expected')]*9
            path.write_text('\n'.join(json.dumps(x) for x in events))
            self.assertEqual(m.failures(run),set())
            path.write_text('\n'.join(json.dumps(dict(event='tool_result',exit_code=1,output='same defect')) for _ in range(3)))
            self.assertEqual(len(m.failures(run)),1)

    def test_long_real_transcript_is_observed_without_three_sessions(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);workspace=root/'role/workspace';workspace.mkdir(parents=True)
            (workspace/'task.py').write_text('unchanged task')
            run=root/'sessions/author-01/run-1';run.mkdir(parents=True)
            lines=[dict(event='tool_result',exit_code=1,output=f'iteration {i}\nValueError: same task defect') for i in range(9)]
            (run/'transcript.jsonl').write_text('\n'.join(json.dumps(v) for v in lines))
            with patch('iteration_lifecycle.runner_state',return_value=True):
                observation=m.observe('author-01',{'root':str(root/'role'),'uid':999999999},{'controller_uid':0},root/'sessions')
            self.assertTrue(observation['sustained_session_failure'])
            self.assertEqual(len(observation['shared_failure']),1)
            first=m.decision({},observation,100000)
            self.assertEqual(m.decision(first,observation,103600)['action'],'reset')
            inbox=root/'role/selftest/inbox';results=root/'role/selftest/results'
            inbox.mkdir(parents=True);results.mkdir()
            job='a'*32
            (inbox/(job+'.json')).write_text('{}')
            (results/(job+'.json')).write_text('{"status":"selftest_passed"}')
            with patch('iteration_lifecycle.runner_state',return_value=True):
                progressed=m.observe('author-01',{'root':str(root/'role'),'uid':999999999},{'controller_uid':0},root/'sessions')
            self.assertNotEqual(progressed['source_signature'],observation['source_signature'])
            self.assertEqual(m.decision(first,progressed,103600)['action'],'observe')

    def test_corrupt_observation_does_not_crash_watchdog(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state=Path(raw)/'monitor';state.mkdir()
            (state/'author-01.json').write_text('{partial')
            m.scan({'roles':{'author-01':{}},'controller_uid':0},Path(raw)/'sessions',state,now=100000)
            self.assertEqual(json.loads((state/'author-01.json').read_text())['action'],'inspection_required')

    def test_scan_invokes_online_reset_once_and_preserves_sources(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'observations';session=root/'.state/gewu-multiverse'
            source=root/'task.py';source.write_text('preserve me')
            config={'roles':{'author-01':{}},'controller_uid':0}
            with patch.object(m,'observe',return_value=self.observation()),patch('role_reset.online_reset',return_value={'reset_id':'receipt'}) as reset:
                m.scan(config,session,state,now=100000)
                m.scan(config,session,state,now=103600)
                m.scan(config,session,state,now=200000)
            reset.assert_called_once()
            self.assertEqual(reset.call_args.kwargs,{'apply':True})
            self.assertEqual(source.read_text(),'preserve me')
            receipt=json.loads((state/'author-01.json').read_text())
            self.assertEqual(receipt['attempted_signature'],'a')

    def test_reset_failure_does_not_spin(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state=Path(raw)/'monitor';session=Path(raw)/'.state/gewu-multiverse'
            config={'roles':{'author-01':{}},'controller_uid':0}
            with patch.object(m,'observe',return_value=self.observation()),patch('role_reset.online_reset',side_effect=RuntimeError('hold retained')) as reset:
                m.scan(config,session,state,now=100000)
                m.scan(config,session,state,now=103600)
                m.scan(config,session,state,now=200000)
            self.assertEqual(reset.call_count,1)

if __name__=='__main__':unittest.main()
