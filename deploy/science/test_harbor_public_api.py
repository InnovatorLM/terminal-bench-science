import base64
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from evaluation_queue import W
from harbor_public_api import extract, serve


def task_zip():
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        for name, value in {'instruction.md':'Copy input to output.', 'task.toml':'version = "1.0"',
                            'environment/Dockerfile':'FROM python:3.12-slim',
                            'tests/test.sh':'#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n'}.items():
            z.writestr('demo/' + name, value)
    return data.getvalue()


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=W/'.tmp', prefix='harbor-public-test-')
        self.root = Path(self.temp.name)
        (self.root/'secret').write_text('unit-test-only-secret')
        self.settings = dict(state=str(self.root/'state'),secret_file=str(self.root/'secret'),
            host='127.0.0.1',port=0,default_attempts=5,max_pending=10,max_pending_per_ip=2,
            max_jobs_per_day=20,max_upload_bytes=1024*1024)
        self.server,self.service=serve(self.settings,self.root/'settings.json',run_scheduler=False)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def submit(self,key='test-idempotency-1',raw=None):
        req=Request(self.base+'/tasks',data=task_zip() if raw is None else raw,
            headers={'Content-Type':'application/octet-stream','Idempotency-Key':key})
        with urlopen(req) as r:
            self.assertEqual(r.status,202);return json.load(r)
    def test_private_receipt_idempotency_and_optional_solution(self):
        raw=task_zip();first=self.submit(raw=raw);second=self.submit(raw=raw)
        self.assertEqual(first['run_id'],second['run_id'])
        self.assertEqual(len(list(self.service.runs.iterdir())),1)
        with self.assertRaises(HTTPError) as e:urlopen(self.base+first['status_url'])
        self.assertEqual(e.exception.code,403)
        req=Request(self.base+first['status_url'],headers={'Authorization':'Bearer '+first['token']})
        with urlopen(req) as r:d=json.load(r)
        self.assertEqual(d['status'],'queued');self.assertTrue(d['reference_placeholder'])
        solution=next((self.service.runs/first['run_id']/'source').glob('*/solution/solve.sh'))
        self.assertEqual(solution.read_text().splitlines()[-1],'exit 0')
    def test_cross_job_token_and_limits(self):
        one=self.submit('request-one');two=self.submit('request-two')
        req=Request(self.base+two['status_url'],headers={'Authorization':'Bearer '+one['token']})
        with self.assertRaises(HTTPError) as e:urlopen(req)
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(HTTPError) as e:self.submit('request-three')
        self.assertEqual(e.exception.code,429)
    def test_archive_traversal_and_links(self):
        for kind in ('traversal','symlink'):
            data=io.BytesIO()
            with tarfile.open(fileobj=data,mode='w') as tar:
                m=tarfile.TarInfo('../escape' if kind=='traversal' else 'link')
                if kind=='symlink':m.type=tarfile.SYMTYPE;m.linkname='/etc/passwd'
                tar.addfile(m)
            with self.assertRaises(ValueError):extract(data.getvalue(),self.root/kind)
        self.assertFalse((self.root/'escape').exists())
    def test_client_cannot_choose_commands_or_credentials(self):
        req=Request(self.base+'/tasks',data=json.dumps({'data':base64.b64encode(task_zip()).decode(),
            'command':'touch /tmp/escape'}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as e:urlopen(req)
        self.assertEqual(e.exception.code,400)
        self.assertEqual(list(self.service.runs.iterdir()),[])
    def test_public_task_rejects_host_env_interpolation(self):
        from harbor_public_policy import validate_task
        root=self.root/'task';root.mkdir()
        (root/'task.toml').write_text('version="1.0"'+chr(10)+'[environment.env]'+chr(10)+'VALUE="'+('$'+'{SECRET_FROM_HOST}')+'"')
        with self.assertRaises(ValueError):validate_task(root)
        (root/'task.toml').write_text('version="1.0"'+chr(10)+'[environment.env]'+chr(10)+'VALUE="literal"')
        self.assertTrue(validate_task(root))
    def test_model_lease_revocation_and_budget(self):
        import time
        one=self.submit();root=self.service.runs/one['run_id'];p=root/'model-lease.json'
        lease=dict(token='test-job-token',expires_at=time.time()+60,max_requests=1,revoked=False)
        p.write_text(json.dumps(lease))
        self.assertEqual(self.service.model_lease('test-job-token')['token'],'test-job-token')
        with self.assertRaises(PermissionError):self.service.model_lease('test-job-token')
        from harbor_public_api import Service
        restarted=Service(self.settings,self.root/'settings.json')
        with self.assertRaises(PermissionError):restarted.model_lease('test-job-token')
        lease['revoked']=True;p.write_text(json.dumps(lease))
        with self.assertRaises(PermissionError):self.service.model_lease('test-job-token')

    def test_failed_task_diagnostic_archive_requires_its_task_token(self):
        from harbor_public_artifacts import publish_artifacts
        from evaluation_queue import atomic_json
        one=self.submit('failed-task-one');two=self.submit('failed-task-two')
        root=self.service.runs/one['run_id'];(root/'build.log').write_text('ERROR: build failed\n')
        result=self.service.status(one['run_id']);result['status']='failed'
        result.update(publish_artifacts(root,result));atomic_json(root/'public.json',result)
        for token in ['',two['token']]:
            req=Request(self.base+one['artifacts_url'],headers={'Authorization':'Bearer '+token})
            with self.assertRaises(HTTPError) as e:urlopen(req)
            self.assertEqual(e.exception.code,403)
        req=Request(self.base+one['artifacts_url'],headers={'Authorization':'Bearer '+one['token']})
        with urlopen(req) as response:packet=response.read();self.assertEqual(response.status,200)
        with zipfile.ZipFile(io.BytesIO(packet)) as archive:
            self.assertEqual(archive.read('diagnostics/build.log'),b'ERROR: build failed\n')

if __name__=='__main__':unittest.main()
