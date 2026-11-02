import json,tempfile,unittest
from pathlib import Path
from install_role_skills import install,selected,bundle,discovery,BEGIN,SOURCE,RECEIPT
W=Path('/mnt/data/zhanglinfeng/workspace')
class ProvisionTests(unittest.TestCase):
    def test_roles_discovery_and_idempotence(self):
        expected={
            'author-04':{'acs-task-pipeline','harbor-acs-evaluation','acs-task-troubleshooting'},
            'evaluator-01':{'acs-task-pipeline','harbor-acs-evaluation','acs-quality-rollout-review','acs-task-troubleshooting'},
            'curator':{'acs-task-pipeline','acs-review-coordination','acs-task-troubleshooting'},
        }
        for role,names_expected in expected.items():
            with self.subTest(role=role),tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
                root=Path(d);(root/'instruction.md').write_text('Original instructions')
                names=install(root,role,owner=None);manifest=(root/'skills'/RECEIPT).read_bytes()
                install(root,role,owner=None)
                self.assertEqual(set(names),names_expected)
                self.assertEqual((root/'instruction.md').read_text().count(BEGIN),1)
                self.assertIn('Original instructions',(root/'instruction.md').read_text())
                self.assertEqual((root/'skills'/RECEIPT).read_bytes(),manifest)
                self.assertFalse((root/'skills/manifest.json').exists())
                self.assertIn('/skills/.manifest.json',(root/'instruction.md').read_text())
                self.assertNotIn('/skills/manifest.json',(root/'instruction.md').read_text())
                self.assertEqual(set(discovery(root/'skills')['visible_entries']),names_expected)
                receipt=json.loads(manifest)
                for name in names:
                    self.assertEqual(receipt['skills'][name],bundle(SOURCE/name))
                    self.assertEqual(bundle(root/'skills'/name),bundle(SOURCE/name))
    def test_recognized_legacy_receipt_is_migrated_without_changing_resources(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
            root=Path(d);(root/'instruction.md').write_text('policy');install(root,'author-01',owner=None)
            hidden=root/'skills'/RECEIPT;legacy=root/'skills/manifest.json';before=hidden.read_bytes()
            hidden.rename(legacy)
            instruction=root/'instruction.md';instruction.write_text(instruction.read_text().replace('/skills/.manifest.json','/skills/manifest.json'))
            install(root,'author-01',owner=None)
            self.assertFalse(legacy.exists());self.assertEqual(hidden.read_bytes(),before)
            self.assertNotIn('/skills/manifest.json',instruction.read_text())
            for name in selected('author-01'):self.assertEqual(bundle(root/'skills'/name),bundle(SOURCE/name))
    def test_unrecognized_legacy_receipt_is_preserved(self):
        for contents in ('{"user":"custom file"}','plain text'):
            with self.subTest(contents=contents),tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
                root=Path(d);(root/'instruction.md').write_text('policy');(root/'skills').mkdir()
                legacy=root/'skills/manifest.json';legacy.write_text(contents)
                with self.assertRaisesRegex(ValueError,'not an installer receipt'):install(root,'author-01',owner=None)
                self.assertEqual(legacy.read_text(),contents)
                self.assertFalse((root/'skills'/RECEIPT).exists())
    def test_legacy_receipt_must_match_installed_resources(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
            root=Path(d);(root/'instruction.md').write_text('policy');install(root,'author-01',owner=None)
            hidden=root/'skills'/RECEIPT;legacy=root/'skills/manifest.json';hidden.rename(legacy)
            resource=root/'skills/acs-task-pipeline/SKILL.md';resource.write_text('changed resource')
            with self.assertRaisesRegex(ValueError,'does not match installed'):install(root,'author-01',owner=None)
            self.assertTrue(legacy.exists());self.assertEqual(resource.read_text(),'changed resource')
    def test_legacy_receipt_symlink_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
            root=Path(d);(root/'instruction.md').write_text('policy');install(root,'author-01',owner=None)
            hidden=root/'skills'/RECEIPT;before=hidden.read_bytes();legacy=root/'skills/manifest.json';legacy.symlink_to(hidden)
            with self.assertRaisesRegex(ValueError,'manifest is a symlink'):install(root,'author-01',owner=None)
            self.assertTrue(legacy.is_symlink());self.assertEqual(hidden.read_bytes(),before)
    def test_discovery_refuses_visible_file_or_directory_without_skill(self):
        for as_directory in (False,True):
            with self.subTest(as_directory=as_directory),tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
                root=Path(d);(root/'instruction.md').write_text('policy');install(root,'author-01',owner=None)
                entry=root/'skills/stray'
                if as_directory:entry.mkdir()
                else:entry.write_text('not a skill')
                with self.assertRaisesRegex(ValueError,'Visible skill entry'):discovery(root/'skills')
    def test_resource_change_changes_version_and_reinstall_repairs_it(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
            root=Path(d);(root/'instruction.md').write_text('policy');install(root,'author-01',owner=None)
            skill=root/'skills/acs-task-pipeline';before=bundle(skill)
            reference=skill/'references/receipt.md';reference.write_text('obsolete receipt')
            self.assertNotEqual(bundle(skill)['sha256'],before['sha256'])
            install(root,'author-01',owner=None)
            self.assertEqual(bundle(skill),before)
    def test_skill_root_link_refused_without_writing_target(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
            root=Path(d);target=root/'elsewhere';target.mkdir();(root/'skills').symlink_to(target,target_is_directory=True)
            (root/'instruction.md').write_text('policy')
            with self.assertRaisesRegex(ValueError,'root is a symlink'):install(root,'author-01',owner=None)
            self.assertFalse(list(target.iterdir()))
    def test_bundle_rejects_reference_link(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as d:
            root=Path(d);(root/'SKILL.md').write_text('skill');(root/'reference').symlink_to(root/'SKILL.md')
            with self.assertRaisesRegex(ValueError,'links forbidden'):bundle(root)
    def test_initializer_calls_installer(self):
        self.assertIn('install_skills(root, role)',Path(__file__).with_name('prepare_role_runtime.py').read_text())
