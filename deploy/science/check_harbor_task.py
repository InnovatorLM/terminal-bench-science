#!/usr/bin/env python3
"""Nonexecuting mechanical checks against the installed Harbor schema."""
import argparse
import hashlib
import importlib.metadata
import inspect
import json
import math
from pathlib import Path
import subprocess
from evaluation_queue import W, atomic_json, digest_tree, root_file
from deployment_paths import allowed_development_path


def validate_mechanical_check(path, snapshot, expected_hash):
    path = root_file(path)
    raw = path.read_bytes()
    report = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != expected_hash or report.get('snapshot_sha256') != digest_tree(snapshot):
        raise ValueError('Mechanical check does not match registered evidence/task')
    if any(report.get(key) is not True for key in ['schema_parsed', 'required_files_nonempty', 'shell_syntax_valid']):
        raise ValueError('Mechanical checks did not pass')
    if report.get('task_code_executed') is not False or report.get('scientific_acceptance') is not False:
        raise ValueError('Mechanical evidence scope is inconsistent')
    return report


def inspect_task(snapshot, *, acs_environment=None):
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.paths import TaskPaths
    snapshot = Path(snapshot)
    if not allowed_development_path(snapshot):
        raise ValueError('Task must be workspace-local')
    before = digest_tree(snapshot)
    paths = TaskPaths(snapshot)
    for name in ['instruction.md', 'task.toml', 'environment/Dockerfile', 'tests/test.sh', 'solution/solve.sh']:
        path = snapshot / name
        if not path.is_file() or not path.read_bytes().strip():
            raise ValueError('Standard Harbor file missing or empty: ' + name)
    config = TaskConfig.model_validate_toml(paths.config_path.read_text())
    from docker_context_preflight import require_contexts
    docker_context = require_contexts(snapshot)
    network = None
    if acs_environment is not None:
        from acs_network_preflight import require_compatible
        network = require_compatible(config, environment=acs_environment)
    timeouts = {'agent': config.agent.timeout_sec, 'verifier': config.verifier.timeout_sec, 'build': config.environment.build_timeout_sec}
    for value in timeouts.values():
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError('Task timeouts must be finite and positive')
    resources = {name: getattr(config.environment, name) for name in ['cpus', 'memory_mb', 'storage_mb', 'gpus']}
    for name, value in resources.items():
        if value is not None and (value < 0 or (name != 'gpus' and value == 0)):
            raise ValueError('Invalid declared resource: ' + name)
    for name in ['tests/test.sh', 'solution/solve.sh']:
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-n', str(snapshot / name)], env={'PATH': '/usr/bin:/bin', 'HOME': str(W / '.state/harbor-home')}, capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError('Shell entry point syntax check failed: ' + name)
    if digest_tree(snapshot) != before:
        raise ValueError('Task changed during mechanical check')
    schema = Path(inspect.getfile(TaskConfig))
    report = {'snapshot_sha256': before, 'harbor_version': importlib.metadata.version('harbor'),
            'task_config_source_sha256': hashlib.sha256(schema.read_bytes()).hexdigest(),
            'checker_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'schema_parsed': True, 'required_files_nonempty': True, 'shell_syntax_valid': True,
            'schema_resolved_resources': resources, 'schema_resolved_timeouts': timeouts, 'task_code_executed': False,
            'scientific_acceptance': False, 'license_verified': False, 'runtime_verified': False}
    report['docker_context_preflight'] = docker_context
    if network is not None:
        report['acs_network_preflight'] = network
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--acs', action='store_true', help='Check the actual ACK/ACS network capabilities without cloud access')
    args = parser.parse_args()
    if args.output.exists() or not allowed_development_path(args.output):
        raise ValueError('Require a fresh workspace output')
    atomic_json(args.output, inspect_task(args.task, acs_environment={} if args.acs else None))


if __name__ == '__main__':
    main()
