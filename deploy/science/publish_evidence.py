#!/usr/bin/env python3
"""Publish allowlisted, redacted evidence into an evaluator's private packet."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from execution_evidence import read_file
from selftest_logs import sanitize, credential_values

W = Path('/mnt/data/zhanglinfeng/workspace')


def redact(value, secrets):
    if isinstance(value, str):
        return sanitize(value, secrets)
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        import re
        return {key: '[REDACTED]' if re.search(r'(?i)(api[_-]?key|password|authorization|secret|access_token|credential)', key)
                else redact(item, secrets) for key, item in value.items()}
    return value


def publish(src, dst, secrets=(), gid=None):
    src, dst = Path(src), Path(dst)
    if os.geteuid() != 0 or not src.resolve().is_relative_to(W) or not dst.resolve().is_relative_to(W):
        raise ValueError('Operator workspace-local packet paths required')
    if dst.exists() or src.is_symlink() or any(p.is_symlink() for p in dst.parents):
        raise ValueError('Fresh unlinked evidence destination required')
    dst.mkdir(mode=0o700, parents=True)
    names = ['result.json', 'config.json', 'trial.log', 'agent/trajectory.json',
             'agent/mini-swe-agent.trajectory.json', 'agent/mini-swe-agent.txt',
             'verifier/reward.txt', 'verifier/test-stdout.txt', 'verifier/test-stderr.txt']
    names += [str(path.relative_to(src)) for path in sorted((src/'agent').rglob('*'))
              if path.is_file() and path.suffix in {'.json','.jsonl','.txt','.log'} and str(path.relative_to(src)) not in names]
    files = {}
    try:
        for name in names:
            path = src / name
            if not path.exists():
                continue
            raw = read_file(path, src)
            if name == 'config.json':
                config = json.loads(raw)
                content = {'agent': {key: config.get('agent', {}).get(key) for key in ('name', 'model_name')},
                           'task_name': Path(config.get('task', {}).get('path', '')).name}
            elif name.endswith('.jsonl'):
                content='\n'.join(json.dumps(redact(json.loads(line),secrets),ensure_ascii=False) for line in raw.decode().splitlines() if line.strip())+'\n'
            elif name.endswith('.json'):
                content = redact(json.loads(raw), secrets)
            else:
                content = sanitize(raw.decode('utf-8', errors='replace'), secrets)
            rendered = (json.dumps(content, ensure_ascii=False, indent=2) if isinstance(content, (dict, list)) else content).encode()
            if any(secret and str(secret).encode() in rendered for secret in secrets):
                raise ValueError('Credential remains after redaction')
            target = dst / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(rendered)
            target.chmod(0o600)
            files[name] = {'source_sha256': hashlib.sha256(raw).hexdigest(), 'sha256': hashlib.sha256(rendered).hexdigest(), 'bytes': len(rendered)}
        trace = json.loads((dst / 'agent/trajectory.json').read_text()) if 'agent/trajectory.json' in files else {}
        trajectory = (str(trace.get('schema_version', '')).startswith('ATIF-') and isinstance(trace.get('steps'), list)
                      and any(isinstance(step, dict) and step.get('source') == 'agent' for step in trace['steps']))
        verifier = all(name in files for name in ('verifier/reward.txt', 'verifier/test-stdout.txt'))
        manifest = {'trajectory_available': trajectory, 'verifier_available': verifier, 'files': files,
                    'sanitized': True, 'raw_trajectory_exported': False, 'scientific_acceptance': False,
                    'redaction_scope': 'Known credential values, structured secret fields, bearer tokens and private keys; not an exhaustive secret audit'}
        path = dst / 'manifest.json';path.write_text(json.dumps(manifest, indent=2));path.chmod(0o600)
        if gid is not None:
            for path in [dst, *dst.rglob('*')]:
                os.chown(path, 0, gid);path.chmod(0o750 if path.is_dir() else 0o640)
        return manifest
    except BaseException:
        shutil.rmtree(dst)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial-dir', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    result = publish(args.trial_dir, args.out, credential_values(W))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
