import configparser
from pathlib import Path
import tempfile
import unittest
from forgejo_runtime_paths import normalize

W=Path('/mnt/data/zhanglinfeng/workspace')

class ForgejoPathsTests(unittest.TestCase):
    def test_public_url_survives_runtime_normalization(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            (root/'app.ini').write_text('[server]\nROOT_URL = http://127.0.0.1:3000/\n')
            (root/'public-root-url').write_text('https://example.org:3443/\n')
            normalize(root,3000)
            cfg=configparser.ConfigParser();cfg.read_string('[top]\n'+(root/'app.ini').read_text())
            self.assertEqual(cfg['server']['ROOT_URL'],'https://example.org:3443/')
            self.assertEqual(cfg['server']['HTTP_PORT'],'3000')

    def test_rewrites_database_repository_and_data_paths_together(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);path=root/'app.ini'
            path.write_text('WORK_PATH = /old\n[database]\nDB_TYPE = sqlite3\nPATH = /old/forgejo.db\n[repository]\nROOT = /old/repositories\n[server]\nHTTP_PORT = 3000\nAPP_DATA_PATH = /old/data\n[log]\nROOT_PATH = /old/log\n[security]\nSECRET_KEY = fixture-value\n')
            normalize(root,3001)
            cfg=configparser.ConfigParser(interpolation=None);cfg.read_string('[top]\n'+path.read_text())
            self.assertEqual(cfg['database']['PATH'],str(root/'data/forgejo.db'))
            self.assertEqual(cfg['repository']['ROOT'],str(root/'data/repositories'))
            self.assertEqual(cfg['server']['APP_DATA_PATH'],str(root/'data'))
            self.assertEqual(cfg['server']['HTTP_PORT'],'3001')
            self.assertEqual(cfg['log']['ROOT_PATH'],str(root/'log'))
            self.assertEqual(cfg['security']['SECRET_KEY'],'fixture-value')

if __name__=='__main__':unittest.main()
