import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from evaluation_queue import W


class OwnedServiceTests(unittest.TestCase):
    def test_original_launcher_exit_stops_replacement_service(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root = Path(raw)
            ready, cleaned = root / 'ready', root / 'cleaned'
            owner = subprocess.Popen(['sleep', '30'])
            guard = subprocess.Popen([
                sys.executable, str(Path(__file__).with_name('owned_service.py')),
                '--owner-pid', str(owner.pid), '/bin/bash', '-c',
                'trap \'echo clean >"$2"; exit 0\' TERM; echo ready >"$1"; while true; do sleep .1; done',
                'service', str(ready), str(cleaned),
            ])
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    if guard.poll() is not None:
                        self.fail('Service guard exited before readiness')
                    time.sleep(.05)
                self.assertTrue(ready.exists())
                owner.terminate()
                owner.wait(timeout=5)
                guard.wait(timeout=10)
                self.assertEqual(guard.returncode, 0)
                self.assertEqual(cleaned.read_text().strip(), 'clean')
            finally:
                for process in (guard, owner):
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
