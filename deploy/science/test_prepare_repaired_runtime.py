import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from prepare_repaired_runtime import prepare,stage_software,WORKSPACE


class PreparationTests(unittest.TestCase):
    def test_checkpoint_preserves_controls_and_does_not_copy_software_or_claim_ready(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE/'.tmp') as raw:
            root=Path(raw);source=root/'source';donor=root/'donor';donor.mkdir()
            broker=source/'.state/gewu-multiverse/role-broker/config.json';broker.parent.mkdir(parents=True)
            broker.write_text(json.dumps({'roles':{'author-01':{'uid':21001,'gid':21001}}}))
            path=source/'.config/gewu-multiverse/credentials.toml';path.parent.mkdir(parents=True);path.write_text('fixture secret');path.chmod(0o600)
            software=donor/'.config/gewu-multiverse/agents/author-01/usr/local/bin/python3'
            software.parent.mkdir(parents=True);software.write_bytes(b'not-a-real-interpreter')
            output=root/'prepared';result=prepare(source,donor,output,1)
            self.assertFalse(result['runnable']);self.assertFalse(result['software_copied'])
            self.assertTrue(result['roles'][0]['donor_software_present']['usr/local/bin/python3'])
            self.assertFalse((output/'controls/.config/gewu-multiverse/agents/author-01/usr').exists())
            self.assertEqual(path.read_text(),'fixture secret')
            for item in result['controls']:
                copy=output/'controls'/item['path']
                self.assertEqual(copy.stat().st_mode&0o777,0o600)
                self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(),item['sha256'])
            self.assertNotIn('fixture secret',(output/'migration-plan.json').read_text())
            with self.assertRaises(ValueError):prepare(source,donor,output)

    def test_linked_control_is_refused(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE/'.tmp') as raw:
            root=Path(raw);source=root/'source';donor=root/'donor';donor.mkdir()
            broker=source/'.state/gewu-multiverse/role-broker/config.json';broker.parent.mkdir(parents=True)
            broker.write_text(json.dumps({'roles':{'author-01':{'uid':21001,'gid':21001}}}))
            control=source/'.config/gewu-multiverse/multiverse.toml';control.parent.mkdir(parents=True)
            control.symlink_to(broker)
            with self.assertRaises(ValueError):prepare(source,donor,root/'out')

    def test_software_moves_are_resumable_and_do_not_touch_donor_work(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE/'.tmp') as raw:
            root=Path(raw);source=root/'source';donor=root/'donor';runtime=root/'runtime'
            broker=source/'.state/gewu-multiverse/role-broker/config.json';broker.parent.mkdir(parents=True)
            broker.write_text(json.dumps({'roles':{'author-01':{'uid':29001,'gid':29001}}}))
            old=donor/'.config/gewu-multiverse/agents/author-01'
            (old/'bin').mkdir(parents=True);(old/'bin/bash').write_bytes(b'fixture executable')
            (old/'workspace').mkdir();(old/'workspace/keep.txt').write_text('latest donor work')
            identity=(old/'bin').stat().st_ino;journal=root/'journal.json'
            probe=lambda path,entry:{'uid':entry['uid'],'fixture':True}
            first=stage_software(source,donor,runtime,journal,probe)
            new=runtime/'.config/gewu-multiverse/agents/author-01'
            self.assertEqual((new/'bin').stat().st_ino,identity)
            self.assertFalse((old/'bin').exists())
            self.assertEqual((old/'workspace/keep.txt').read_text(),'latest donor work')
            self.assertEqual(first['status'],'software_ready_payload_pending')
            second=stage_software(source,donor,runtime,journal,probe)
            self.assertEqual(second['roles']['author-01']['status'],'verified')
            self.assertEqual(journal.stat().st_mode&0o777,0o600)

    def test_software_existing_destination_fails_without_overwrite(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE/'.tmp') as raw:
            root=Path(raw);source=root/'source';donor=root/'donor';runtime=root/'runtime'
            broker=source/'.state/gewu-multiverse/role-broker/config.json';broker.parent.mkdir(parents=True)
            broker.write_text(json.dumps({'roles':{'author-01':{'uid':29001,'gid':29001}}}))
            scope=Path('.config/gewu-multiverse/agents/author-01/bin')
            for base in (donor,runtime):(base/scope).mkdir(parents=True)
            (donor/scope/'bash').write_text('source');(runtime/scope/'bash').write_text('target')
            with self.assertRaisesRegex(ValueError,'already exists'):
                stage_software(source,donor,runtime,root/'journal.json',lambda *_:{})
            self.assertEqual((donor/scope/'bash').read_text(),'source')
            self.assertEqual((runtime/scope/'bash').read_text(),'target')


if __name__=='__main__':unittest.main()
