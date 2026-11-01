import configparser
from pathlib import Path
import tempfile
import unittest
from forgejo_runtime_paths import normalize

W=Path('/mnt/data/zhanglinfeng/workspace')

class ForgejoPathsTests(unittest.TestCase):
    def test_rewrites_database_repository_and_data_paths_together(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);path=root/'app.ini'
            path.write_text('WORK_PATH = /old\n[database]\nDB_TYPE = sqlite3\nPATH = /old/forgejo.db\n[repository]\nROOT = /old/repositories\n[server]\nHTTP_PORT = 3000\nAPP_DATA_PATH = /old/data\n[security]\nSECRET_KEY = fixture-value\n')
            normalize(root,3001)
            cfg=configparser.ConfigParser(interpolation=None);cfg.read_string('[top]\n'+path.read_text())
            self.assertEqual(cfg['database']['PATH'],str(root/'data/forgejo.db'))
            self.assertEqual(cfg['repository']['ROOT'],str(root/'data/repositories'))
            self.assertEqual(cfg['server']['APP_DATA_PATH'],str(root/'data'))
            self.assertEqual(cfg['server']['HTTP_PORT'],'3001')
            self.assertEqual(cfg['security']['SECRET_KEY'],'fixture-value')

if __name__=='__main__':unittest.main()
