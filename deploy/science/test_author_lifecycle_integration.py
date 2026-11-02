"""Offline operator -> real Rust engine integration, never production roles.

Set GEWU_ENGINE_TEST_BINARY to the compiled Rust unit-test executable. This suite
uses a private universe, unused role UID, real processes and production lifecycle
code. Only the model executable and elapsed observation timestamps are fixtures.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import dead_end_watchdog
import role_reset


MOCK_RUNNER = r'''
import datetime, hashlib, json, os, sys, time
from pathlib import Path
assert sys.argv[1] == '--run-agent'
run = Path(sys.argv[2])
plan = json.loads((run/'plan.json').read_text())
instruction = Path(plan['instruction']).read_bytes()
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
records = [dict(event='run_started', ts=now, run_id=plan['run_id'],
                agent=plan['agent'], pid=os.getpid(), workspace=plan['workspace']),
           dict(event='instruction_read', ts=now, path=plan['instruction'],
                bytes=len(instruction), digest=hashlib.sha256(instruction).hexdigest())]
records += [dict(event='tool_result', ts=now, turn=1, index=i+1,
                 tool='shell', exit_code=1, signal=None, duration_ms=1,
                 output='ValueError: isolated repeated failure', segments=[])
            for i in range(9)]
with (run/'transcript.jsonl').open('a') as stream:
    for record in records:
        stream.write(json.dumps(record)+'\n')
    stream.flush()
(run/'fixture-instruction.txt').write_bytes(instruction)
(run/'fixture-ready').touch()
while True:
    time.sleep(1)
'''


@unittest.skipUnless(os.environ.get('GEWU_ENGINE_TEST_BINARY'),
                     'set GEWU_ENGINE_TEST_BINARY to run the real-controller integration')
@unittest.skipUnless(os.geteuid() == 0, 'operator integration requires root and a disposable UID')
class AuthorLifecycleIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix='gewu-lifecycle-fixture-', dir=role_reset.W/'.tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root/'.state/gewu-multiverse'
        self.role = self.root/'isolated-author'
        self.controller_role = self.root/'.config/gewu-multiverse/agents/author-01'
        (self.role/'workspace').mkdir(parents=True)
        (self.role/'instruction.md').write_text('Preserve the isolated author policy.\n')
        self.work = self.role/'workspace/retained-work.txt'
        self.work.write_text('preserve existing source and numerical evidence\n')
        self.ledger = self.role/'home/role/.state/author-iteration/ledger.json'
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text('{"idea":"existing","active_seconds":12345}\n')
        self.original_ledger = self.ledger.read_bytes()
        # This UID is unrelated to every production role and must have no process.
        used = set()
        for proc in Path('/proc').iterdir():
            if proc.name.isdigit():
                try:
                    used.add(proc.stat().st_uid)
                except FileNotFoundError:
                    pass
        self.uid = 900000 + os.getpid()
        while self.uid in used:
            self.uid += 1
        self.config = dict(controller_uid=os.getuid(), roles={
            'author-01': dict(uid=self.uid, gid=self.uid, root=str(self.role))})
        (self.state/'role-broker').mkdir(parents=True)
        (self.state/'role-broker/config.json').write_text(json.dumps(self.config))
        runner = self.root/'mock-model-runner.py'
        runner.write_text('#!'+sys.executable+'\n'+MOCK_RUNNER)
        runner.chmod(0o700)
        env = dict(os.environ, GEWU_LIFECYCLE_FIXTURE_ROOT=str(self.root),
                   PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(role_reset.W/'.tmp'))
        self.controller = subprocess.Popen(
            ['gewu-multiverse-lifecycle-fixture', '--exact',
             'engine::tests::operator_lifecycle_process_fixture', '--ignored',
             '--nocapture', '--test-threads=1'],
            executable=os.environ['GEWU_ENGINE_TEST_BINARY'], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop_controller)
        self.first = self.wait_for_run()
        self.first_pid = int((self.first/'session.pid').read_text())
        self.first_transcript = (self.first/'transcript.jsonl').read_bytes()

    def stop_controller(self):
        (self.root/'fixture-stop').touch()
        try:
            output = self.controller.communicate(timeout=6)[0]
        except subprocess.TimeoutExpired:
            self.controller.kill()
            output = self.controller.communicate(timeout=3)[0]
        self.assertEqual(self.controller.returncode, 0, output.decode(errors='replace'))

    def wait_for(self, condition, description, seconds=8):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            if condition():
                return
            if self.controller.poll() is not None:
                self.fail('controller exited before '+description)
            time.sleep(.02)
        self.fail('timed out waiting for '+description)

    def wait_for_run(self, excluding=None):
        def ready():
            return [run for run in (self.state/'author-01').glob('run-*')
                    if run != excluding and (run/'fixture-ready').exists()
                    and (run/'session.pid').exists()]
        self.wait_for(ready, 'a real child to read its instruction')
        return sorted(ready())[-1]

    def process_alive(self, pid):
        try:
            return role_reset.process_info(pid)['state'] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            return False

    def assert_settled(self, run, pid):
        self.wait_for(lambda: not (run/'session.pid').exists(), 'PID cleanup')
        self.assertFalse((run/'session.pgid').exists())
        self.assertFalse(self.process_alive(pid))
        contents = (run/'transcript.jsonl').read_bytes()
        self.assertTrue(contents.startswith(self.first_transcript))
        endings = [json.loads(line) for line in contents.splitlines()
                   if json.loads(line).get('event') == 'run_finished']
        self.assertEqual(len(endings), 1, 'one durable ending is required')
        self.assertEqual(endings[0]['outcome'], 'stopped')

    def test_watchdog_real_online_reset_stops_checkpoints_and_resumes(self):
        # A broker child outside the runner's process group must also be stopped,
        # without touching the controller or any real role UID.
        command = subprocess.Popen(['/bin/sleep', '60'], user=self.uid,
                                   group=self.uid, extra_groups=[], start_new_session=True)
        def cleanup_command():
            if command.poll() is None:
                command.kill()
            command.wait(timeout=3)
        self.addCleanup(cleanup_command)
        monitor = self.root/'dead-end-observations'
        dead_end_watchdog.scan(self.config, self.state, monitor, now=100000)
        first = json.loads((monitor/'author-01.json').read_text())
        self.assertEqual(first['action'], 'observe')
        self.assertTrue(first['runner_live'])
        self.assertTrue(first['sustained_session_failure'])
        self.assertFalse(first['waiting'])
        # Same real source and transcript, second observation after 1 h. No
        # online_reset, observer, process identity or ACK operation is mocked.
        dead_end_watchdog.scan(self.config, self.state, monitor, now=103601)
        result = json.loads((monitor/'author-01.json').read_text())
        self.assertEqual(result['action'], 'reset_ready', result)
        receipt = json.loads((self.state/'role-reset'/(result['reset_id']+'.json')).read_text())
        self.assertEqual(receipt['mode'], 'online')
        self.assertTrue(receipt['hold_released'])
        self.assertEqual(receipt['pause_controller_pid'], self.controller.pid)
        self.assertEqual(receipt['survivors'], [])
        self.assertIn(command.pid, [entry['pid'] for entry in receipt['processes']])
        command.wait(timeout=3)
        self.assert_settled(self.first, self.first_pid)
        self.assertFalse((self.state/'operator-role-holds/author-01').exists())
        ack = json.loads((self.state/'author-01/operator-hold-ack.json').read_text())
        self.assertEqual(ack['nonce'], receipt['pause_nonce'])
        self.assertTrue(ack['quiescent'])
        self.assertEqual(self.ledger.read_bytes(), self.original_ledger)
        self.assertEqual(self.work.read_text(), 'preserve existing source and numerical evidence\n')
        resumed = self.wait_for_run(excluding=self.first)
        expected = 'Operator reset: '+receipt['reset_id']
        self.assertIn(expected, (resumed/'fixture-instruction.txt').read_text())
        self.assertIn(expected, (self.role/'instruction.md').read_text())
        self.assertIn('Preserve the isolated author policy.', (self.role/'instruction.md').read_text())
        self.assertIn('do the work', (self.controller_role/'instruction.md').read_text())
        instruction_event = next(json.loads(line) for line in
                                 (resumed/'transcript.jsonl').read_text().splitlines()
                                 if json.loads(line)['event'] == 'instruction_read')
        self.assertEqual(instruction_event['digest'], hashlib.sha256(
            (self.controller_role/'instruction.md').read_bytes()).hexdigest())
        # The next real session must not turn the same unchanged source into a
        # repeated automatic reset. Persistent attempted-signature survives.
        dead_end_watchdog.scan(self.config, self.state, monitor, now=104001)
        self.assertEqual(json.loads((monitor/'author-01.json').read_text())['action'],
                         'already_attempted')
        self.assertEqual(len(list((self.state/'role-reset').glob('*.json'))), 1)

    def test_review_identity_pause_and_feedback_resume_real_runner(self):
        current = dict(key='commit:tasks/fixture', generation=3,
                       commit='a'*40, task='tasks/fixture', state='reviewing',
                       author_must_pause=True)
        assignment = self.controller_role/'review-current-assignment.json'
        feedback = self.controller_role/'review-feedback.json'
        assignment.write_text(json.dumps(current))
        # An old generation is never accepted as a review of the current task.
        feedback.write_text(json.dumps(dict(current, generation=2)))
        time.sleep(.2)
        self.assertTrue(self.process_alive(self.first_pid))
        feedback.write_text(json.dumps(current))
        self.assert_settled(self.first, self.first_pid)
        time.sleep(.2)
        self.assertEqual(len(list((self.state/'author-01').glob('run-*'))), 1)
        feedback.write_text(json.dumps(dict(current, state='waiting_author', author_must_pause=False)))
        resumed = self.wait_for_run(excluding=self.first)
        self.assertNotEqual(int((resumed/'session.pid').read_text()), self.first_pid)
        self.assertEqual(self.ledger.read_bytes(), self.original_ledger)
        self.assertEqual(self.work.read_text(), 'preserve existing source and numerical evidence\n')


if __name__ == '__main__':
    unittest.main()
