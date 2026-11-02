"""Relocate immutable registrations without rewriting their signed template."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from evaluation_queue import W, atomic_json, digest_tree, validated_template


class MigrationTests(unittest.TestCase):
    def fixture(self, root):
        registered=root/'registered'
        snapshot=registered/'task'
        snapshot.mkdir(parents=True)
        (snapshot/'task.toml').write_text('version="1.0"\n')
        original=W/'universes/historical/.state/registered/task'
        host='dptech-sh-pai-acr-registry.cn-shanghai.cr.aliyuncs.com/terminal-bench-universe/'
        config={'n_attempts':1,'n_concurrent_trials':1,'retry':{'max_retries':0},
                'environment':{'type':'ack','kwargs':{'use_sandbox_claim':False,
                    'runtime_image':host+'task@sha256:'+'a'*64,
                    'verifier_image':host+'task-verifier@sha256:'+'b'*64}},
                'agents':[{'import_path':'universe_metrics:MetadataOnlyMiniSwe',
                    'model_name':'openai/deepseek-v4.1-flash-1017','env':{
                        'OPENAI_API_KEY':'${OPENAI_API_KEY}',
                        'OPENAI_BASE_URL':'https://dawnfm.gravarc.cn/v1',
                        'OPENAI_API_BASE':'https://dawnfm.gravarc.cn/v1'}}],
                'datasets':[{'path':str(original.parent),'task_names':[original.name]}]}
        template=registered/'template.json'
        atomic_json(template,config)
        entry={'snapshot':str(snapshot),'snapshot_sha256':digest_tree(snapshot),
               'template':str(template),'template_sha256':hashlib.sha256(template.read_bytes()).hexdigest(),
               'source_commit':'c'*40}
        provenance={**entry,'snapshot':str(original),'template':str(original.parent/'template.json')}
        atomic_json(registered/'provenance.json',provenance)
        request={'job_id':'d'*32,'snapshot_sha256':entry['snapshot_sha256'],'evaluation_id':'task-v1'}
        return entry,request,provenance,config

    def test_workspace_tmp_target_loads_with_immutable_source_provenance(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            entry,request,_,original=self.fixture(Path(raw))
            path=Path(entry['template']);before=path.read_bytes()
            config=validated_template(entry,request)
            self.assertEqual(config['datasets'],[{'path':str(Path(entry['snapshot']).parent),'task_names':['task']}])
            original['datasets']=config['datasets']
            self.assertEqual(config,original)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(hashlib.sha256(before).hexdigest(),entry['template_sha256'])
            self.assertEqual(digest_tree(entry['snapshot']),entry['snapshot_sha256'])

    def test_migration_refuses_hash_commit_and_original_dataset_mismatches(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            entry,request,provenance,_=self.fixture(Path(raw))
            path=Path(entry['snapshot']).parent/'provenance.json'
            changes=[{'snapshot_sha256':'f'*64},{'template_sha256':'f'*64},
                     {'source_commit':'f'*40},{'snapshot':str(W/'other/task')},
                     {'snapshot':'relative/task'}]
            for change in changes:
                with self.subTest(change=change):
                    atomic_json(path,{**provenance,**change})
                    with self.assertRaisesRegex(ValueError,'Dataset is not the registered snapshot'):
                        validated_template(entry,request)
            atomic_json(path,provenance)
            for bad_commit in (None,'','not-a-commit'):
                changed=dict(entry,source_commit=bad_commit)
                atomic_json(path,{**provenance,'source_commit':bad_commit})
                with self.assertRaises(ValueError):validated_template(changed,request)

    def test_provenance_must_be_present_root_owned_and_not_writable_or_linked(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            entry,request,provenance,_=self.fixture(Path(raw))
            path=Path(entry['snapshot']).parent/'provenance.json'
            path.chmod(0o666)
            with self.assertRaises(ValueError):validated_template(entry,request)
            path.chmod(0o600)
            os.chown(path,21010,21010)
            try:
                with self.assertRaises(ValueError):validated_template(entry,request)
            finally:os.chown(path,0,0)
            path.unlink()
            with self.assertRaises(OSError):validated_template(entry,request)
            other=path.with_name('other.json');atomic_json(other,provenance);path.symlink_to(other)
            with self.assertRaises(ValueError):validated_template(entry,request)

    def test_snapshot_template_and_request_still_require_exact_hashes(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            entry,request,_,_=self.fixture(Path(raw))
            with self.assertRaises(ValueError):validated_template(entry,dict(request,snapshot_sha256='f'*64))
            snapshot=Path(entry['snapshot']);(snapshot/'extra').write_text('mutation')
            with self.assertRaises(ValueError):validated_template(entry,request)
            (snapshot/'extra').unlink()
            path=Path(entry['template']);path.write_bytes(path.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'Registered template changed'):
                validated_template(entry,request)


if __name__=='__main__':unittest.main()
