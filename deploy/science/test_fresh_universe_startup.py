import json,os,pathlib,subprocess,sys,tempfile,unittest
from evaluation_queue import W

class FreshUniverseStartupTests(unittest.TestCase):
    def ready_check(self):
        text=pathlib.Path(__file__).with_name('start-universe').read_text()
        return text.split("<<'PY_QUEUE_CHECK'\n",1)[1].split('\nPY_QUEUE_CHECK',1)[0]

    def test_queue_ready_reopens_transient_storage_errors(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import errno
        class Ready:
            calls=0
            def lstat(self):return SimpleNamespace(st_mode=0o100600,st_uid=0)
            def read_text(self):
                self.calls+=1
                if self.calls<=3:raise OSError([errno.ESTALE,errno.EIO,errno.EINTR][self.calls-1],'synthetic transient failure')
                return json.dumps({'pid':12345})
        ready=Ready();code=self.ready_check()
        with patch('pathlib.Path',return_value=ready),patch('os.kill'),patch('time.sleep'),patch.object(sys,'argv',['check','12345','ready.json']):
            exec(code, {})
        self.assertEqual(ready.calls,4)

    def test_queue_ready_does_not_retry_permission_errors(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import errno
        class Ready:
            def lstat(self):return SimpleNamespace(st_mode=0o100600,st_uid=0)
            def read_text(self):raise PermissionError(errno.EACCES,'synthetic refused access')
        code=self.ready_check()
        with patch('pathlib.Path',return_value=Ready()),patch('os.kill'),patch.object(sys,'argv',['check','12345','ready.json']):
            with self.assertRaises(PermissionError):exec(code, {})

    def test_empty_catalog_starts_and_rejects_unregistered_task(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=pathlib.Path(raw)
            for name in ('state','inbox','results'):(root/name).mkdir(mode=0o700)
            os.chown(root/'inbox',24001,24001)
            for name,value in [('registry',{}),('broker',{'roles':{f'author-{i:02d}':{} for i in range(1,31)}})]:
                p=root/(name+'.json');p.write_text(json.dumps(value));p.chmod(0o600)
            cfg={'state':str(root/'state'),'registry':str(root/'registry.json'),'broker_config':str(root/'broker.json'),'role_queues':[{'inbox':str(root/'inbox'),'results':str(root/'results'),'evaluator_uid':24001}]}
            p=root/'config.json';p.write_text(json.dumps(cfg));p.chmod(0o600)
            request=root/'inbox'/('a'*32+'.json');request.write_text(json.dumps({'job_id':'a'*32,'evaluation_id':'not-registered','snapshot_sha256':'b'*64}));os.chown(request,24001,24001)
            ready=root/'state/ready.json'
            r=subprocess.run([sys.executable,str(pathlib.Path(__file__).with_name('evaluation_queue.py')),'--config',str(p),'--ready-file',str(ready),'--once','--mock'],capture_output=True,timeout=45)
            self.assertEqual(r.returncode,0,r.stderr.decode())
            self.assertEqual(json.loads(ready.read_text())['max_parallel_jobs'],150)
            self.assertEqual(json.loads((root/'results'/request.name).read_text())['status'],'rejected')

    def test_dispatcher_supervisor_terminates_owned_child(self):
        text=pathlib.Path(__file__).with_name('start-universe').read_text()
        start=text.index("    dispatcher_child=''\n")
        end=text.index(') &\nreview_dispatch_pid',start)
        block=text[start:end]
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=pathlib.Path(raw);(root/'gewu-multiverse/deploy/science').mkdir(parents=True);(root/'review-dispatcher').mkdir()
            stub=root/'gewu-multiverse/deploy/science/review_dispatcher.py';stub.write_text('import os,pathlib,time\npathlib.Path(os.environ["CHILD_PID"]).write_text(str(os.getpid()))\nwhile True: time.sleep(.1)\n')
            env=dict(os.environ,PY=sys.executable,W=str(root),SERVICE_STATE=str(root),GEWU_ROLE_BROKER_CONFIG=str(root/'none'),CHILD_PID=str(root/'child.pid'))
            process=subprocess.Popen(['bash','-c',block],env=env)
            try:
                import time
                deadline=time.monotonic()+10
                while not (root/'child.pid').exists() and time.monotonic()<deadline:time.sleep(.05)
                pid=int((root/'child.pid').read_text());process.terminate();process.wait(timeout=10)
                self.assertFalse(pathlib.Path('/proc',str(pid)).exists())
            finally:
                if process.poll() is None:process.kill();process.wait()

if __name__=='__main__':unittest.main()
