#!/usr/bin/env python3
"""Operator build/publish of commit-frozen Harbor images, with source receipts.

This is build provenance, never a scientific acceptance or an oracle-leak audit.
Dockerfile code runs only in the configured Docker build service, not host shell.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
from dockerfile_bases import pin_bases, official_library_transport

from evaluation_queue import W, atomic_json, digest_tree
from register_evaluation import freeze
from deployment_paths import allowed_development_path, universe_root

REGISTRY='dptech-sh-pai-acr-registry.cn-shanghai.cr.aliyuncs.com/terminal-bench-universe'
PINNED=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$')
HARNESS={'mini-swe-agent':'2.2.8','litellm':'1.106.2','openai':'2.54.0','mcp':'2.3.0'}


def validate_dockerfile(path,python_base):
    stages=set();bases=[]
    source=Path(path).read_text()
    if re.search(r'^\s*#\s*escape\s*=',source,re.I|re.M):
        raise ValueError('Custom Dockerfile escape directives require separate review')
    source=re.sub(r'\\\r?\n',' ',source)
    for line in source.splitlines():
        if re.match(r'^\s*(COPY|ADD)\s',line,re.I):
            for reference in re.findall(r'--from=(\S+)',line):
                if reference.lower() not in stages and not (reference.isdigit() and int(reference)<len(bases)) and not PINNED.fullmatch(reference):
                    raise ValueError('External COPY/ADD source must use a digest')
        if not re.match(r'^\s*FROM\s',line,re.I):continue
        match=re.fullmatch(r'\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+([A-Za-z0-9_.-]+))?\s*',line,re.I)
        if not match:raise ValueError('Unsupported FROM syntax; supply a reviewed explicit base')
        image=match[1].replace('${PYTHON_BASE}',python_base).replace('$PYTHON_BASE',python_base)
        if image!='scratch' and image.lower() not in stages and not PINNED.fullmatch(image):raise ValueError('Every external FROM must use a digest or the explicit pinned PYTHON_BASE')
        bases.append(image)
        if match[2]:stages.add(match[2].lower())
    if not bases:raise ValueError('Dockerfile requires FROM')
    return bases


def validate_context(directory):
    directory=Path(directory);sha=digest_tree(directory)
    for path in directory.rglob('*'):
        if path.is_file():
            if path.name in {'.env','kubeconfig','id_rsa','id_ed25519'} or '.git' in path.relative_to(directory).parts or path.relative_to(directory).as_posix()=='.docker/config.json':
                raise ValueError('Known credential/config path refused in build context')
            if b'-----BEGIN PRIVATE KEY-----' in path.read_bytes() or b'-----BEGIN OPENSSH PRIVATE KEY-----' in path.read_bytes():
                raise ValueError('Private key marker refused in build context')
    return sha


def receipt_labels(commit,snapshot_hash,context_hash,role):
    return {'org.opencontainers.image.revision':commit,'science.task.snapshot':snapshot_hash,'science.build.context':context_hash,'science.image.role':role}


def validate_build_receipt(receipt,snapshot,commit,task_path,template):
    snapshot=Path(snapshot);snapshot_hash=digest_tree(snapshot)
    if receipt.get('source_commit')!=commit or receipt.get('source_task_path')!=task_path or receipt.get('task_name')!=Path(task_path).name or receipt.get('snapshot_sha256')!=snapshot_hash:
        raise ValueError('Build receipt does not match frozen author commit/task')
    if receipt.get('published') is not True or receipt.get('registry_config_linkage_verified') is not True or receipt.get('scientific_acceptance') is not False:
        raise ValueError('Require published source receipt, not scientific acceptance')
    if receipt.get('environment_context_sha256')!=digest_tree(snapshot/'environment') or receipt.get('tests_context_sha256')!=digest_tree(snapshot/'tests'):
        raise ValueError('Build contexts differ from frozen task')
    if receipt.get('harness','mini-swe-agent')=='codex':
        versions=receipt.get('harness_versions',{})
        if set(versions)!={'codex','rg'} or any(not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',v) for v in versions.values()):raise ValueError('Codex harness version receipt mismatch')
    elif receipt.get('harness_versions')!=HARNESS:
        raise ValueError('Harness version receipt mismatch')
    images=receipt['images'];expected_prefix=REGISTRY+'/'+Path(task_path).name
    for role,context_hash in [('official',receipt['environment_context_sha256']),('runtime',receipt['runtime_adapter_context_sha256']),('verifier',receipt['verifier_effective_context_sha256'])]:
        image=images[role];reference=image.get('published_digest','')
        repository=expected_prefix+('-verifier' if role=='verifier' else '')
        if not re.fullmatch(re.escape(repository)+r'@sha256:[a-f0-9]{64}',reference) or not re.fullmatch(r'sha256:[a-f0-9]{64}',image.get('image_config_id','')):
            raise ValueError('Unexpected published project image/config ID')
        if image.get('labels')!=receipt_labels(commit,snapshot_hash,context_hash,role):
            raise ValueError('Receipt image provenance labels mismatch')
    environment=template['environment']['kwargs']
    for role in ['runtime','verifier']:
        if environment.get(role+'_image')!=images[role]['published_digest']:
            raise ValueError('Evaluation image is not the source-bound published image')
    overrides=environment.get('pod_overrides',{}).get('spec',{}).get('containers',[])
    if len(overrides)!=1 or overrides[0].get('image')!=images['runtime']['published_digest']:
        raise ValueError('Pod image override conflicts with source-bound runtime')
    return snapshot_hash


class Docker:
    def __init__(self,output):
        self.output=output;self.step=0
        runtime=universe_root()
        self.env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(runtime/'.state/harbor-home'),'DOCKER_CONFIG':str(W/'.state/docker-config-shanghai-public'),'TMPDIR':str(runtime/'.tmp')}
        for name in ['DOCKER_HOST','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','HTTP_PROXY','HTTPS_PROXY','NO_PROXY','http_proxy','https_proxy','no_proxy']:
            if name in os.environ:self.env[name]=os.environ[name]
    def run(self,args,json_output=False):
        self.step+=1
        # Logs stay root-private; do not print daemon output or registry errors.
        path=self.output/f'docker-{self.step:02d}.log'
        if not json_output:
            descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(descriptor,'wb') as log:
                result=subprocess.run(['docker',*args],env=self.env,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
            if result.returncode:raise ValueError(f'Docker step {self.step} failed; inspect private build log')
            return path.read_text(errors='replace')
        result=subprocess.run(['docker',*args],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=1200)
        path.write_bytes(result.stdout+b'\n'+result.stderr);path.chmod(0o600)
        if result.returncode:raise ValueError(f'Docker step {self.step} failed; inspect private build log')
        return json.loads(result.stdout) if json_output else result.stdout.decode()


def publish_image(docker,tag,image_id):
    docker.run(['push',tag])
    image=docker.run(['image','inspect',tag],True)[0]
    if image['Id']!=image_id:raise ValueError('Local image changed during publication')
    repository=tag.rsplit(':',1)[0]
    refs=[ref for ref in image.get('RepoDigests',[]) if ref.startswith(repository+'@sha256:')]
    if len(refs)!=1:raise ValueError('Require one unambiguous published image digest')
    manifest=docker.run(['manifest','inspect',refs[0]],True)
    if manifest.get('config',{}).get('digest')!=image_id:raise ValueError('Registry manifest configuration differs from built image')
    return refs[0]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ['repo','commit','task-path','output','python-base']:parser.add_argument('--'+flag,required=True)
    parser.add_argument('--harness',choices=['codex','mini-swe-agent'],default='codex')
    parser.add_argument('--codex-binary')
    parser.add_argument('--rg-binary')
    parser.add_argument('--publish',action='store_true',help='Push project Shanghai ACR images and verify manifest/config linkage')
    args=parser.parse_args();output=Path(args.output)
    if os.geteuid()!=0 or not allowed_development_path(output) or output.exists() or not PINNED.fullmatch(args.python_base):raise ValueError('Require operator, fresh workspace output and pinned base image')
    task_name=Path(args.task_path).name
    if not re.fullmatch('[a-z0-9][a-z0-9._-]{0,90}',task_name):raise ValueError('Task directory must already have a lowercase registry-compatible ID')
    state=W/'.state/science-image-builds';state.mkdir(mode=0o700,exist_ok=True)
    with (state/'builder.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        output.mkdir(mode=0o700)
        snapshot=output/'task';task_hash=freeze(args.repo,args.commit,args.task_path,snapshot)
        env_hash=validate_context(snapshot/'environment');tests_hash=validate_context(snapshot/'tests')
        docker=Docker(output)
        def resolve_base(image):
            transport=official_library_transport(image,os.environ.get('GEWU_DOCKER_LIBRARY_MIRROR'))
            docker.run(['pull',transport])
            info=docker.run(['image','inspect',transport],True)[0]
            refs=info.get('RepoDigests',[])
            if len(refs)!=1 or not PINNED.fullmatch(refs[0]):
                raise ValueError('Base image must resolve to one immutable repository digest')
            return refs[0]
        effective_environment=output/'environment-effective'
        shutil.copytree(snapshot/'environment',effective_environment)
        base_resolution=pin_bases(effective_environment/'Dockerfile',args.python_base,resolve_base)
        bases=validate_dockerfile(effective_environment/'Dockerfile',args.python_base)
        effective_tests=output/'tests-effective'
        if (snapshot/'tests/Dockerfile').exists():
            shutil.copytree(snapshot/'tests',effective_tests)
            base_resolution+=pin_bases(effective_tests/'Dockerfile',args.python_base,resolve_base)
            verifier_bases=validate_dockerfile(effective_tests/'Dockerfile',args.python_base)
        else:verifier_bases=[]
        identity={'source_commit':args.commit,'source_task_path':args.task_path,'task_name':task_name,'snapshot_sha256':task_hash,'environment_context_sha256':env_hash,'tests_context_sha256':tests_hash,'python_base':args.python_base,'runtime_source_bases':bases,'verifier_source_bases':verifier_bases,'scientific_acceptance':False,'oracle_leakage_review_required':True,'hermetic_build':False,'published':False}
        atomic_json(output/'identity.json',identity)
        identity['base_resolution']=base_resolution
        identity['official_library_mirror']=os.environ.get('GEWU_DOCKER_LIBRARY_MIRROR')
        identity['environment_effective_context_sha256']=digest_tree(effective_environment)
        atomic_json(output/'identity.json',identity)
        images={}
        def build(role,context,tag,context_hash,extra=()):
            labels=receipt_labels(args.commit,task_hash,context_hash,role)
            command=['build','--force-rm','--build-arg','PYTHON_BASE='+args.python_base,'-t',tag]
            for key,value in labels.items():command.extend(['--label',key+'='+value])
            command.extend(extra);command.append(str(context));docker.run(command)
            image=docker.run(['image','inspect',tag],True)[0]
            if any(image['Config'].get('Labels',{}).get(key)!=value for key,value in labels.items()):raise ValueError('Built image provenance labels mismatch')
            images[role]={'tag':tag,'image_config_id':image['Id'],'labels':labels,'published_digest':None}
            if args.publish:images[role]['published_digest']=publish_image(docker,tag,image['Id'])
            atomic_json(output/'images-in-progress.json',images)
            return images[role]['published_digest'] or image['Id']
        official=build('official',effective_environment,REGISTRY+'/'+task_name+':official',env_hash)
        runtime=output/'runtime'
        codex_metadata=None
        if args.harness=='codex':
            if not args.codex_binary or not args.rg_binary:raise ValueError('Codex requires explicit workspace native binaries')
            from codex_runtime_context import prepare
            codex_metadata=prepare(runtime,official,args.codex_binary,args.rg_binary)
        else:runtime.mkdir()
        harness_base=os.environ.get('UE_HARNESS_BASE_IMAGE')
        if harness_base and not PINNED.fullmatch(harness_base):
            raise ValueError('UE_HARNESS_BASE_IMAGE must be digest-pinned')
        if harness_base:
            dockerfile=('FROM '+harness_base+' AS harness\nFROM '+official+'\n'
                        'COPY --from=harness /usr/local/bin/ /opt/harness-bin/\n'
                        'COPY --from=harness /usr/local/lib/python3.11/site-packages/ /opt/harness-site/\n'
                        'COPY --from=harness /usr/local/bin/mini-swe-agent /usr/local/bin/mini-swe-agent\n'
                        'RUN cp -a /opt/harness-site/. /usr/local/lib/python3.12/site-packages/ 2>/dev/null || cp -a /opt/harness-site/. /usr/local/lib/python3.11/site-packages/\n'
                        'ENV PATH=/usr/local/bin:$PATH LITELLM_LOCAL_MODEL_COST_MAP=True\n'
                        'RUN python3 -c "from minisweagent.models.litellm_model import LitellmModel"\n')
        else:
            requirements=' '.join("'"+name+'=='+version+"'" for name,version in HARNESS.items())
            dockerfile='FROM '+official+'\nENV PIP_NO_CACHE_DIR=1 LITELLM_LOCAL_MODEL_COST_MAP=True\nRUN python3 -m pip install --timeout 30 --retries 1 '+requirements+'\nRUN python3 -c "from minisweagent.models.litellm_model import LitellmModel"\n'
        if args.harness!='codex':(runtime/'Dockerfile').write_text(dockerfile)
        runtime_hash=validate_context(runtime)
        build('runtime',runtime,REGISTRY+'/'+task_name+':'+(codex_metadata['runtime_tag'] if codex_metadata else 'minisweagent-2.2.8'),runtime_hash)
        if (snapshot/'tests/Dockerfile').exists():
            verifier_context=effective_tests
        else:
            verifier_context=output/'verifier';verifier_context.mkdir();(verifier_context/'Dockerfile').write_text('FROM '+official+'\n')
        verifier_hash=validate_context(verifier_context)
        build('verifier',verifier_context,REGISTRY+'/'+task_name+'-verifier:official',verifier_hash)
        if digest_tree(snapshot)!=task_hash or digest_tree(snapshot/'environment')!=env_hash or digest_tree(snapshot/'tests')!=tests_hash:raise ValueError('Frozen source changed during image build')
        atomic_json(output/'receipt.json',{**identity,'images':images,'runtime_adapter_context_sha256':runtime_hash,'verifier_effective_context_sha256':verifier_hash,'harness_versions':(codex_metadata['harness_versions'] if codex_metadata else HARNESS),'harness':args.harness,'native_binaries':(codex_metadata['native_binaries'] if codex_metadata else {}),'published':args.publish,'registry_config_linkage_verified':args.publish,'standard_tests_uploaded_by_harbor':not (snapshot/'tests/Dockerfile').exists(),'scientific_acceptance':False})
        print(json.dumps({'source_commit':args.commit,'task_name':task_name,'published':args.publish,'receipt_complete':True,'scientific_acceptance':False}))


if __name__=='__main__':main()
