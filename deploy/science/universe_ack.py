import asyncio
import os
from copy import deepcopy
from pathlib import Path
import json
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
        if self._universe_role == 'verifier' and getattr(self, 'trial_paths', None):
            self._record_verifier_collection(self.trial_paths.verifier_dir,
                dict(status='pending', complete=False, files=0, manifest={}))
        if self._universe_role == 'runtime' and getattr(self, 'trial_paths', None):
            self._record_agent_collection(self.trial_paths.agent_dir,
                dict(status='pending', complete=False, files=0, manifest={}))
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

    async def _final_agent_evidence(self, target):
        # Final collection has a separate infrastructure budget; the model's
        # four-hour budget is unchanged. Retries reuse verified partial bytes.
        while True:
            result = await self._download_agent_evidence(target)
            if result.get('complete', True):
                return result
            await asyncio.sleep(1)

    def _record_agent_collection(self, target, record):
        from datetime import datetime, timezone
        from acs_evidence_transport import safe_destination
        path = safe_destination(Path(target) / 'acs-collection.json')
        record = dict(record, schema_version=2, observed_at=datetime.now(timezone.utc).isoformat())
        path.write_text(json.dumps(record)); path.chmod(0o600)

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
            operation = self._download_agent_evidence(target) if checkpoint else self._final_agent_evidence(target)
            result = await asyncio.wait_for(operation, timeout=90 if checkpoint else 600)
            record = dict(result, status='checkpoint' if checkpoint else 'collected')
        except Exception as error:
            record = dict(status='collection_failed', complete=False, error_type=type(error).__name__,
                          partial_chunks_retained=True)
            if hasattr(self, 'logger'):
                self.logger.warning('Partial agent evidence collection (%s); cleanup continues', type(error).__name__)
        # The original files remain private. A failed later collection does not
        # delete the last successfully retained checkpoint.
        try:
            self._record_agent_collection(target, record)
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
        if getattr(self,'_verifier_evidence_downloaded',False):
            return
        if (target/'test-stdout.txt').is_file() and not (target/'acs-collection.json').exists():
            return  # Legacy completed collection has no coverage receipt.
        try:
            await asyncio.wait_for(self.download_dir_filtered('/logs/verifier',target),timeout=600)
        except Exception as error:
            # Transfer failure must not prevent cleanup. A missing reward is
            # diagnosed by Harbor's verifier after available logs are saved.
            try:
                self._record_verifier_collection(target, dict(status='collection_failed', complete=False,
                    error_type=type(error).__name__, partial_chunks_retained=True))
            except (OSError, ValueError):
                pass
            if hasattr(self,'logger'):
                self.logger.warning('Partial verifier evidence collection (%s); cleanup continues',type(error).__name__)

    def _record_verifier_collection(self, target, record):
        from datetime import datetime, timezone
        from acs_evidence_transport import safe_destination
        path = safe_destination(Path(target) / 'acs-collection.json')
        record = dict(record, schema_version=2, observed_at=datetime.now(timezone.utc).isoformat())
        path.write_text(json.dumps(record)); path.chmod(0o600)

    async def _final_verifier_evidence(self, target, **filters):
        from acs_verifier_transport import download
        if not hasattr(self, '_verifier_collection_lock'):
            self._verifier_collection_lock = asyncio.Lock()
        async with self._verifier_collection_lock:
            while True:
                result = await download(self, target, **filters)
                if result['stable']:
                    return result
                await asyncio.sleep(1)

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
            result = await asyncio.wait_for(self._final_agent_evidence(target_dir), timeout=600)
            self._record_agent_collection(target_dir, dict(result, status='collected'))
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
        if str(source_dir) == '/logs/verifier':
            target = Path(target_dir)
            result = await asyncio.wait_for(self._final_verifier_evidence(target,
                include=include, exclude=exclude, protect=protect), timeout=600)
            self._record_verifier_collection(target, dict(result, status='collected'))
            self._verifier_evidence_downloaded = result['complete']
            # Missing reward is a native Harbor scoring error, not a transport
            # failure. Invalid reward is preserved verbatim with all diagnostics.
            if 'reward.txt' not in result['manifest']:
                return
            with (target/'reward.txt').open('rb') as inp:
                raw_reward = inp.read(128)
            try:
                reward = raw_reward.decode().strip()
            except UnicodeDecodeError:
                raise ValueError('Invalid verifier reward; available diagnostics retained') from None
            if result['manifest']['reward.txt']['bytes'] > 128 or reward not in {'0', '1', '0.0', '1.0'}:
                raise ValueError('Invalid verifier reward; available diagnostics retained')
            return
        return await super().download_dir_filtered(source_dir, target_dir, include=include, exclude=exclude, protect=protect)
