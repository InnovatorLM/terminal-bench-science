"""Current deployment inbox ownership regression; no cloud or model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from evaluation_queue import W, read_request

class CurrentQueueTests(unittest.TestCase):
    def test_current_roles_are_distinct_and_private(self):
        config=json.loads((W/'.config/science-evaluation/queue.json').read_text())
        queues=config['role_queues']
        self.assertEqual({q['role'] for q in queues},{'evaluator-01','evaluator-02','evaluator-03'})
        self.assertEqual(len({q['evaluator_uid'] for q in queues}),3)
        self.assertEqual(config['max_jobs_per_day'],0)
        # Inspect configured identities, but never inject a test request into
        # an inbox a production worker could claim before the test removes it.
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);root.chmod(0o755)
            for queue in queues:
                inbox=root/queue['role'];inbox.mkdir(mode=0o700)
                os.chown(inbox,queue['evaluator_uid'],queue['evaluator_uid'])
                path=inbox/(uuid.uuid4().hex+'.json')
                path.write_text(json.dumps(dict(job_id=path.stem,evaluation_id='fixture',snapshot_sha256='0'*64)))
                os.chown(path,queue['evaluator_uid'],queue['evaluator_uid']);path.chmod(0o600)
                self.assertEqual(read_request(path,queue['evaluator_uid'])['evaluation_id'],'fixture')
                for other in queues:
                    if other==queue:continue
                    with self.assertRaises(ValueError):read_request(path,other['evaluator_uid'])
                    result=subprocess.run(['setpriv','--reuid='+str(other['evaluator_uid']),'--regid='+str(other['evaluator_uid']),'--clear-groups','/bin/cat',str(path)],capture_output=True)
                    self.assertNotEqual(result.returncode,0)
    def test_worker_initializes_without_production_roles(self):
        # Startup must never consume real evaluator requests, even in mock
        # mode. The capacity policy now needs an explicit author roster too.
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            for name in ('state','inbox','results'):(root/name).mkdir(mode=0o700)
            os.chown(root/'inbox',21011,21011)
            (root/'registry.json').write_text('{"fixture":{}}')
            (root/'broker.json').write_text('{"roles":{"author-01":{}}}')
            config={'state':str(root/'state'),'registry':str(root/'registry.json'),
                    'broker_config':str(root/'broker.json'),
                    'role_queues':[{'inbox':str(root/'inbox'),'results':str(root/'results'),'evaluator_uid':21011}]}
            path=root/'config.json';path.write_text(json.dumps(config))
            result=subprocess.run([sys.executable,str(Path(__file__).with_name('evaluation_queue.py')),
                '--config',str(path),'--once','--mock'],capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode())
