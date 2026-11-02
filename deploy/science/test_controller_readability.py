import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from controller_readability import check
from evaluation_queue import W


class ControllerReadabilityTests(unittest.TestCase):
    def test_absent_state_is_valid(self):
        check(W/'.tmp/nonexistent-controller-readability-fixture')

    @unittest.skipUnless(os.geteuid() == 0, 'Requires actual privilege drop')
    def test_private_root_plan_blocks_then_controller_owned_plan_passes(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root = Path(raw); root.chmod(0o755)
            run = root/'author-01/run-example';run.mkdir(parents=True)
            run.parent.chmod(0o755);run.chmod(0o755)
            plan = run/'plan.json';plan.write_text('private test value');plan.chmod(0o600)
            command = ['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups',
                       os.sys.executable, str(Path(__file__).with_name('controller_readability.py')), str(root)]
            blocked = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn(str(plan), blocked.stderr)
            self.assertNotIn('private test value', blocked.stderr)
            os.chown(plan, 65534, 65534)
            passed = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(passed.returncode, 0, passed.stderr)


if __name__ == '__main__':
    unittest.main()
