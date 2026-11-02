import asyncio
import os
from copy import deepcopy
from pathlib import Path
import base64
import fnmatch
import json
import shlex
import stat
import tempfile
from harbor.environments.ack import ACKEnvironment

class UniverseACK(ACKEnvironment):
    def __init__(self, *, runtime_image, verifier_image, environment_dir, pod_overrides=None, **kwargs):
        overrides = deepcopy(pod_overrides or {})
        from acs_scheduling import enforce
        overrides['spec'] = enforce(overrides.get('spec', {}))
        role = 'verifier' if Path(environment_dir).name == 'tests' else 'runtime'
        self._universe_role = role
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
        if self._universe_role == 'runtime' and getattr(self, 'trial_paths', None):
            self._agent_checkpoint_task = asyncio.create_task(self._checkpoint_agent_logs())

    async def _checkpoint_agent_logs(self):
        while True:
            await asyncio.sleep(60)
            if getattr(self, '_agent_evidence_downloaded', False):
                return
            await self._retain_incomplete_agent_evidence(checkpoint=True)

    async def _download_agent_evidence(self, target):
        from acs_evidence_transport import download_agent
        if not hasattr(self, '_agent_collection_lock'):
            self._agent_collection_lock = asyncio.Lock()
        async with self._agent_collection_lock:
            return await download_agent(self, target)

    async def stop(self, delete=True):
        try:
            checkpoint = getattr(self, '_agent_checkpoint_task', None)
            if checkpoint is not None:
                checkpoint.cancel()
                try:
                    await checkpoint
                except asyncio.CancelledError:
                    pass
            await self._capture_pod_evidence('before_stop')
            await self._retain_incomplete_agent_evidence()
            await self._retain_incomplete_verifier_evidence()
        finally:
            await super().stop(delete=delete)

    async def _retain_incomplete_agent_evidence(self, checkpoint=False):
        paths = getattr(self, 'trial_paths', None)
        target = getattr(paths, 'agent_dir', None)
        if (target is None or getattr(self, '_core_api', None) is None
                or getattr(self, '_universe_role', 'runtime') != 'runtime'):
            return
        if not checkpoint and getattr(self, '_agent_evidence_downloaded', False):
            return
        try:
            result = await asyncio.wait_for(self._download_agent_evidence(target), timeout=90)
            record = dict(result, status='checkpoint' if checkpoint else 'collected')
        except Exception as error:
            record = dict(status='collection_failed', error_type=type(error).__name__)
            if hasattr(self, 'logger'):
                self.logger.warning('Partial agent evidence collection (%s); cleanup continues', type(error).__name__)
        # The original files remain private. A failed later collection does not
        # delete the last successfully retained checkpoint.
        try:
            from datetime import datetime, timezone
            from acs_evidence_transport import safe_destination
            path = safe_destination(Path(target) / 'acs-collection.json')
            record['observed_at'] = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps(record)); path.chmod(0o600)
        except (OSError, ValueError):
            pass

    async def _retain_incomplete_verifier_evidence(self):
        # Harbor downloads verifier output only after verify() returns. A
        # timeout cancels that path, so retain partial diagnostics before the
        # owned Pod disappears. Never turn this best-effort copy into a score.
        paths=getattr(self,'trial_paths',None)
        if paths is None or getattr(self,'_core_api',None) is None:
            return
        target=paths.verifier_dir
        if getattr(self,'_verifier_evidence_downloaded',False) or (target/'test-stdout.txt').is_file():
            return
        try:
            await asyncio.wait_for(self.download_dir_filtered('/logs/verifier',target),timeout=40)
        except Exception as error:
            # Transfer failure must not prevent cleanup. A missing reward is
            # diagnosed by Harbor's verifier after available logs are saved.
            if hasattr(self,'logger'):
                self.logger.warning('Partial verifier evidence collection (%s); cleanup continues',type(error).__name__)

    def _get_image_url(self):
        return self._universe_image

    async def download_file(self, source_path, target_path):
        # ACK's tar transport can return successfully without a matching file.
        # Stage into an empty directory so an old destination cannot certify a
        # failed transfer; only publish a newly downloaded regular file.
        target = Path(target_path)
        def validate_destination():
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                raise ValueError('Symlink in file download destination')
            if target.exists() and not target.is_file():
                raise ValueError('File download destination is not a regular file')
        validate_destination()
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.acs-download-', dir=target.parent) as staging:
            downloaded = Path(staging) / target.name
            from acs_evidence_transport import download_file
            await asyncio.wait_for(download_file(self, source_path, downloaded), timeout=120)
            try:
                mode = downloaded.lstat().st_mode
            except FileNotFoundError:
                raise FileNotFoundError('ACK download produced no file for ' + str(source_path)) from None
            if not stat.S_ISREG(mode):
                raise ValueError('ACK download did not produce a regular file for ' + str(source_path))
            validate_destination()
            downloaded.chmod(0o600)
            downloaded.replace(target)

    async def download_dir(self, source_dir, target_dir):
        if str(source_dir) == '/logs/agent':
            await asyncio.wait_for(self._download_agent_evidence(target_dir), timeout=90)
            self._agent_evidence_downloaded = True
            return
        if str(source_dir) == '/logs/verifier':
            return await self.download_dir_filtered(source_dir, target_dir)
        return await super().download_dir(source_dir, target_dir)

    async def download_dir_filtered(self, source_dir, target_dir, *, include=None, exclude=None, protect=None):
        if str(source_dir) == '/logs/agent':
            # Raw solver evidence is mandatory even if a caller filters the
            # display logs. Access control and redaction happen on publication.
            return await self.download_dir(source_dir, target_dir)
        # Use text-safe transport for all verifier evidence, not just reward.
        if str(source_dir) == '/logs/verifier':
            script = '''import pathlib,json,base64
root=pathlib.Path('/logs/verifier'); files={}; total=0
for p in sorted(root.rglob('*')):
 if p.is_symlink(): raise ValueError('Symlink in verifier evidence')
 if not p.is_file(): continue
 if total+p.stat().st_size>16*1024*1024: raise ValueError('Verifier evidence exceeds transfer limit')
 data=p.read_bytes(); total+=len(data)
 if total>16*1024*1024: raise ValueError('Verifier evidence exceeds transfer limit')
 files[p.relative_to(root).as_posix()]=base64.b64encode(data).decode('ascii')
print(json.dumps(files))'''
            result = await self.exec(command='python3 -I -c ' + shlex.quote(script),timeout_sec=30)
            if result.return_code != 0:
                raise ValueError('Unable to collect complete verifier evidence')
            files = json.loads(result.stdout)
            target = Path(target_dir)
            if not isinstance(files, dict):
                raise ValueError('Invalid verifier evidence payload')
            if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                raise ValueError('Symlink in evidence destination')
            # Validate the entire payload before writing any file. A later
            # malicious path, collision or oversized file must not leave a
            # partially accepted evidence bundle or write outside the target.
            decoded = {}
            total = 0
            for name, encoded in files.items():
                if not isinstance(name, str) or not isinstance(encoded, str):
                    raise ValueError('Invalid verifier evidence entry')
                rel = Path(name)
                if rel.is_absolute() or '..' in rel.parts or not rel.parts or str(rel) != name:
                    raise ValueError('Unsafe verifier evidence path')
                dest = target / rel
                if dest.is_symlink() or any(p.is_symlink() for p in dest.parents):
                    raise ValueError('Symlink in evidence destination')
                if (dest.exists() and not dest.is_file()) or any(p.exists() and not p.is_dir() for p in dest.parents):
                    raise ValueError('Conflicting verifier evidence path')
                if len(encoded) > 4 * ((16 * 1024 * 1024 + 2) // 3):
                    raise ValueError('Verifier evidence exceeds transfer limit')
                data = base64.b64decode(encoded, validate=True)
                total += len(data)
                if total > 16 * 1024 * 1024:
                    raise ValueError('Verifier evidence exceeds transfer limit')
                decoded[rel] = data
            if any(parent in decoded for rel in decoded for parent in rel.parents):
                raise ValueError('Conflicting verifier evidence paths')
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            for rel, data in decoded.items():
                name = str(rel)
                selected = (not include or any(fnmatch.fnmatch(name, p) for p in include))
                selected = selected and not any(fnmatch.fnmatch(name, p) for p in (exclude or []))
                if not selected and name not in (protect or []) and name != 'reward.txt':
                    continue
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                dest.write_bytes(data)
                dest.chmod(0o600)
            self._verifier_evidence_downloaded=True
            # Timeouts and verifier crashes often never write reward.txt. Keep
            # their stdout/stderr before reporting that scoring is unavailable.
            # An invalid reward is retained verbatim for diagnosis, never fixed.
            raw_reward = decoded.get(Path('reward.txt'))
            if raw_reward is None:
                # The directory transfer succeeded. Let Harbor report its
                # native RewardFileNotFoundError (or parse reward.json), rather
                # than wrapping a task's missing output as a download failure.
                return
            try:
                reward = raw_reward.decode().strip()
            except UnicodeDecodeError:
                raise ValueError('Invalid verifier reward; available diagnostics retained') from None
            if reward not in {'0', '1', '0.0', '1.0'}:
                raise ValueError('Invalid verifier reward; available diagnostics retained')
            return
        return await super().download_dir_filtered(source_dir, target_dir, include=include, exclude=exclude, protect=protect)
