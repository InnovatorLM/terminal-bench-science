"""Allowlisted build diagnostics and atomic result packets for public jobs."""
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

from publish_evidence import redact
from selftest_logs import sanitize


def diagnostic_paths(root):
    root = Path(root)
    paths = [root/name for name in ('build.log', 'build.stdout.log', 'build.stderr.log',
        'build.json', 'mechanical-check.json', 'diagnosis.md', 'build-investigation.json')]
    build = root/'build'
    paths += sorted(build.glob('docker-*.log'))
    paths += sorted(build.glob('docker-*.json'))
    paths += [build/name for name in ('failure.json', 'identity.json', 'receipt.json',
        'images-in-progress.json', 'build-context-preflight.json')]
    for context in ('task/environment', 'task/tests', 'environment-effective',
                    'tests-effective', 'runtime', 'verifier'):
        paths += [build/context/name for name in ('Dockerfile', '.dockerignore')]
    return [p for p in paths if p.is_file() and not p.is_symlink()
            and p.resolve().is_relative_to(root.resolve())]


def build_diagnostics(root, secrets=()):
    root = Path(root)
    invocation = root/'build.json'
    metadata = json.loads(invocation.read_text()) if invocation.is_file() else None
    commands = [json.loads(p.read_text()) for p in sorted((root/'build').glob('docker-*.json'))]
    failed = next((row for row in commands if row.get('returncode') not in (None, 0)
        or row.get('status') in {'exception', 'timed_out'}), None)
    logs = []
    if failed:
        stem = 'docker-' + str(failed['step']).zfill(2)
        logs += [root/'build'/(stem+'.stderr.log'), root/'build'/(stem+'.stdout.log')]
    logs += sorted((root/'build').glob('docker-*.log'))
    logs += [root/'build.stderr.log', root/'build.log']
    first = None
    for path in logs:
        if not path.is_file():
            continue
        with path.open(errors='replace') as stream:
            for line in stream:
                if re.search(r'(?i)(^\s*(?:error|fatal)|\b(?:error response from daemon|failed to solve|returned a non-zero code)\b)', line):
                    first = dict(message=line.strip()[:2000], log='diagnostics/'+str(path.relative_to(root)))
                    break
        if first:
            break
    report = dict(invocation=metadata, docker_commands=commands, first_error=first,
        docker_builds=[row for row in commands if row.get('operation')=='build'],
        stdout_stderr_separate=bool(metadata),
        command_capture='recorded' if commands else 'unavailable_for_legacy_run')
    failure = root/'build/failure.json'
    if failure.is_file():
        report['failure'] = json.loads(failure.read_text())
    preflight = root/'build/build-context-preflight.json'
    if preflight.is_file():
        report['verifier_build'] = json.loads(preflight.read_text()).get('verifier_build')
    investigation = root/'build-investigation.json'
    if investigation.is_file():
        report['investigation'] = json.loads(investigation.read_text())
    return redact(report, secrets)


def publish_artifacts(root, public, secrets=()):
    root = Path(root)
    public['build_diagnostics'] = build_diagnostics(root, secrets)
    pending, final = root/'artifacts.zip.writing', root/'artifacts.zip'
    manifest = {}
    def safe(data):
        if any(secret.encode() in data for secret in secrets if secret):
            raise ValueError('Credential detected in result packet')
        return data
    def put(archive, name, data):
        data = safe(data)
        archive.writestr(name, data)
        manifest[name] = dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    try:
        with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                result = {k:v for k,v in public.items() if k not in {'artifacts_url', 'artifacts_sha256'}}
                put(archive, 'result.json', json.dumps(redact(result, secrets), ensure_ascii=False, indent=2).encode())
                for path in diagnostic_paths(root):
                    text = path.read_text(errors='replace')
                    if path.suffix=='.json':
                        text = json.dumps(redact(json.loads(text), secrets), ensure_ascii=False, indent=2)
                    else:
                        text = sanitize(text, secrets)
                    put(archive, 'diagnostics/'+str(path.relative_to(root)), text.encode())
                for path in sorted((root/'evidence').rglob('*')):
                    if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                        put(archive, str(path.relative_to(root)), path.read_bytes())
                archive.writestr('manifest.json', json.dumps(dict(files=manifest,
                    logs='Complete retained output with secrets redacted; legacy runs may only have combined streams.'),
                    ensure_ascii=False, indent=2).encode())
        os.replace(pending, final)
    finally:
        pending.unlink(missing_ok=True)
    return dict(artifacts_url='/tasks/'+public['run_id']+'/artifacts',
                artifacts_sha256=hashlib.sha256(final.read_bytes()).hexdigest())
