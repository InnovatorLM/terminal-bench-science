import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from universe_observer import scan

class ObserverTests(unittest.TestCase):
    def test_explicit_deployment_never_reads_old_runtime(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEST_TMPDIR', '/mnt/data/zhanglinfeng/workspace/.tmp')) as tmp:
            root=Path(tmp); selected=root/'selected'; old=root/'old'
            for universe,message in ((selected,'healthy'),(old,'rewards=null ValueError')):
                run=universe/'.state/gewu-multiverse/author-01/run-1'
                run.mkdir(parents=True); (run/'run.log').write_text(message)
            result=scan(selected)
            self.assertEqual(result['universe_root'],str(selected/'.state/gewu-multiverse'))
            self.assertFalse(result['findings'])
            self.assertEqual(result['actions']['mode'],'proposal_only')
            self.assertFalse(result['actions']['automatic_role_reset'])

    def test_environment_selects_deployment_and_no_fallback(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEST_TMPDIR', '/mnt/data/zhanglinfeng/workspace/.tmp')) as tmp:
            root=Path(tmp); selected=root/'selected'; old=root/'old'
            run=old/'.state/gewu-multiverse/author-01/run-1'
            run.mkdir(parents=True); (run/'run.log').write_text('rewards=null ValueError')
            with patch('universe_observer.DEFAULT_ROOT',root), patch.dict(os.environ,GEWU_UNIVERSE_ROOT=str(selected)):
                result=scan()
            self.assertEqual(result['universe_root'],str(selected/'.state/gewu-multiverse'))
            self.assertEqual([x['kind'] for x in result['findings']],['deployment_missing'])

    def test_wait_or_one_error_never_resets(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEST_TMPDIR', '/mnt/data/zhanglinfeng/workspace/.tmp')) as tmp:
            root=Path(tmp); run=root/'.state/gewu-multiverse/author-01/run-1'
            run.mkdir(parents=True); (run/'run.log').write_text('waiting ValueError')
            os.utime(run,(1,1)); result=scan(root)
            self.assertFalse(result['actions']['automatic_role_reset'])
            self.assertTrue(all(x.get('reset_eligible') is not True for x in result['findings']))

if __name__ == '__main__': unittest.main()
