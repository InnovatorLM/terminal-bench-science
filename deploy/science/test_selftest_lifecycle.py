import subprocess
import tempfile
import unittest
import sys
import time
from pathlib import Path
from evaluation_queue import W
from selftest_lifecycle import recover_interrupted, owned_run

class RecoveryTests(unittest.TestCase):
    def test_interrupted_job_keeps_source(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root = Path(raw)
            work = root / 'job'
            work.mkdir()
            source = work / 'upload.tar'
            source.write_bytes(b'preserved')
            result = root / 'result.json'
            result.write_text('{"status":"building"}')
            published = []
            self.assertTrue(recover_interrupted(work, result, published.append))
            self.assertEqual(published[0]['status'], 'interrupted')
            self.assertEqual(source.read_bytes(), b'preserved')

    def test_finished_jobs_are_not_repeated(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root = Path(raw)
            result = root / 'r'
            result.write_text('{"status":"selftest_passed"}')
            self.assertTrue(recover_interrupted(root / 'job', result, lambda _: self.fail('republished')))

    def test_new_job_is_eligible(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            self.assertFalse(recover_interrupted(Path(raw) / 'job', Path(raw) / 'r', lambda _: None))

    def test_malformed_result_does_not_crash_worker_or_restart_cloud_work(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            root=Path(raw);result=root/'result';result.write_text('{partial')
            published=[]
            self.assertTrue(recover_interrupted(root/'work',result,published.append))
            self.assertEqual(published[0]['status'],'interrupted')
            self.assertIn('Malformed',published[0]['receipt_recovery'])
            self.assertEqual(result.read_text(),'{partial')
            self.assertEqual((root/'work/malformed-prior-result.json').read_text(),'{partial')

    def test_timeout_is_not_swallowed(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            owned_run(['sleep', '20'], timeout=0.05)

    def test_timeout_allows_guard_to_finish_cleanup_before_force_kill(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);ready=root/'ready';cleaned=root/'cleaned'
            script=('import signal,time;from pathlib import Path;'
                    'signal.signal(signal.SIGTERM,lambda *_:(time.sleep(.15),Path('+repr(str(cleaned))+').touch(),exit(0)));'
                    'Path('+repr(str(ready))+').touch();time.sleep(60)')
            with self.assertRaises(subprocess.TimeoutExpired):
                owned_run([sys.executable,'-c',script],timeout=1,cleanup_grace=2)
            self.assertTrue(ready.exists())
            self.assertTrue(cleaned.exists())

    def test_successful_wrapper_cannot_leave_late_writing_child(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            target = Path(raw) / 'late'
            descendant = 'import time;from pathlib import Path;time.sleep(1);Path(' + repr(str(target)) + ').write_text("orphan")'
            parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(descendant) + '])'
            result = owned_run([sys.executable, '-c', parent], timeout=3)
            self.assertEqual(result.returncode, 0)
            time.sleep(1.2)
            self.assertFalse(target.exists())

    def test_successful_wrapper_reaps_sigterm_ignoring_descendant(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            target=Path(raw)/'late';ready=Path(raw)/'ready'
            descendant='import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('+repr(str(ready))+').touch();time.sleep(1);Path('+repr(str(target))+').touch()'
            parent='import subprocess,sys,time;from pathlib import Path;subprocess.Popen([sys.executable,"-c",'+repr(descendant)+']);\nwhile not Path('+repr(str(ready))+').exists():time.sleep(.01)'
            self.assertEqual(owned_run([sys.executable,'-c',parent],timeout=3).returncode,0)
            time.sleep(1.2);self.assertFalse(target.exists())

if __name__ == '__main__':
    unittest.main()
