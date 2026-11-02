#!/usr/bin/env python3
"""Operator build/publish of commit-frozen Harbor images, with source receipts.

This is build provenance, never a scientific acceptance or an oracle-leak audit.
Dockerfile code runs only in the configured Docker build service, not host shell.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import stat
from dockerfile_bases import pin_bases, official_library_transport

from evaluation_queue import W, atomic_json, digest_tree
from register_evaluation import freeze
from deployment_paths import allowed_development_path, universe_root

REGISTRY='dptech-sh-pai-acr-registry.cn-shanghai.cr.aliyuncs.com/terminal-bench-universe'
PINNED=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$')
HARNESS={'mini-swe-agent':'2.2.8','litellm':'1.106.2','openai':'2.54.0','mcp':'2.3.0'}
BASE_RESOLUTION_FAILURE={
    'error_class':'platform_base_image_resolution',
    'failure_owner':'platform',
    'stage':'base_image_resolution',
    'retry_requires_task_change':False,
    'cloud_execution_started':False,
    'remediation':'Platform must resolve the requested base image from pull/inspect evidence. Keep the submitted task unchanged; do not repeatedly edit or resubmit it to work around this failure.',
}


class BaseImageResolutionError(ValueError):
    """The platform could not bind a requested base to observed pull evidence."""


def image_repository(reference):
    """Normalize Docker's repository aliases without confusing ports and tags."""
    name=reference.split('@',1)[0]
    if ':' in name.rsplit('/',1)[-1]:name=name.rsplit(':',1)[0]
    parts=name.split('/')
    if len(parts)==1 or not ('.' in parts[0] or ':' in parts[0] or parts[0]=='localhost'):
        name='docker.io/'+name
    if name.startswith(('index.docker.io/','registry-1.docker.io/')):
        name='docker.io/'+name.split('/',1)[1]
    if name.startswith('docker.io/') and name.count('/')==1:
        name='docker.io/library/'+name.split('/',1)[1]
    return name


def select_base_digest(requested,info,pull_output=''):
    """Bind this pull, not every historical digest attached to its config ID."""
    repository=image_repository(requested)
    explicit=requested.split('@',1)[1] if '@' in requested else None
    if explicit is not None and not re.fullmatch(r'sha256:[a-f0-9]{64}',explicit):
        raise BaseImageResolutionError('Requested base digest is invalid')
    refs=info.get('RepoDigests',[])
    if not isinstance(refs,list):raise BaseImageResolutionError('Invalid base image inspect digest list')
    matching={ref.split('@',1)[1] for ref in refs if isinstance(ref,str) and PINNED.fullmatch(ref) and image_repository(ref)==repository}
    pulled=set(re.findall(r'^Digest:\s*(sha256:[a-f0-9]{64})\s*$',pull_output,re.M))
    if len(pulled)>1:raise BaseImageResolutionError('Pull reported conflicting base digests')
    observed=next(iter(pulled),None)
    if explicit is not None and observed is not None and explicit!=observed:
        raise BaseImageResolutionError('Pulled base digest differs from explicit requested digest')
    chosen=explicit or observed
    if chosen is None:
        if len(matching)!=1:raise BaseImageResolutionError('Base digest is ambiguous without this pull evidence')
        chosen=next(iter(matching))
    if chosen not in matching:
        raise BaseImageResolutionError('Requested repository/pull digest is absent from inspected image')
    return repository+'@'+chosen


def resolve_base_image(docker,image,mirror=None):
    transport=official_library_transport(image,mirror)
    pulled=docker.run(['pull',transport])
    info=docker.run(['image','inspect',transport],True)[0]
    try:return select_base_digest(transport,info,pulled)
    except BaseImageResolutionError:
        atomic_json(docker.output/'failure.json',BASE_RESOLUTION_FAILURE)
        raise


def build_failure_details(output):
    from evaluation_queue import root_file
    path=Path(output)/'failure.json'
    if not path.exists():return {}
    value=json.loads(root_file(path).read_text())
    if value.get('error_class')==BASE_RESOLUTION_FAILURE['error_class']:
        return dict(BASE_RESOLUTION_FAILURE)
    return {}


@contextlib.contextmanager
def builder_slot(inherited_fd=None):
    """Serialize builds; an inherited descriptor keeps the same locked slot."""
    state=W/'.state/science-image-builds';state.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=state/'builder.lock'
    if inherited_fd is not None:
        info=os.fstat(inherited_fd);expected=path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(expected.st_dev,expected.st_ino):
            raise ValueError('Inherited builder descriptor does not match lock file')
        # A descriptor inherited via pass_fds shares the parent's open file
        # description and lock. A caller with an unlocked descriptor must obtain
        # the lock here; a conflicting owner is never bypassed.
        fcntl.flock(inherited_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield inherited_fd
        return
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield lock.fileno()


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
    parser.add_argument('--lock-fd',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--publish',action='store_true',help='Push project Shanghai ACR images and verify manifest/config linkage')
    args=parser.parse_args();output=Path(args.output)
    if os.geteuid()!=0 or not allowed_development_path(output) or output.exists() or not PINNED.fullmatch(args.python_base):raise ValueError('Require operator, fresh workspace output and pinned base image')
    task_name=Path(args.task_path).name
    if not re.fullmatch('[a-z0-9][a-z0-9._-]{0,90}',task_name):raise ValueError('Task directory must already have a lowercase registry-compatible ID')
    with builder_slot(args.lock_fd):
        output.mkdir(mode=0o700)
        snapshot=output/'task';task_hash=freeze(args.repo,args.commit,args.task_path,snapshot)
        env_hash=validate_context(snapshot/'environment');tests_hash=validate_context(snapshot/'tests')
        from docker_context_preflight import require_contexts
        require_contexts(snapshot)
        docker=Docker(output)
        def resolve_base(image):
            return resolve_base_image(docker,image,os.environ.get('GEWU_DOCKER_LIBRARY_MIRROR'))
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
