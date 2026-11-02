import asyncio
import os
from copy import deepcopy
from pathlib import Path
import base64
import fnmatch
import json
import shlex
from harbor.environments.ack import ACKEnvironment

class UniverseACK(ACKEnvironment):
    def __init__(self, *, runtime_image, verifier_image, environment_dir, pod_overrides=None, **kwargs):
        overrides = deepcopy(pod_overrides or {})
        from acs_scheduling import enforce
        overrides['spec'] = enforce(overrides.get('spec', {}))
        role = 'verifier' if Path(environment_dir).name == 'tests' else 'runtime'
        image = verifier_image if role == 'verifier' else runtime_image
        import re
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}',image):
            raise ValueError('Explicit immutable image digest required')
        overrides.setdefault('spec', {}).setdefault('containers', [{'name': 'main'}])[0]['image'] = image
        overrides.setdefault('metadata', {}).setdefault('labels', {})['universe-role'] = role
        self._universe_image = image
        super().__init__(environment_dir=environment_dir, pod_overrides=overrides, **kwargs)

    def _validate_definition(self):
        # This adapter only executes an operator-published immutable image.
        # Requiring a local Dockerfile (especially tests/Dockerfile) would
        # prevent a separate verifier from starting despite its bound image.
        if not self.environment_dir.is_dir():
            raise FileNotFoundError('Task environment directory is missing')
        if not getattr(self,'_universe_image',None):
            raise ValueError('Published image identity is required')

    async def _capture_pod_evidence(self, stage):
        directory=os.environ.get('GEWU_POD_EVIDENCE_DIR')
        if not directory or self._core_api is None:
            return
        try:
            pod=await asyncio.to_thread(self._core_api.read_namespaced_pod,
                name=self.pod_name,namespace=self.namespace,_request_timeout=(2,5))
            from kubernetes.client import ApiClient
            from acs_helpers.pod_evidence import capture
            capture(ApiClient().sanitize_for_serialization(pod),directory,stage)
        except Exception as error:
            # Cleanup must proceed even if Kubernetes cannot return evidence.
            # The guard marks missing observations incomplete, never fabricated.
            self.logger.warning('Pod evidence capture failed (%s)',type(error).__name__) if hasattr(self,'logger') else None

    async def start(self, force_build=False):
        await super().start(force_build=force_build)
        await self._capture_pod_evidence('started')

    async def stop(self, delete=True):
        try:
            await self._capture_pod_evidence('before_stop')
        finally:
            await super().stop(delete=delete)

    def _get_image_url(self):
        return self._universe_image

    async def download_dir(self, source_dir, target_dir):
        if str(source_dir) == '/logs/verifier':
            return await self.download_dir_filtered(source_dir, target_dir)
        return await super().download_dir(source_dir, target_dir)

    async def download_dir_filtered(self, source_dir, target_dir, *, include=None, exclude=None, protect=None):
        # Use text-safe transport for all verifier evidence, not just reward.
        if str(source_dir) == '/logs/verifier':
            script = '''import pathlib,json,base64
root=pathlib.Path('/logs/verifier'); files={}; total=0
for p in sorted(root.rglob('*')):
 if p.is_symlink(): raise ValueError('Symlink in verifier evidence')
 if not p.is_file(): continue
 data=p.read_bytes(); total+=len(data)
 if total>16*1024*1024: raise ValueError('Verifier evidence exceeds transfer limit')
 files[p.relative_to(root).as_posix()]=base64.b64encode(data).decode('ascii')
print(json.dumps(files))'''
            result = await self.exec(command='python3 -I -c ' + shlex.quote(script))
            if result.return_code != 0:
                raise ValueError('Unable to collect complete verifier evidence')
            files = json.loads(result.stdout)
            target = Path(target_dir)
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            reward = base64.b64decode(files.get('reward.txt', ''), validate=True).decode().strip()
            if reward not in {'0', '1', '0.0', '1.0'}:
                raise ValueError('Missing or invalid verifier reward')
            for name, encoded in files.items():
                rel = Path(name)
                if rel.is_absolute() or '..' in rel.parts or not rel.parts:
                    raise ValueError('Unsafe verifier evidence path')
                selected = (not include or any(fnmatch.fnmatch(name, p) for p in include))
                selected = selected and not any(fnmatch.fnmatch(name, p) for p in (exclude or []))
                if not selected and name not in (protect or []) and name != 'reward.txt':
                    continue
                dest = target / rel
                if dest.is_symlink() or any(p.is_symlink() for p in dest.parents):
                    raise ValueError('Symlink in evidence destination')
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                dest.write_bytes(base64.b64decode(encoded, validate=True))
                dest.chmod(0o600)
            return
        return await super().download_dir_filtered(source_dir, target_dir, include=include, exclude=exclude, protect=protect)
