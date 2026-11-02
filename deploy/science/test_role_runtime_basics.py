import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from role_runtime_basics import repair_role, probe

W=Path('/mnt/data/zhanglinfeng/workspace')

@unittest.skipUnless(os.geteuid()==0, 'operator provisioning test')
class RuntimeBasicsTests(unittest.TestCase):
    def test_repairs_missing_files_without_replacing_role_identity(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);root=base/'role';root.mkdir()
            source=base/'.config/universe-roles';source.mkdir(parents=True)
            credential=source/'evaluator-01.json'
            credential.write_text(json.dumps({'login':'fixture','token':'fixture-secret'}));credential.chmod(0o600)
            with patch('role_runtime_basics.probe',return_value={'ready':True}):
                repair_role(root,'evaluator-01',base,uid=21191)
                target=root/'home/role/git-credential.json'
                self.assertEqual(target.stat().st_uid,21191)
                self.assertEqual(target.stat().st_mode&0o777,0o600)
                original=target.read_bytes()
                credential.unlink()
                repair_role(root,'evaluator-01',base,uid=21191)
                self.assertEqual(target.read_bytes(),original)
                self.assertEqual((root/'workspace').stat().st_uid,65534)
                self.assertEqual((root/'workspace').stat().st_gid,21191)
                self.assertEqual((root/'workspace').stat().st_mode&0o7777,0o2770)
                self.assertTrue(os.access(root/'usr/local/bin/role-askpass',os.X_OK))
                self.assertEqual((root/'usr/local/bin/cmp').read_bytes(),Path('/usr/bin/cmp').read_bytes())
                self.assertEqual((root/'usr/local/bin/cmp').stat().st_uid,0)

    def test_repairs_immutable_chroot_paths_for_git(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);root=base/'role';root.mkdir()
            source=base/'.config/universe-roles';source.mkdir(parents=True)
            credential=source/'author-01.json'
            credential.write_text(json.dumps({'login':'fixture','token':'fixture-secret'}));credential.chmod(0o600)
            etc=root/'etc'; etc.mkdir(); (etc/'gitconfig').write_text('')
            helper=root/'usr/lib/git-core/git-remote-http'; helper.parent.mkdir(parents=True); helper.write_text('#!/bin/sh\n')
            etc.chmod(0o700); (etc/'gitconfig').chmod(0o600); helper.chmod(0o600)
            with patch('role_runtime_basics.probe',return_value={'ready':True}):
                repair_role(root,'author-01',base,uid=21191)
            self.assertEqual(etc.stat().st_uid,0)
            self.assertEqual(etc.stat().st_mode&0o777,0o755)
            self.assertEqual((etc/'gitconfig').stat().st_mode&0o777,0o644)
            self.assertEqual(helper.stat().st_mode&0o777,0o755)

    def test_accepts_internal_git_remote_https_alias(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);root=base/'role';root.mkdir()
            source=base/'.config/universe-roles';source.mkdir(parents=True)
            credential=source/'evaluator-01.json'
            credential.write_text(json.dumps({'login':'fixture','token':'fixture-secret'}));credential.chmod(0o600)
            helper=root/'usr/lib/git-core/git-remote-http';helper.parent.mkdir(parents=True);helper.write_text('#!/bin/sh\n');helper.chmod(0o755)
            (helper.parent/'git-remote-https').symlink_to('git-remote-http')
            with patch('role_runtime_basics.probe',return_value={'ready':True}):
                repair_role(root,'evaluator-01',base,uid=21191)
            self.assertEqual((helper.parent/'git-remote-https').resolve(),helper.resolve())

    def test_role_path_symlink_cannot_overwrite_external_file(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);root=base/'role';root.mkdir();outside=base/'outside';outside.mkdir()
            (root/'home').symlink_to(outside)
            with self.assertRaises(ValueError):repair_role(root,'evaluator-01',base,uid=21191)
            self.assertEqual(list(outside.iterdir()),[])

    def test_probe_uses_role_uid_and_does_not_return_error_output(self):
        with patch('role_runtime_basics.subprocess.run',return_value=subprocess.CompletedProcess([],1,'fixture-secret','fixture-secret')) as run:
            result=probe(W,21191)
        self.assertFalse(result['ready'])
        self.assertNotIn('fixture-secret',json.dumps(result))
        command=run.call_args.args[0]
        self.assertIn('--userspec=21191:21191',command)
        self.assertIn('--groups=21191',command)

if __name__=='__main__':unittest.main()
