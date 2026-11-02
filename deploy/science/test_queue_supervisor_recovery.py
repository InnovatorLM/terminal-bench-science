"""Supervisor replacement/capacity checks with real local processes, no ACS/LLM."""
import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from evaluation_queue import W, parallel_limit, dispatch_paused


class QueueRecoveryTests(unittest.TestCase):
    def test_capacity_counts_authors_including_waiting_authors_not_reviewers(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            broker = Path(raw)/'broker.json'
            roles = {f'author-{i:02}': {'paused': True} for i in range(1, 11)}
            roles.update({f'evaluator-{i:02}': {} for i in range(1, 4)})
            broker.write_text(json.dumps({'roles': roles})); broker.chmod(0o600)
            config = {'broker_config': str(broker)}
            self.assertEqual(parallel_limit(config), 50)
            self.assertEqual(parallel_limit({**config, 'max_parallel_jobs': 7}), 7)
            for value in [True, 0, 51, '50', None]:
                with self.assertRaises(ValueError):
                    parallel_limit({**config, 'max_parallel_jobs': value})
            roles['author-11'] = {}
            broker.write_text(json.dumps({'roles': roles}))
            self.assertEqual(parallel_limit(config), 55)
            for payload in [[], {}, {'roles': []}, {'roles': {'author-01': None}},
                            {'roles': {'evaluator-01': {}}}]:
                broker.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    parallel_limit(config)
        for config in [[], {}, {'broker_config': None}]:
            with self.assertRaises(ValueError):
                parallel_limit(config)
        self.assertFalse(dispatch_paused({}))
        self.assertTrue(dispatch_paused({'dispatch_paused': True}))
        with self.assertRaises(ValueError):
            dispatch_paused({'dispatch_paused': 'true'})

    def test_adopts_surviving_worker_and_resizes_without_replay_or_cancellation(self):
        script = Path(__file__).with_name('evaluation_queue.py').resolve()
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root = Path(raw)
            for name in ['state', 'inbox', 'results']:
                (root/name).mkdir(mode=0o700)
            os.chown(root/'inbox', 21011, 21011)
            job_id = 'a'*32
            request = root/'inbox'/(job_id+'.json')
            identity = dict(job_id=job_id, evaluation_id='fixture', snapshot_sha256='b'*64)
            request.write_text(json.dumps(identity))
            os.chown(request, 21011, 21011)
            job = root/'state'/job_id
            job.mkdir()
            # Spawn through a disposable parent to verify the new supervisor
            # works for non-child workers after a legacy supervisor exits.
            worker_code = "import time; time.sleep(90)"
            argv = [sys.executable, '-c', worker_code, str(script),
                    '--single-request', str(request), '--queue-index', '0']
            parent = subprocess.run([sys.executable, '-c',
                'import subprocess,sys,json; p=subprocess.Popen(json.loads(sys.argv[1]),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)',
                json.dumps(argv)], capture_output=True, text=True, check=True)
            worker_pid = int(parent.stdout.strip())
            (job/'claim.json').write_text(json.dumps(dict(identity, queue_worker_pid=worker_pid)))
            (root/'registry.json').write_text('{"fixture":{}}')
            broker = root/'broker.json'
            def roster(count):
                pending = root/'broker-pending.json'
                pending.write_text(json.dumps({'roles': {
                    f'author-{i:02}': {} for i in range(1, count+1)}}))
                pending.replace(broker)
            roster(10)
            config = dict(state=str(root/'state'), registry=str(root/'registry.json'),
                          broker_config=str(broker), dispatch_paused=True, role_queues=[dict(evaluator_uid=21011,
                          inbox=str(root/'inbox'), results=str(root/'results'))])
            config_path = root/'config.json'
            def update():
                pending = root/'pending.json'
                pending.write_text(json.dumps(config)); pending.replace(config_path)
            update()
            ready = root/'state/ready.json'
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(W/'.tmp'))
            queue = subprocess.Popen([sys.executable, str(script), '--config', str(config_path),
                '--ready-file', str(ready), '--owner-pid', str(os.getpid()), '--mock'],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            def wait_capacity(expected):
                deadline = time.monotonic()+15
                while time.monotonic() < deadline:
                    if queue.poll() is not None:
                        self.fail(queue.stderr.read().decode())
                    if ready.exists():
                        try:
                            data = json.loads(ready.read_text())
                        except OSError as error:
                            # FUSE can invalidate an open handle at atomic
                            # replacement. Reopen, bounded by this deadline.
                            if error.errno not in {errno.ESTALE, errno.EIO, errno.ENOENT}:
                                raise
                            time.sleep(.05)
                            continue
                        if data['max_parallel_jobs'] == expected:
                            return data
                    time.sleep(.05)
                self.fail('capacity update timed out')
            try:
                data = wait_capacity(50)
                self.assertEqual(data['adopted_workers'], [dict(job_id=job_id, pid=worker_pid)])
                self.assertTrue(data['dispatch_paused'])
                roster(11); wait_capacity(55)
                # An invalid hot update must not terminate the supervisor or
                # cancel its adopted worker. Then restore the valid config.
                pending = root/'pending.json'
                pending.write_text('[]'); pending.replace(config_path)
                time.sleep(1.3)
                self.assertIsNone(queue.poll())
                self.assertEqual(json.loads(ready.read_text())['max_parallel_jobs'], 55)
                config['max_parallel_jobs'] = 1
                config['dispatch_paused'] = False
                update(); wait_capacity(1)
                self.assertFalse(json.loads(ready.read_text())['dispatch_paused'])
                os.kill(worker_pid, 0)
                self.assertFalse((root/'results'/request.name).exists(), 'live job was replayed')
                self.assertFalse((job/'result.json').exists())
                queue.terminate(); queue.wait(timeout=10)
                self.assertEqual(queue.returncode, 0)
                # The orphan may briefly remain a zombie until PID 1 reaps it.
                proc = Path('/proc')/str(worker_pid)/'stat'
                self.assertTrue(not proc.exists() or proc.read_text().split(') ',1)[1][0] == 'Z')
            finally:
                if queue.poll() is None:
                    queue.terminate(); queue.wait(timeout=10)
                queue.stderr.close()
                try: os.kill(worker_pid, 15)
                except ProcessLookupError: pass


if __name__ == '__main__':
    unittest.main()
