"""Exercise the shared image build slot using real competing processes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import build_task_images as build
from evaluation_queue import W


class BuilderSlotTests(unittest.TestCase):
    def test_inherited_slot_avoids_deadlock_and_competitor_waits(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);entered=root/'entered'
            inherit=("import sys;from pathlib import Path;import build_task_images as b;"
                     "b.W=Path(sys.argv[1]);"
                     "exec('with b.builder_slot(int(sys.argv[2])):\\n print(\\\"inherited\\\")')")
            compete=("import sys;from pathlib import Path;import build_task_images as b;"
                     "b.W=Path(sys.argv[1]);"
                     "exec('with b.builder_slot():\\n Path(sys.argv[2]).touch()')")
            with patch.object(build,'W',root):
                with build.builder_slot() as fd:
                    got=subprocess.run([sys.executable,'-c',inherit,str(root),str(fd)],
                                       pass_fds=(fd,),cwd=Path(__file__).parent,capture_output=True,text=True,timeout=15)
                    self.assertEqual(got.returncode,0,got.stderr)
                    child=subprocess.Popen([sys.executable,'-c',compete,str(root),str(entered)],cwd=Path(__file__).parent)
                    try:
                        time.sleep(.3)
                        self.assertIsNone(child.poll())
                        self.assertFalse(entered.exists())
                    except BaseException:
                        child.terminate();child.wait();raise
                self.assertEqual(child.wait(timeout=15),0)
                self.assertTrue(entered.exists())

    def test_inherited_descriptor_cannot_point_to_another_file(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            with patch.object(build,'W',root),build.builder_slot():
                fd=os.open(root/'not-a-lock',os.O_CREAT|os.O_RDWR,0o600)
                try:
                    with self.assertRaises(ValueError):
                        with build.builder_slot(fd):pass
                finally:os.close(fd)


if __name__=='__main__':unittest.main()
