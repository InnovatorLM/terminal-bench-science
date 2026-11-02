"""Offline source/image binding checks, no Docker/ACR/ACS calls."""
import json
import copy
from pathlib import Path
import tempfile
import unittest
from evaluation_queue import W
from build_task_images import validate_context,validate_dockerfile,receipt_labels,publish_image,validate_build_receipt,REGISTRY,HARNESS,select_base_digest,resolve_base_image,BaseImageResolutionError,build_failure_details

BASE='public.ecr.aws/docker/library/python@sha256:'+'a'*64


class BuildTests(unittest.TestCase):
    def test_base_resolution_uses_requested_repository_among_aliases(self):
        digest='sha256:'+'a'*64
        self.assertEqual(select_base_digest('public.ecr.aws/docker/library/python:3.12-slim',
            {'RepoDigests':['other.example/python@sha256:'+'b'*64,'public.ecr.aws/docker/library/python@'+digest]}),
            'public.ecr.aws/docker/library/python@'+digest)
        self.assertEqual(select_base_digest('localhost:5000/team/python:tag',
            {'RepoDigests':['localhost:5000/team/python@'+digest]}),'localhost:5000/team/python@'+digest)
        self.assertEqual(select_base_digest('python:3.12',{'RepoDigests':['python@'+digest]}),'docker.io/library/python@'+digest)

    def test_this_pull_selects_among_multiple_digests_for_same_repository(self):
        repo='public.ecr.aws/docker/library/python';old='sha256:'+'1'*64;current='sha256:'+'a'*64
        info={'RepoDigests':[repo+'@'+old,repo+'@'+current]}
        self.assertEqual(select_base_digest(repo+':3.12-slim',info,'Digest: '+current+'\n'),repo+'@'+current)
        with self.assertRaisesRegex(BaseImageResolutionError,'ambiguous'):
            select_base_digest(repo+':3.12-slim',info)
        with self.assertRaisesRegex(BaseImageResolutionError,'absent'):
            select_base_digest(repo+':3.12-slim',info,'Digest: sha256:'+'c'*64+'\n')

    def test_explicit_digest_and_pull_must_agree_exactly(self):
        repo='registry.example/python';a='sha256:'+'a'*64;b='sha256:'+'b'*64;info={'RepoDigests':[repo+'@'+a,repo+'@'+b]}
        self.assertEqual(select_base_digest(repo+'@'+a,info,'Digest: '+a+'\n'),repo+'@'+a)
        with self.assertRaisesRegex(BaseImageResolutionError,'differs from explicit'):
            select_base_digest(repo+'@'+a,info,'Digest: '+b+'\n')
        with self.assertRaisesRegex(BaseImageResolutionError,'conflicting'):
            select_base_digest(repo+':tag',info,'Digest: '+a+'\nDigest: '+b+'\n')
        with self.assertRaisesRegex(BaseImageResolutionError,'absent'):
            select_base_digest('other.example/python@'+a,info,'Digest: '+a+'\n')

    def test_resolver_failure_has_operator_owned_platform_classification(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            class FakeDocker:
                output=Path(raw)
                def run(self,args,json_output=False):
                    if args[0]=='pull':return 'Digest: sha256:'+'a'*64+'\n'
                    return [{'RepoDigests':['wrong.example/python@sha256:'+'a'*64]}]
            with self.assertRaises(BaseImageResolutionError):resolve_base_image(FakeDocker(),'python:3.12')
            details=build_failure_details(Path(raw))
            self.assertEqual(details['failure_owner'],'platform')
            self.assertFalse(details['retry_requires_task_change'])
            self.assertEqual((Path(raw)/'failure.json').stat().st_mode&0o777,0o600)

    def test_registration_requires_matching_publication(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as temporary:
            snapshot=Path(temporary)
            for name in ['environment','tests']:
                (snapshot/name).mkdir();(snapshot/name/'data').write_text(name)
            sha=validate_context(snapshot);commit='e'*40
            receipt={'source_commit':commit,'source_task_path':'tasks/proof','task_name':'proof','snapshot_sha256':sha,'published':True,'registry_config_linkage_verified':True,'scientific_acceptance':False,'harness_versions':HARNESS,'environment_context_sha256':validate_context(snapshot/'environment'),'tests_context_sha256':validate_context(snapshot/'tests'),'runtime_adapter_context_sha256':'a'*64,'verifier_effective_context_sha256':'b'*64,'images':{}}
            for role,context in [('official',receipt['environment_context_sha256']),('runtime','a'*64),('verifier','b'*64)]:
                receipt['images'][role]={'published_digest':REGISTRY+'/proof'+('-verifier' if role=='verifier' else '')+'@sha256:'+'c'*64,'image_config_id':'sha256:'+'d'*64,'labels':receipt_labels(commit,sha,context,role)}
            image=receipt['images']['runtime']['published_digest']
            template={'environment':{'kwargs':{'runtime_image':image,'verifier_image':receipt['images']['verifier']['published_digest'],'pod_overrides':{'spec':{'containers':[{'image':image}]}}}}}
            self.assertEqual(validate_build_receipt(receipt,snapshot,commit,'tasks/proof',template),sha)
            for key,value in [('source_commit','f'*40),('snapshot_sha256','0'*64),('environment_context_sha256','0'*64),('published',False)]:
                bad=copy.deepcopy(receipt);bad[key]=value
                with self.subTest(field=key),self.assertRaises(ValueError):validate_build_receipt(bad,snapshot,commit,'tasks/proof',template)
            for mutate in ['repository','labels','pod']:
                bad=copy.deepcopy(receipt);config=copy.deepcopy(template)
                if mutate=='repository':bad['images']['runtime']['published_digest']='foreign/task@sha256:'+'c'*64
                elif mutate=='labels':bad['images']['official']['labels']['science.build.context']='0'*64
                else:config['environment']['kwargs']['pod_overrides']['spec']['containers'][0]['image']='foreign:latest'
                with self.subTest(field=mutate),self.assertRaises(ValueError):validate_build_receipt(bad,snapshot,commit,'tasks/proof',config)

    def test_context_and_pinned_stages(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as temporary:
            context=Path(temporary);dockerfile=context/'Dockerfile'
            dockerfile.write_text('ARG PYTHON_BASE\nFROM ${PYTHON_BASE} AS base\nFROM base\n')
            self.assertEqual(validate_dockerfile(dockerfile,BASE),[BASE,'base'])
            before=validate_context(context);(context/'data.json').write_text('{"input":1}')
            self.assertNotEqual(validate_context(context),before)
            (context/'config.json').write_text('{"scientific_parameters":[]}')
            validate_context(context)
            for source in ['FROM python:3.11-slim\n','FROM ${UNRESOLVED}\n','FROM \\\npython:latest AS hidden\nFROM '+BASE+'\n','FROM '+BASE+'\nCOPY --from=python:latest /x /x\n','# escape=`\nFROM '+BASE+'\n']:
                dockerfile.write_text(source)
                with self.assertRaises(ValueError):validate_dockerfile(dockerfile,BASE)
            dockerfile.write_text('FROM '+BASE+'\n')
            (context/'leaked.env').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):validate_context(context)
            (context/'leaked.env').unlink();(context/'kubeconfig').write_text('fixture')
            with self.assertRaises(ValueError):validate_context(context)

    def test_registry_configuration_must_match_build(self):
        repository='registry.example/task';tag=repository+':official';image_id='sha256:'+'b'*64;digest=repository+'@sha256:'+'c'*64
        class FakeDocker:
            mismatch=False
            def run(self,args,json_output=False):
                if args[0]=='push':return ''
                if args[0]=='image':return [{'Id':image_id,'RepoDigests':[digest]}]
                return {'config':{'digest':'sha256:'+'d'*64 if self.mismatch else image_id}}
        docker=FakeDocker();self.assertEqual(publish_image(docker,tag,image_id),digest)
        docker.mismatch=True
        with self.assertRaises(ValueError):publish_image(docker,tag,image_id)
        labels=receipt_labels('e'*40,'f'*64,'a'*64,'runtime')
        self.assertEqual(labels['org.opencontainers.image.revision'],'e'*40)
        self.assertNotEqual(labels,receipt_labels('e'*40,'f'*64,'b'*64,'runtime'))


if __name__=='__main__':unittest.main()
