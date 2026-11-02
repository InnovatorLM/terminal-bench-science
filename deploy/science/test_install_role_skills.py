import json,tempfile,unittest
from pathlib import Path
from install_role_skills import install,selected,bundle,BEGIN,SOURCE
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
                names=install(root,role,owner=None);manifest=(root/'skills/manifest.json').read_bytes()
                install(root,role,owner=None)
                self.assertEqual(set(names),names_expected)
                self.assertEqual((root/'instruction.md').read_text().count(BEGIN),1)
                self.assertIn('Original instructions',(root/'instruction.md').read_text())
                self.assertEqual((root/'skills/manifest.json').read_bytes(),manifest)
                receipt=json.loads(manifest)
                for name in names:
                    self.assertEqual(receipt['skills'][name],bundle(SOURCE/name))
                    self.assertEqual(bundle(root/'skills'/name),bundle(SOURCE/name))
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
