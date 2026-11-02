"""Validate the original-author to frozen-build identity bridge.

This prepares registration evidence only. It never grants iteration qualification
or scientific acceptance, and does not rewrite image provenance labels.
"""
import hashlib
import argparse
import json
import os
import re
from pathlib import Path

from author_submission_gate import read_operator_file, validate_identity
from build_task_images import validate_build_receipt
from evaluation_queue import digest_tree, atomic_json, root_file


def prepare(work, commit, task, template):
    work = Path(work)
    validate_identity(commit, task)
    status_raw = read_operator_file(work/'status.json', 1024*1024)
    status = json.loads(status_raw)
    if not isinstance(status, dict):
        raise ValueError('Invalid selftest status')
    if (status.get('schema_version') != 2 or status.get('status') != 'selftest_passed'
            or status.get('source_commit') != commit or status.get('source_task_path') != task
            or status.get('source_git_tree_verified') is not True
            or status.get('quality_acceptance') is not False):
        raise ValueError('Require successful source-bound selftest for this author version')
    execution_commit = status.get('execution_commit')
    execution_task = status.get('execution_task_path')
    if not isinstance(execution_commit, str) or not re.fullmatch('[a-f0-9]{40}', execution_commit):
        raise ValueError('Missing frozen build commit')
    # A Git-verified source task may have been submitted from a staging
    # directory with another basename. Preserve that execution identity for
    # snapshot and receipt checks instead of confusing it with source identity.
    if (not isinstance(execution_task, str)
            or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,90}', execution_task)):
        raise ValueError('Invalid frozen task path')
    trials = status.get('trials')
    if not isinstance(trials, list) or len(trials) != 2:
        raise ValueError('Require oracle and negative control evidence')
    for trial, agent, reward in zip(trials, ('oracle', 'nop'), (1, 0)):
        if (not isinstance(trial, dict) or trial.get('agent') != agent or trial.get('exit_code') != 0
                or trial.get('cleanup_confirmed') is not True
                or not isinstance(trial.get('results'), list) or len(trial['results']) != 1):
            raise ValueError('Incomplete selftest execution or cleanup')
        result = trial['results'][0]
        if (not isinstance(result, dict) or result.get('exception_type')
                or not isinstance(result.get('rewards'), dict)
                or type(result['rewards'].get('reward')) not in (int,float)
                or result['rewards'].get('reward') != reward):
            raise ValueError('Selftest controls did not pass')
    snapshot = work/'source'/execution_task
    if digest_tree(snapshot) != status.get('snapshot_sha256'):
        raise ValueError('Frozen selftest task changed')
    receipt_raw = read_operator_file(work/'build/receipt.json')
    receipt = json.loads(receipt_raw)
    validate_build_receipt(receipt, snapshot, execution_commit, execution_task, template)
    for role in ('official', 'runtime', 'verifier'):
        if status.get('images', {}).get(role) != receipt['images'][role]['published_digest']:
            raise ValueError('Selftest image differs from frozen publication')
    return dict(source_commit=commit, source_task_path=task,
                execution_commit=execution_commit, execution_task_path=execution_task,
                snapshot=str(snapshot), snapshot_sha256=status['snapshot_sha256'],
                selftest_status_sha256=hashlib.sha256(status_raw).hexdigest(),
                build_receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
                scientific_acceptance=False, iteration_qualified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Operator required')
    from evaluation_queue import W
    if args.output.exists() or not args.output.resolve().is_relative_to(W):
        raise ValueError('Require fresh workspace output')
    evidence = prepare(args.work, args.commit, args.task,
                       json.loads(root_file(args.template).read_text()))
    atomic_json(args.output, evidence)
    print(json.dumps({'prepared': True, 'iteration_qualified': False,
                      'scientific_acceptance': False, 'output': str(args.output)}))


if __name__ == '__main__':
    main()
