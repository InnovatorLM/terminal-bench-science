import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from evaluation_queue import W
from author_iteration_clock import transition
import iteration_lifecycle as lifecycle


class LifecycleTests(unittest.TestCase):
    def completed_acs_fixture(self, base):
        root=base/'role';sessions=base/'sessions';job='a'*32
        ledger={};transition(ledger,'start','idea-a',10)
        transition(ledger,'acs-wait','idea-a',20,run_id=job)
        request=dict(role='author-01',job_id=job,idea_id='idea-a',commit='b'*40,
                     task='tasks/proof',received_at=21)
        status=dict(schema_version=2,role='author-01',job_id=job,idea_id='idea-a',
                    source_commit=request['commit'],source_repository='repo',
                    source_task_path=request['task'],received_at=21,completed_at=60,
                    status='selftest_passed',trials=[dict(cleanup_confirmed=True),dict(cleanup_confirmed=True)])
        marker=root/'selftest/current-request.json';marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps(request))
        receipt=sessions/'author-selftest'/job/'status.json';receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(status))
        return root,sessions,ledger,marker,receipt,request,status

    def test_completed_acs_resumes_from_observation_without_rewriting_history(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(Path(raw))
            old=json.loads(json.dumps(ledger))
            lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,100)
            row=ledger['ideas']['idea-a']
            self.assertEqual(row['status'],'active');self.assertEqual(row['active_seconds'],10)
            self.assertEqual(row['events'][:-1],old['ideas']['idea-a']['events'])
            self.assertEqual(row['events'][-1]['at'],100)
            lifecycle.reconcile_session(ledger,True,120)
            self.assertEqual(row['active_seconds'],30)
            lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,130)
            self.assertEqual(len(row['events']),3)
            self.assertEqual(json.loads(receipt.read_text()),status)

    def test_newer_same_idea_job_must_also_be_terminal_before_resuming_old_wait(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(Path(raw))
            newest=dict(request,job_id='c'*32,commit='d'*40,received_at=65)
            marker.write_text(json.dumps(newest))
            new_receipt=sessions/'author-selftest'/newest['job_id']/'status.json';new_receipt.parent.mkdir()
            current=dict(status,job_id=newest['job_id'],source_commit=newest['commit'],received_at=65,
                         completed_at=90,status='running_nop')
            new_receipt.write_text(json.dumps(current))
            lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,100)
            self.assertEqual(ledger['ideas']['idea-a']['status'],'acs_wait')
            current['status']='selftest_passed';new_receipt.write_text(json.dumps(current))
            lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,110)
            self.assertEqual(ledger['ideas']['idea-a']['status'],'active')
            self.assertEqual(ledger['ideas']['idea-a']['active_seconds'],10)

    def test_watchdog_persists_completed_acs_resume_and_then_counts_active_time(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw)
            root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(base)
            directory=root/'home/role/.state/author-iteration';directory.mkdir(parents=True)
            path=directory/'ledger.json';path.write_text(json.dumps(ledger))
            (sessions/'author-01/run-current').mkdir(parents=True)
            config=dict(controller_uid=os.getuid(),roles={'author-01':dict(
                root=str(root),uid=os.getuid(),gid=os.getgid())})
            observations=base/'observations'
            with patch.object(lifecycle,'runner_state',return_value=True):
                with patch.object(lifecycle.time,'time',return_value=100):
                    self.assertEqual(lifecycle.scan_clocks(config,sessions,observations),[])
                with patch.object(lifecycle.time,'time',return_value=120):
                    self.assertEqual(lifecycle.scan_clocks(config,sessions,observations),[])
            row=json.loads(path.read_text())['ideas']['idea-a']
            self.assertEqual(row['status'],'active');self.assertEqual(row['active_seconds'],30)
            observed=json.loads((observations/'author-01.json').read_text())
            self.assertEqual(observed['ideas']['idea-a']['events'][-1],dict(action='resume',at=100))

    def test_unknown_mismatched_or_unfinished_execution_never_resumes_clock(self):
        for changes in (dict(status='running_nop'),dict(idea_id='other'),dict(role='author-02'),
                        dict(source_commit='c'*40),dict(source_task_path='tasks/other'),
                        dict(job_id='c'*32),dict(completed_at=None),dict(completed_at=101),
                        dict(received_at=22),dict(trials=[dict(cleanup_confirmed=False)]),
                        dict(trials=[]),dict(status='failed',trials=[],cloud_execution_attempted=True),
                        dict(source_repository=None),dict(schema_version=1)):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(Path(raw))
                receipt.write_text(json.dumps(dict(status,**changes)))
                before=json.dumps(ledger,sort_keys=True)
                lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,100)
                self.assertEqual(json.dumps(ledger,sort_keys=True),before)

    def test_no_live_runner_or_unresolved_review_platform_hold_keeps_pause(self):
        for state in ('reviewing','waiting_platform','no_runner'):
            with self.subTest(state=state),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(Path(raw))
                hold=dict(request,key='version:task',generation=1,state=state,author_must_pause=True,
                          feedback_source='selftest_handoff')
                for name in ('review-feedback.json','review-current-assignment.json'):
                    (root/name).write_text(json.dumps(hold))
                lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,state!='no_runner',100)
                self.assertEqual(ledger['ideas']['idea-a']['status'],'acs_wait')
                self.assertEqual(ledger['ideas']['idea-a']['active_seconds'],10)

    def test_preflight_rejection_uses_captured_request_only_with_explicit_no_cloud_evidence(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(Path(raw))
            failed={k:v for k,v in status.items() if not k.startswith('source_')}
            failed.update(status='failed',trials=[],cloud_execution_started=False,cloud_execution_attempted=False)
            receipt.write_text(json.dumps(failed))
            original={k:status[k] for k in ('job_id','idea_id','source_repository','source_task_path','source_commit')}
            (receipt.parent/'request.json').write_text(json.dumps(original))
            for bad in (dict(failed,cloud_execution_started=None),dict(failed,cloud_execution_attempted=True)):
                receipt.write_text(json.dumps(bad))
                lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,100)
                self.assertEqual(ledger['ideas']['idea-a']['status'],'acs_wait')
            receipt.write_text(json.dumps(failed))
            lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,100)
            self.assertEqual(ledger['ideas']['idea-a']['status'],'active')
            self.assertEqual(ledger['ideas']['idea-a']['active_seconds'],10)
            self.assertNotIn('source_commit',json.loads(receipt.read_text()))

    def test_new_idea_marker_and_explicit_platform_wait_are_not_rewritten(self):
        for changed in ('idea','platform'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(Path(raw))
                if changed=='idea':marker.write_text(json.dumps(dict(request,idea_id='new-idea')))
                else:ledger['ideas']['idea-a']['status']='platform_wait'
                before=json.dumps(ledger,sort_keys=True)
                lifecycle.resume_completed_acs_wait(ledger,root,'author-01',sessions,True,100)
                self.assertEqual(json.dumps(ledger,sort_keys=True),before)

    def test_exact_review_hold_tracks_new_request_and_rollout_identity(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root,sessions,ledger,marker,receipt,request,status=self.completed_acs_fixture(Path(raw))
            hold=dict(request,key='version:task',generation=2,state='waiting_platform',
                      stage='five_rollouts',author_must_pause=True,
                      evaluation_id='evaluation',snapshot_sha256='e'*64)
            for name in ('review-feedback.json','review-current-assignment.json'):
                (root/name).write_text(json.dumps(hold))
            self.assertTrue(lifecycle.review_waiting(root))
            for field,value in [('job_id','c'*32),('idea_id','other'),('commit','c'*40),
                                ('task','tasks/other'),('received_at',22)]:
                with self.subTest(field=field):
                    marker.write_text(json.dumps(dict(request,**{field:value})))
                    self.assertFalse(lifecycle.review_waiting(root))
            marker.write_text(json.dumps(request))
            (root/'review-current-assignment.json').write_text(json.dumps(dict(hold,snapshot_sha256='f'*64)))
            self.assertFalse(lifecycle.review_waiting(root))

    def test_watchdog_reconciliation_publishes_operator_observation(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);root=base/'role';directory=root/'home/role/.state/author-iteration'
            directory.mkdir(parents=True);ledger={};transition(ledger,'start','idea-a',10)
            (directory/'ledger.json').write_text(json.dumps(ledger))
            config=dict(controller_uid=os.getuid(),roles={'author-01':dict(root=str(root),uid=os.getuid(),gid=os.getgid())})
            observations=base/'operator-observations'
            with patch.object(lifecycle,'runner_state',return_value=False),patch.object(lifecycle.time,'time',return_value=20):
                self.assertEqual(lifecycle.scan_clocks(config,state_root=base/'sessions',observation_root=observations),[])
            proof=json.loads((observations/'author-01.json').read_text())
            self.assertFalse(proof['runner_live'])
            self.assertEqual(proof['ideas']['idea-a']['status'],'session_paused')
            self.assertEqual(proof['ideas']['idea-a']['events'][-1]['action'],'session-pause')

    def test_external_workspace_uses_current_deployment_sessions(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);root=base/'donor/.config/gewu-multiverse/agents/author-01'
            directory=root/'home/role/.state/author-iteration';directory.mkdir(parents=True)
            path=directory/'ledger.json';state={};transition(state,'start','idea-a',10)
            path.write_text(json.dumps(state))
            sessions=base/'runtime/.state/gewu-multiverse'
            current=sessions/'author-01/run-current';current.mkdir(parents=True)
            old=base/'donor/.state/gewu-multiverse/author-01/run-old';old.mkdir(parents=True)
            config=dict(controller_uid=os.getuid(),roles={'author-01':dict(root=str(root),uid=os.getuid(),gid=os.getgid())})
            with patch.object(lifecycle,'runner_state',side_effect=lambda run,uid:run==current) as observed,patch.object(lifecycle.time,'time',return_value=120):
                self.assertEqual(lifecycle.scan_clocks(config,state_root=sessions),[])
            self.assertEqual(json.loads(path.read_text())['ideas']['idea-a']['status'],'active')
            self.assertEqual(observed.call_args.args[0],current)

    def test_busy_clock_does_not_block_global_watchdog(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);directory=root/'home/role/.state/author-iteration';directory.mkdir(parents=True)
            path=directory/'ledger.json';path.write_text('{}')
            config=dict(controller_uid=os.getuid(),roles={'author-01':dict(root=str(root),uid=os.getuid(),gid=os.getgid())})
            with (directory/'clock.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(lifecycle.scan_clocks(config),[])
            self.assertEqual(path.read_text(),'{}')

    def test_one_bad_ledger_does_not_disable_other_roles(self):
        config=dict(controller_uid=1,roles={'author-01':{},'author-02':{}})
        with patch.object(lifecycle,'_scan_clocks',side_effect=[ValueError('private role content'),None]) as scan,patch('builtins.print') as output:
            errors=lifecycle.scan_clocks(config)
        self.assertEqual(scan.call_count,2)
        self.assertEqual(errors,[dict(role='author-01',error_type='ValueError')])
        self.assertNotIn('private role content',str(output.call_args))

    def test_ledger_pause_resume_persists_and_keeps_permissions(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            universe=Path(raw);root=universe/'.config/gewu-multiverse/agents/author-01'
            directory=root/'home/role/.state/author-iteration';directory.mkdir(parents=True)
            run=universe/'.state/gewu-multiverse/author-01/run-test';run.mkdir(parents=True)
            path=directory/'ledger.json';state={};transition(state,'start','idea-a',10)
            path.write_text(json.dumps(state))
            config=dict(controller_uid=os.getuid(),roles={'author-01':dict(root=str(root),uid=os.getuid(),gid=os.getgid())})
            with patch.object(lifecycle,'runner_state',return_value=False),patch.object(lifecycle,'terminal_time',return_value=100),patch.object(lifecycle.time,'time',return_value=120):
                lifecycle.scan_clocks(config)
            paused=json.loads(path.read_text())['ideas']['idea-a']
            self.assertEqual(paused['active_seconds'],90)
            self.assertEqual(paused['status'],'session_paused')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with patch.object(lifecycle,'runner_state',return_value=True),patch.object(lifecycle.time,'time',return_value=10000):
                lifecycle.scan_clocks(config)
            resumed=json.loads(path.read_text())['ideas']['idea-a']
            self.assertEqual(resumed['active_seconds'],90)
            self.assertEqual(resumed['status'],'active')
            self.assertEqual(resumed['last_at'],10000)

    def test_runner_identity_requires_exact_run_and_uid(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            run=Path(raw);(run/'session.pid').write_text(str(os.getpid()))
            self.assertFalse(lifecycle.runner_state(run,os.getuid()))

    def test_terminal_record_can_be_read_after_large_history(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            run=Path(raw)
            (run/'transcript.jsonl').write_text('x'*20000+'\n'+json.dumps(dict(event='run_finished',ts='2026-10-31T00:00:00Z'))+'\n')
            self.assertEqual(lifecycle.terminal_time(run),1793404800)
