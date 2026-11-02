import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deployment_paths import WORKSPACE
from record_operator_maintenance import record


class MaintenanceRecordingTests(unittest.TestCase):
    def test_confirmed_window_pauses_without_resetting_or_reopening_ideas(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE/'.tmp') as raw:
            root=Path(raw);runtime=root/'runtime';runtime.mkdir()
            proof=root/'proof.json'
            proof.write_text(json.dumps({'runtime':str(runtime),'production_started':False,
                'status':'software_ready_payload_pending','started_at':200,
                'roles':{'author-01':{'uid':29001,'gid':29001}}}));proof.chmod(0o600)
            clock=runtime/'.config/gewu-multiverse/agents/author-01/home/role/.state/author-iteration/ledger.json'
            clock.parent.mkdir(parents=True);clock.write_text(json.dumps({'ideas':{
                'current':{'status':'active','active_seconds':100,'last_at':100,'events':[{'action':'start','at':0}]},
                'old':{'status':'abandoned','active_seconds':20000,'last_at':100,'events':[{'action':'start','at':0}]}}}))
            os.chown(clock,29001,29001);clock.chmod(0o600)
            with self.assertRaises(PermissionError):record(runtime,proof,continuity_confirmed=False,now=250)
            result=record(runtime,proof,continuity_confirmed=True,pause_clocks=True,now=250)
            ledger=json.loads(clock.read_text())['ideas']
            self.assertEqual(ledger['current']['status'],'session_paused')
            self.assertEqual(ledger['current']['active_seconds'],200)
            self.assertEqual(ledger['current']['events'][0],{'action':'start','at':0})
            self.assertTrue(ledger['current']['automatic_session_pause'])
            self.assertEqual(ledger['old']['status'],'abandoned')
            self.assertEqual(clock.stat().st_uid,29001)
            record(runtime,proof,continuity_confirmed=True,now=300)
            path=runtime/'.state/gewu-multiverse/maintenance/verified-pauses.json'
            value=json.loads(path.read_text())
            self.assertEqual(len(value['intervals']),1)
            self.assertEqual(value['intervals'][0]['started_at'],200)
            self.assertEqual(value['intervals'][0]['finished_at'],300)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with patch('record_operator_maintenance.role_processes',return_value=[12345]):
                with self.assertRaisesRegex(RuntimeError,'still active'):
                    record(runtime,proof,continuity_confirmed=True,now=350)
            self.assertEqual(json.loads(path.read_text())['observed_at'],300)


if __name__=='__main__':unittest.main()
