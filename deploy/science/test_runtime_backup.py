import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import runtime_backup

W=Path('/mnt/data/zhanglinfeng/workspace')

class CycleComplete(Exception):pass

class BackupTests(unittest.TestCase):
    def test_fallback_retains_evidence_and_excludes_credentials_and_external_links(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);src=base/'source';src.mkdir();dst=base/'backup';dst.mkdir();dst.chmod(0o755)
            for name in ['credentials.toml','git-credential.json','dawnfm.env','service.token','app.ini',
                         'kubeconfig','private.key','custom-secret-dir/key','secrets/key','.cache/data','.tmp/cache',
                         'worker.pid','state.lock','trajectory.json','review.json','session.stdout.log']:
                path=src/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture')
            external=base/'external';external.write_text('fixture-outside')
            (src/'external-link').symlink_to(external)
            with patch('sys.argv',['backup','--source',str(src),'--target',str(dst)]),\
                 patch.object(runtime_backup.shutil,'which',return_value=None),\
                 patch.object(runtime_backup.time,'sleep',side_effect=CycleComplete):
                with self.assertRaises(CycleComplete):runtime_backup.main()
            files={p.relative_to(dst).as_posix() for p in dst.rglob('*') if p.is_file()}
            self.assertEqual(files,{'trajectory.json','review.json','session.stdout.log'})
            self.assertEqual(dst.stat().st_mode&0o777,0o700)
            receipt=json.loads((src/'.state/gewu-multiverse/runtime-backup.jsonl').read_text())
            self.assertEqual(receipt['status'],'ok')

if __name__=='__main__':unittest.main()
