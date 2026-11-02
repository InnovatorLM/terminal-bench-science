"""Local queue occupancy checks; no Kubernetes, Docker, or model calls."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from evaluation_queue import W
import author_selftest_worker as worker


class CapacityTests(unittest.TestCase):
    def hold(self,stack,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        handle=stack.enter_context(path.open('a'))
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_metrics_separate_local_phases_pending_and_unknown_cloud_occupancy(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw,contextlib.ExitStack() as stack:
            root=Path(raw);state=root/'state';state.mkdir();roles={}
            for index in range(1,4):
                role=f'author-{index:02}';path=root/role;roles[role]={'root':str(path)}
                (path/'selftest/results').mkdir(parents=True);(path/'selftest/inbox').mkdir()
            jobs=['a'*32,'b'*32,'c'*32]
            candidates=[]
            for index,job in enumerate(jobs,1):
                role=f'author-{index:02}';q=Path(roles[role]['root'])/'selftest/inbox'/(job+'.json')
                q.write_text('{}');candidates.append(((100+index)*10**9,role,q))
            for job,phase in zip(jobs[:2],['building','running_nop']):
                (state/job).mkdir();(state/job/'status.json').write_text(json.dumps(
                    {'status':phase,'updated_at':190,'phase_started_at':180}))
            for name in ('author-02',jobs[1]):self.hold(stack,state/'locks'/(name+'.lock'))
            self.hold(stack,root/'.state/science-image-builds/builder.lock')
            terminal=Path(roles['author-03']['root'])/'selftest/results'/('d'*32+'.json')
            terminal.write_text(json.dumps({'status':'failed','error_class':'task_build_context_invalid','error':'not-exported'}))
            pool=SimpleNamespace(limit=10,children={('author-01',jobs[0]):SimpleNamespace(pid=123,poll=lambda:None)})
            with patch.object(worker,'W',root):report=worker.queue_snapshot(roles,state,pool,candidates,now=200)
            self.assertEqual(report['capacity']['tracked_live_processes'],1)
            self.assertEqual(report['capacity']['external_or_unidentified_locked_roles'],1)
            self.assertIsNone(report['capacity']['free_slots'])
            self.assertEqual(report['queue']['observed_active_requests'],2)
            self.assertEqual(report['queue']['pending_requests'],1)
            self.assertEqual(report['queue']['oldest_pending_wait_seconds'],97)
            self.assertEqual(report['queue']['oldest_pending_job_id'],jobs[2])
            self.assertTrue(report['build']['shared_lock_held'])
            self.assertEqual(report['build']['observed_selftest_build_phase_jobs'],1)
            self.assertEqual(report['acs']['observed_selftest_execution_phase_jobs'],1)
            self.assertIsNone(report['acs']['actual_running_pods'])
            self.assertEqual(report['recent_terminal_results']['failure_classifications'],{'task_build_context_invalid':1})
            self.assertNotIn('not-exported',json.dumps(report))

    def test_unidentified_owner_does_not_become_a_fake_free_slot_or_pending_job(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw,contextlib.ExitStack() as stack:
            root=Path(raw);state=root/'state';state.mkdir();role=root/'role';job='e'*32
            self.hold(stack,state/'locks/author-01.lock')
            roles={'author-01':{'root':str(role)}}
            candidates=[(100*10**9,'author-01',role/'selftest/inbox'/(job+'.json'))]
            pool=SimpleNamespace(limit=10,children={})
            report=worker.queue_snapshot(roles,state,pool,candidates,now=200)
            self.assertEqual(report['queue']['ownership_unknown_requests'],1)
            self.assertEqual(report['queue']['pending_requests'],0)
            self.assertEqual(report['active_jobs'],[])
            self.assertIsNone(report['queue']['oldest_pending_wait_seconds'])
            self.assertEqual(report['observation_gaps']['unidentified_locked_roles'],['author-01'])

    def test_restarted_supervisor_counts_surviving_author_locks_before_scheduling(self):
        class Pool:
            limit=2
            def __init__(self):self.children={('author-01','a'*32):object()};self.started=[]
            def reap(self):pass
            def start(self,key,command):self.started.append(key);self.children[key]=object();return True
        pool=Pool();q=Path('c'*32+'.json');candidates=[(1,'author-03',q)]
        config={'roles':{'author-01':{},'author-02':{},'author-03':{}}}
        with patch.object(worker,'role_locked',side_effect=lambda role:role=='author-02'):
            worker.schedule(pool,candidates,config,lambda role,q:['offline'])
        self.assertEqual(pool.started,[])
        with patch.object(worker,'role_locked',return_value=False):
            worker.schedule(pool,candidates,config,lambda role,q:['offline'])
        self.assertEqual(pool.started,[('author-03','c'*32)])

    def test_terminal_metrics_are_bounded_and_missing_rewards_are_not_control_failures(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);results=root/'role/selftest/results';results.mkdir(parents=True)
            receipts=[{'status':'selftest_passed'},
                      {'status':'selftest_failed','trials':[{'diagnostics':[{'scored':False,'diagnostic':'verifier_reward_missing'}]}]},
                      {'status':'interrupted'}]
            for index,payload in enumerate(receipts):
                path=results/(str(index)*32+'.json');path.write_text(json.dumps(payload));os.utime(path,(index+1,index+1))
            report=worker.queue_snapshot({'author-01':{'root':str(root/'role')}},root/'state',
                SimpleNamespace(limit=15,children={}),[],now=100,terminal_sample_per_role=2)
            recent=report['recent_terminal_results']
            self.assertEqual(recent['sampled'],2)
            self.assertEqual(recent['failure_classifications'],{'worker_interrupted':1,'verifier_reward_missing':1})
            self.assertNotIn('selftest_passed',recent['status_counts'])
            self.assertEqual(worker.DEFAULT_WORKERS,10)
            self.assertEqual(worker.MAX_WORKERS,15)

    def test_failure_classification_requires_an_actual_scored_control(self):
        trial={'agent':'oracle','exit_code':0,'cleanup_confirmed':True,
               'diagnostics':[{'scored':True,'diagnostic':'ok'}],
               'results':[{'rewards':{'reward':0},'exception_type':None}]}
        self.assertEqual(worker.failure_classification({'status':'selftest_failed','trials':[trial]}),'oracle_control_failed')
        without_diagnosis=dict(trial,diagnostics=[])
        self.assertEqual(worker.failure_classification({'status':'selftest_failed','trials':[without_diagnosis]}),'control_score_unverified')
        with_exception=dict(trial,results=[{'rewards':{'reward':0},'exception_type':'ValueError'}])
        self.assertEqual(worker.failure_classification({'status':'selftest_failed','trials':[with_exception]}),'control_trial_exception')
        for value in (None,[],[None],[{'results':None}],[{'exit_code':0,'cleanup_confirmed':True,'results':[{'rewards':[]}]}]):
            result=worker.failure_classification({'status':'selftest_failed','trials':value})
            self.assertNotIn(result,('oracle_control_failed','nop_control_failed'))

    def test_empty_supervisor_publishes_and_stops_without_starting_jobs(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'state';config=root/'broker.json'
            config.write_text(json.dumps({'controller_uid':os.getuid(),'roles':{}}))
            command=[sys.executable,str(Path(worker.__file__).resolve()),'--broker-config',str(config),'--state',str(state)]
            env=os.environ.copy();env.pop('GEWU_SELFTEST_WORKERS',None);env['PYTHONDONTWRITEBYTECODE']='1'
            child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
            try:
                deadline=time.monotonic()+30;receipt=None
                while time.monotonic()<deadline and child.poll() is None:
                    receipt=worker._read_receipt(state/'queue-status.json')
                    if receipt:break
                    time.sleep(.05)
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt['capacity']['configured_workers'],10)
                self.assertEqual(receipt['capacity']['tracked_live_processes'],0)
                self.assertEqual(receipt['queue']['nonterminal_requests'],0)
                child.terminate();_,stderr=child.communicate(timeout=15)
                self.assertEqual(child.returncode,0,stderr)
                final=json.loads((state/'queue-status.json').read_text())
                self.assertTrue(final['supervisor_stopped'])
                self.assertEqual((state/'queue-status.json').stat().st_mode&0o777,0o600)
                self.assertFalse(list(state.glob('[a-f0-9]'*32)))
            finally:
                if child.poll() is None:child.kill();child.communicate(timeout=15)

    def test_cli_rejects_capacity_outside_supported_range_before_creating_state(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state=Path(raw)/'state'
            for count in ('0','16'):
                command=[sys.executable,str(Path(worker.__file__).resolve()),'--workers',count,'--state',str(state)]
                result=subprocess.run(command,capture_output=True,text=True,timeout=30)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('workers must be between 1 and 15',result.stderr)
                self.assertFalse(state.exists())


if __name__=='__main__':unittest.main()
