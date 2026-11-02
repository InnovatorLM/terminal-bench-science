#!/usr/bin/env python3
"""Publish allowlisted, redacted evidence into an evaluator's private packet."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from execution_evidence import read_file, evidence_file
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


def publish_jsonl(path, src, target, secrets):
    """Keep memory proportional to one event, not a four-hour native session."""
    source_hash, output_hash = hashlib.sha256(), hashlib.sha256()
    size, partial, in_key = 0, [], False
    with evidence_file(path, src).open('rb') as inp, target.open('xb') as out:
        os.fchmod(out.fileno(), 0o600)
        for number, raw in enumerate(inp, 1):
            source_hash.update(raw)
            line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
            if not line.strip():
                continue
            try:
                if in_key:
                    raise ValueError('Unterminated private key')
                value = json.dumps(redact(json.loads(line), secrets), ensure_ascii=False)
            except ValueError:
                partial.append(number)
                # A cancelled native writer may leave invalid final JSON. Also
                # protect a pasted multiline PEM in malformed diagnostic text.
                if '-----BEGIN ' in line and 'PRIVATE KEY-----' in line:
                    in_key = True
                if in_key:
                    value = '[REDACTED PRIVATE KEY]'
                    if '-----END ' in line and 'PRIVATE KEY-----' in line:
                        in_key = False
                else:
                    value = sanitize(line, secrets)
            rendered = (value + '\n').encode()
            if any(secret and str(secret).encode() in rendered for secret in secrets):
                raise ValueError('Credential remains after redaction')
            out.write(rendered); output_hash.update(rendered); size += len(rendered)
    return {'source_sha256': source_hash.hexdigest(), 'sha256': output_hash.hexdigest(), 'bytes': size}, partial


def publish(src, dst, secrets=(), gid=None):
    src, dst = Path(src), Path(dst)
    if os.geteuid() != 0 or not src.resolve().is_relative_to(W) or not dst.resolve().is_relative_to(W):
        raise ValueError('Operator workspace-local packet paths required')
    if dst.exists() or src.is_symlink() or any(p.is_symlink() for p in dst.parents):
        raise ValueError('Fresh unlinked evidence destination required')
    dst.mkdir(mode=0o700, parents=True)
    names = ['result.json', 'config.json', 'trial.log', 'agent/trajectory.json',
             'agent/mini-swe-agent.trajectory.json', 'agent/mini-swe-agent.txt',
             'verifier/reward.txt', 'verifier/reward.json', 'verifier/test-stdout.txt', 'verifier/test-stderr.txt']
    names += [str(path.relative_to(src)) for path in sorted((src/'agent').rglob('*'))
              if path.is_file() and path.suffix in {'.json','.jsonl','.txt','.log'} and str(path.relative_to(src)) not in names]
    files = {}
    partial_jsonl = {}
    try:
        for name in names:
            path = src / name
            if not path.exists():
                continue
            target = dst / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if name.endswith('.jsonl') and not any('\n' in secret or '\r' in secret for secret in secrets):
                files[name], partial = publish_jsonl(path, src, target, secrets)
                if partial:
                    partial_jsonl[name] = partial
                continue
            raw = read_file(path, src)
            if name == 'config.json':
                config = json.loads(raw)
                content = {'agent': {key: config.get('agent', {}).get(key) for key in ('name', 'model_name')},
                           'task_name': Path(config.get('task', {}).get('path', '')).name}
            elif name.endswith('.jsonl'):
                lines=[]
                for number,line in enumerate(raw.decode('utf-8',errors='replace').splitlines(),1):
                    if not line.strip():continue
                    try:lines.append(json.dumps(redact(json.loads(line),secrets),ensure_ascii=False))
                    except ValueError:
                        # A cancelled writer can leave a partial final event.
                        # Preserve it, sanitized, and label it instead of losing
                        # the entire earlier session and diagnostic packet.
                        lines.append(sanitize(line,secrets))
                        partial_jsonl.setdefault(name,[]).append(number)
                content='\n'.join(lines)+'\n'
            elif name.endswith('.json'):
                content = redact(json.loads(raw), secrets)
            else:
                content = sanitize(raw.decode('utf-8', errors='replace'), secrets)
            rendered = (json.dumps(content, ensure_ascii=False, indent=2) if isinstance(content, (dict, list)) else content).encode()
            if any(secret and str(secret).encode() in rendered for secret in secrets):
                raise ValueError('Credential remains after redaction')
            target.write_bytes(rendered)
            target.chmod(0o600)
            files[name] = {'source_sha256': hashlib.sha256(raw).hexdigest(), 'sha256': hashlib.sha256(rendered).hexdigest(), 'bytes': len(rendered)}
        trace = json.loads((dst / 'agent/trajectory.json').read_text()) if 'agent/trajectory.json' in files else {}
        trajectory = (str(trace.get('schema_version', '')).startswith('ATIF-') and isinstance(trace.get('steps'), list)
                      and any(isinstance(step, dict) and step.get('source') == 'agent' for step in trace['steps']))
        verifier = ('verifier/test-stdout.txt' in files and any(name in files for name in ('verifier/reward.txt','verifier/reward.json')))
        manifest = {'trajectory_available': trajectory, 'verifier_available': verifier, 'files': files,
                    'partial_jsonl_lines': partial_jsonl,
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
