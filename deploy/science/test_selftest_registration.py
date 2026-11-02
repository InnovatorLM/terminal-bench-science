"""Offline identity bridge tests; no cloud or model execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from evaluation_queue import W, digest_tree
from build_task_images import REGISTRY, HARNESS, receipt_labels
from selftest_registration import prepare


class SelftestRegistrationTests(unittest.TestCase):
    def test_frozen_identity_preserved_and_unqualified_evidence_rejected(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            work=Path(raw);snapshot=work/'source/proof'
            for name in ('environment','tests'):
                (snapshot/name).mkdir(parents=True);(snapshot/name/'data').write_text(name)
            sha=digest_tree(snapshot);original='a'*40;execution='b'*40
            receipt=dict(source_commit=execution,source_task_path='proof',task_name='proof',
                         snapshot_sha256=sha,published=True,registry_config_linkage_verified=True,
                         scientific_acceptance=False,harness_versions=HARNESS,
                         environment_context_sha256=digest_tree(snapshot/'environment'),
                         tests_context_sha256=digest_tree(snapshot/'tests'),
                         runtime_adapter_context_sha256='c'*64,verifier_effective_context_sha256='d'*64,images={})
            for role,context in [('official',receipt['environment_context_sha256']),('runtime','c'*64),('verifier','d'*64)]:
                receipt['images'][role]=dict(published_digest=REGISTRY+'/proof'+('-verifier' if role=='verifier' else '')+'@sha256:'+'e'*64,
                    image_config_id='sha256:'+'f'*64,labels=receipt_labels(execution,sha,context,role))
            image=receipt['images']['runtime']['published_digest']
            template={'environment':{'kwargs':dict(runtime_image=image,verifier_image=receipt['images']['verifier']['published_digest'],pod_overrides={'spec':{'containers':[{'image':image}]}})}}
            status=dict(schema_version=2,status='selftest_passed',source_commit=original,source_task_path='tasks/proof',source_git_tree_verified=True,
                        quality_acceptance=False,execution_commit=execution,execution_task_path='proof',snapshot_sha256=sha,
                        images={r:v['published_digest'] for r,v in receipt['images'].items()},
                        trials=[dict(agent=a,exit_code=0,cleanup_confirmed=True,results=[dict(exception_type=None,rewards={'reward':reward})]) for a,reward in [('oracle',1),('nop',0)]])
            (work/'build').mkdir();(work/'build/receipt.json').write_text(json.dumps(receipt))
            def save(s): (work/'status.json').write_text(json.dumps(s))
            save(status);before=(work/'build/receipt.json').read_bytes()
            result=prepare(work,original,'tasks/proof',template)
            self.assertEqual(result['execution_commit'],execution)
            self.assertEqual(result['source_commit'],original)
            self.assertFalse(result['iteration_qualified'])
            self.assertEqual((work/'build/receipt.json').read_bytes(),before)
            for mutation in ('stale','cleanup','reward','boolean_reward','image','verification','snapshot','execution'):
                bad=copy.deepcopy(status)
                if mutation=='stale':bad['source_commit']='0'*40
                elif mutation=='cleanup':bad['trials'][0]['cleanup_confirmed']=False
                elif mutation=='reward':bad['trials'][1]['results'][0]['rewards']['reward']=1
                elif mutation=='boolean_reward':bad['trials'][0]['results'][0]['rewards']['reward']=True
                elif mutation=='image':bad['images']['official']='other@sha256:'+'0'*64
                elif mutation=='verification':bad['source_git_tree_verified']=False
                elif mutation=='snapshot':bad['snapshot_sha256']='0'*64
                else:bad['execution_commit']=original
                save(bad)
                with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                    prepare(work,original,'tasks/proof',template)
            save(status);(snapshot/'tests/data').write_text('changed')
            with self.assertRaises(ValueError):prepare(work,original,'tasks/proof',template)


if __name__=='__main__':unittest.main()
