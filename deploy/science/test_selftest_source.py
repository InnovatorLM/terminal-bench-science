import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from evaluation_queue import W
from selftest_source import verify_source, SourceMismatchError

class SourceTests(unittest.TestCase):
    def test_upload_must_match_actual_commit(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            workspace = Path(raw)
            repo = workspace / 'repo'
            task = repo / 'tasks/example'
            task.mkdir(parents=True)
            (task / 'input.txt').write_text('original')
            (task / 'solve.sh').write_text('exit 0')
            (task / 'solve.sh').chmod(0o755)
            for args in [['init','-q'],['config','user.email','test@localhost'],['config','user.name','test'],['add','.'],['commit','-qm','fixture']]:
                subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
            commit = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
            upload = workspace / 'upload'
            shutil.copytree(task, upload)
            def verify():
                return verify_source(workspace, 'repo', 'tasks/example', commit, upload)
            self.assertTrue(verify()['source_git_tree_verified'])
            (upload / 'input.txt').write_text('changed')
            with self.assertRaises(SourceMismatchError):
                verify()
            (upload / 'input.txt').write_text('original')
            (upload / 'solve.sh').chmod(0o644)
            with self.assertRaises(SourceMismatchError):
                verify()
            (upload / 'solve.sh').chmod(0o755)
            (upload / 'unexpected').write_text('extra')
            with self.assertRaises(SourceMismatchError):
                verify()

    def test_repository_escape_refused(self):
        with tempfile.TemporaryDirectory(dir=W / '.tmp') as raw:
            with self.assertRaises(ValueError):
                verify_source(raw, '../outside', 'task', 'a'*40, raw)

if __name__ == '__main__':
    unittest.main()
