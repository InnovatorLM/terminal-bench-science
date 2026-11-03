"""Offline source/image binding checks, no Docker/ACR/ACS calls."""
import json
import copy
from pathlib import Path
import tempfile
import unittest
import contextlib
import io
import shutil
from unittest.mock import patch
from evaluation_queue import W
from build_task_images import validate_context,validate_dockerfile,receipt_labels,publish_image,validate_build_receipt,REGISTRY,HARNESS,select_base_digest,resolve_base_image,BaseImageResolutionError,build_failure_details
import build_task_images as builder
from docker_context_preflight import require_contexts,DockerContextError

BASE='public.ecr.aws/docker/library/python@sha256:'+'a'*64


class BuildTests(unittest.TestCase):
    def verifier_fixture(self,root,config=''):
        values={'task.toml':'schema_version="1.3"\n'+config,'environment/Dockerfile':'FROM scratch\n',
                'tests/test.sh':'#!/bin/bash\nexit 0\n'}
        for name,value in values.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)

    def test_direct_builder_rejects_missing_verifier_before_docker_creation(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);source=root/'source';output=root/'build'
            self.verifier_fixture(source,'[verifier.environment]\ncpus=2\n')
            def freeze(_repo,_commit,_task,target):
                shutil.copytree(source,target)
                return builder.digest_tree(target)
            argv=['build_task_images.py','--repo',str(root),'--commit','a'*40,'--task-path','task',
                  '--output',str(output),'--python-base',BASE]
            with patch('sys.argv',argv),patch.object(builder,'freeze',side_effect=freeze), \
                 patch.object(builder,'builder_slot',return_value=contextlib.nullcontext()), \
                 patch.object(builder.os,'geteuid',return_value=0),patch.object(builder,'Docker') as docker:
                with self.assertRaises(DockerContextError):builder.main()
            docker.assert_not_called()
            failure=build_failure_details(output)
            self.assertEqual(failure['error_class'],'task_build_context_invalid')
            self.assertEqual(failure['failure_owner'],'task')
            self.assertFalse(failure['cloud_execution_started'])

    def test_effective_verifier_context_respects_declared_image_over_dockerfile(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);task=root/'task';out=root/'output';out.mkdir()
            image='registry.example/verifier:1';pinned='registry.example/verifier@sha256:'+'b'*64
            self.verifier_fixture(task,'[verifier.environment]\ndocker_image="'+image+'"\n')
            (task/'tests/Dockerfile').write_text('FROM scratch\nCOPY absent /never-used\n')
            plan=require_contexts(task)['verifier_build']
            with patch.object(builder,'resolve_base_image',side_effect=AssertionError('no network')):
                seen=[]
                def resolve(ref):seen.append(ref);return pinned
                context,bases,resolution=builder.prepare_verifier_context(task,out,plan,BASE,resolve)
            self.assertEqual(seen,[image])
            self.assertEqual(bases,[pinned])
            self.assertEqual((context/'Dockerfile').read_text(),'FROM '+pinned+'\n')
            self.assertEqual(resolution,[{'source':image,'resolved':pinned}])
            self.assertNotIn('absent',(context/'Dockerfile').read_text())

    def test_effective_verifier_uses_local_dockerfile_or_explicit_inheritance(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);task=root/'task';out=root/'output';out.mkdir()
            self.verifier_fixture(task,'[verifier]\nenvironment_mode="separate"\n')
            inherited=require_contexts(task)['verifier_build']
            with patch.object(builder,'resolve_base_image',side_effect=AssertionError('no network')):
                self.assertEqual(builder.prepare_verifier_context(task,out,inherited,BASE,None),(None,[],[]))
            (task/'tests/Dockerfile').write_text('FROM scratch\nCOPY test.sh /tests/test.sh\n')
            plan=require_contexts(task)['verifier_build']
            context,bases,resolution=builder.prepare_verifier_context(task,out,plan,BASE,None)
            self.assertEqual(bases,['scratch'])
            self.assertEqual(resolution,[])
            self.assertEqual((context/'test.sh').read_bytes(),(task/'tests/test.sh').read_bytes())

    def test_complete_offline_build_records_source_without_claiming_test_upload(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);source=root/'source';output=root/'build'
            image='registry.example/verifier@sha256:'+'b'*64
            self.verifier_fixture(source,'[verifier.environment]\ndocker_image="'+image+'"\n')
            (source/'tests/Dockerfile').write_text('FROM scratch\nCOPY unused-input /unused\n')
            class FakeDocker:
                def __init__(self,out):self.output=out;self.images={}
                def run(self,args,json_output=False):
                    if args[0]=='build':
                        tag=args[args.index('-t')+1]
                        labels=dict(args[i+1].split('=',1) for i,value in enumerate(args) if value=='--label')
                        self.images[tag]={'Id':'sha256:'+'c'*64,'Config':{'Labels':labels}}
                        return ''
                    if args[:2]==['image','inspect']:return [self.images[args[2]]]
                    raise AssertionError('Unexpected Docker or registry operation')
            def freeze(_repo,_commit,_task,target):
                shutil.copytree(source,target);return builder.digest_tree(target)
            argv=['build_task_images.py','--repo',str(root),'--commit','a'*40,'--task-path','task',
                  '--output',str(output),'--python-base',BASE,'--harness','mini-swe-agent']
            with patch('sys.argv',argv),patch.object(builder,'freeze',side_effect=freeze), \
                 patch.object(builder,'builder_slot',return_value=contextlib.nullcontext()), \
                 patch.object(builder.os,'geteuid',return_value=0),patch.object(builder,'Docker',FakeDocker), \
                 contextlib.redirect_stdout(io.StringIO()):
                builder.main()
            receipt=json.loads((output/'receipt.json').read_text())
            self.assertEqual(receipt['verifier_build']['source'],'declared_image')
            self.assertEqual(receipt['verifier_source_bases'],[image])
            self.assertIsNone(receipt['standard_tests_uploaded_by_harbor'])
            self.assertFalse(receipt['tests_materialization_verified'])
            self.assertFalse(receipt['scientific_acceptance'])

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

    def test_pull_failure_is_recorded_before_digest_inspection(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            class FailedPull:
                output=Path(raw)
                step=1
                def run(self,args,json_output=False):
                    if args[0]!='pull':raise AssertionError('Inspection must not run after failed pull')
                    raise ValueError('Docker step 1 failed')
            with self.assertRaises(ValueError):resolve_base_image(FailedPull(),'python:3.13-slim-bookworm',
                'public.ecr.aws/docker/library')
            failure=json.loads((Path(raw)/'failure.json').read_text())
            self.assertEqual(failure['stage'],'base_image_resolution')
            self.assertEqual(failure['transport_image'],'public.ecr.aws/docker/library/python:3.13-slim-bookworm')
            self.assertEqual(failure['failed_docker_step'],1)
            self.assertFalse(failure['cloud_execution_started'])

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
