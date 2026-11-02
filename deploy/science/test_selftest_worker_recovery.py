"""Exercise recovery through the worker and its real role-visible receipt."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from evaluation_queue import W
import author_selftest_worker as worker
from queue_parallel import ActiveJobs


class WorkerRecoveryTests(unittest.TestCase):
    def test_interrupted_upload_publishes_once_without_restarting_cloud_work(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'state';role=root/'role'
            job='a'*32;work=state/job;work.mkdir(parents=True)
            (role/'selftest/results').mkdir(parents=True)
            (work/'upload.tar').write_bytes(b'preserved evidence')
            request=role/'selftest/inbox'/(job+'.json')
            receipt=role/'selftest/results'/(job+'.json')
            entry={'root':str(role),'uid':os.getuid(),'gid':os.getgid()}
            with patch.object(worker,'STATE',state),patch.object(worker,'owned_run') as run:
                worker.process('author-01',entry,request)
                first=receipt.read_bytes()
                worker.process('author-01',entry,request)
                run.assert_not_called()
            status=json.loads(first)
            self.assertEqual(status['status'],'interrupted')
            self.assertIs(status['quality_acceptance'],False)
            self.assertEqual(receipt.read_bytes(),first)
            self.assertEqual(receipt.stat().st_mode & 0o777,0o640)
            self.assertEqual((work/'upload.tar').read_bytes(),b'preserved evidence')

    def test_existing_unfinished_work_is_scheduled_but_terminal_receipts_are_not(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'state';role=root/'role'
            inbox=role/'selftest/inbox';results=role/'selftest/results'
            inbox.mkdir(parents=True);results.mkdir()
            unfinished='a'*32;finished='b'*32
            for job in (unfinished,finished):(inbox/(job+'.json')).write_text('{}')
            (state/unfinished).mkdir(parents=True)
            (results/(finished+'.json')).write_text('{"status":"selftest_passed"}')
            candidates=worker.pending_requests({'author-01':{'root':str(role)}},state)
            self.assertEqual([(r,q.stem) for _,r,q in candidates],[('author-01',unfinished)])

    def test_ten_process_slots_overlap_and_same_author_is_exclusive(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);pool=ActiveJobs(10)
            script=('import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); '
                    '(p/(sys.argv[2]+".ready")).touch(); '
                    'exec("while not (p/\\"release\\").exists():\\n time.sleep(.02)")')
            candidates=[(1,'author-01',root/('a'*32+'.json')),
                        (2,'author-01',root/('b'*32+'.json')),
                        (3,'author-02',root/('c'*32+'.json')),
                        (4,'author-03',root/('d'*32+'.json')),
                        (5,'author-04',root/('e'*32+'.json')),
                        (6,'author-05',root/('f'*32+'.json')),
                        (7,'author-06',root/('1'*32+'.json')),
                        (8,'author-07',root/('2'*32+'.json')),
                        (9,'author-08',root/('3'*32+'.json')),
                        (10,'author-09',root/('4'*32+'.json')),
                        (11,'author-10',root/('5'*32+'.json')),
                        (12,'author-11',root/('6'*32+'.json'))]
            try:
                def command(role,q):return [sys.executable,'-c',script,str(root),q.stem]
                worker.schedule(pool,candidates,{},command)
                deadline=time.monotonic()+15
                while len(list(root.glob('*.ready')))<10 and time.monotonic()<deadline:time.sleep(.05)
                self.assertEqual({role for role,_ in pool.children},{f'author-{i:02d}' for i in range(1,11)})
                self.assertEqual(len(list(root.glob('*.ready'))),10)
                worker.schedule(pool,candidates,{},command)
                self.assertEqual(len(pool.children),10)
                (root/'release').touch()
                while pool.children and time.monotonic()<deadline:pool.reap();time.sleep(.05)
                self.assertFalse(pool.children)
            finally:pool.stop()

    def test_worker_crash_reports_interruption_without_claiming_cleanup(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'state';role=root/'role';job='e'*32
            (role/'selftest/results').mkdir(parents=True)
            q=role/'selftest/inbox'/(job+'.json')
            with patch.object(worker,'STATE',state):
                worker.publish_worker_interruption('author-01',{'root':str(role),'gid':os.getgid()},q,'RuntimeError')
            receipt=json.loads((role/'selftest/results'/(job+'.json')).read_text())
            self.assertEqual(receipt['status'],'interrupted')
            self.assertNotIn('cleanup_confirmed',receipt)

    def test_handoff_crash_preserves_passed_result_and_reports_platform_wait(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'state';role=root/'role';job='f'*32
            results=role/'selftest/results';results.mkdir(parents=True)
            (results/(job+'.json')).write_text('{"status":"selftest_passed"}')
            q=role/'selftest/inbox'/(job+'.json')
            with patch.object(worker,'STATE',state):
                worker.publish_worker_interruption('author-01',{'root':str(role),'gid':os.getgid()},q,'RuntimeError')
            receipt=json.loads((results/(job+'.json')).read_text())
            self.assertEqual(receipt['status'],'selftest_passed')
            self.assertEqual(receipt['review_handoff']['state'],'waiting_platform')

    def test_actual_child_recovers_unfinished_work_without_cloud_execution(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'state';role=root/'role';job='c'*32
            inbox=role/'selftest/inbox';results=role/'selftest/results'
            inbox.mkdir(parents=True);results.mkdir();work=state/job;work.mkdir(parents=True)
            (work/'status.json').write_text('{"status":"validating"}')
            request=inbox/(job+'.json');request.write_text('{}')
            config=root/'broker.json';config.write_text(json.dumps({'controller_uid':os.getuid(),'roles':{'author-01':{'root':str(role),'uid':os.getuid(),'gid':os.getgid()}}}))
            cmd=[sys.executable,str(Path(worker.__file__).resolve()),'--broker-config',str(config),
                 '--state',str(state),'--single-request',str(request),'--role','author-01']
            completed=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
            self.assertEqual(completed.returncode,0,completed.stderr)
            receipt=json.loads((results/(job+'.json')).read_text())
            self.assertEqual(receipt['status'],'interrupted')
            self.assertNotIn('cleanup_confirmed',receipt)
            self.assertFalse(worker.pending_requests({'author-01':{'root':str(role)}},state))


if __name__=='__main__':unittest.main()
