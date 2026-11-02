"""Verify chronological, same-author result-driven resubmission evidence.

The note is an author claim. This module proves linked execution and changed
task bytes, not that the scientific change is good or sufficiently difficult.
"""
import json
import math
import re
from author_submission_gate import read_operator_file
from evaluation_queue import digest_tree


def validate(iteration, current, state, role):
    if not isinstance(iteration, dict) or set(iteration) != {'previous_job', 'note'}:
        raise ValueError('Invalid iteration linkage')
    job, note = iteration['previous_job'], iteration['note']
    if (not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job)
            or not isinstance(note, str) or not 20 <= len(note.strip()) <= 4000):
        raise ValueError('Invalid previous job or iteration explanation')
    prior = json.loads(read_operator_file(state/job/'status.json', 1024*1024))
    if (not isinstance(prior, dict) or prior.get('schema_version') != 2 or prior.get('role') != role
            or prior.get('job_id') != job or prior.get('status') not in ('selftest_passed', 'selftest_failed')
            or prior.get('source_git_tree_verified') is not True):
        raise ValueError('Prior job must be this author completed container selftest')
    for key in ('source_repository', 'source_task_path'):
        if prior.get(key) != current.get(key):
            raise ValueError('Iteration must refer to the same task/repository')
    if current.get('idea_id') is not None and prior.get('idea_id') != current['idea_id']:
        raise ValueError('Iteration cannot change or introduce an unbound idea ID')
    end, start = prior.get('completed_at'), current.get('received_at')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (end, start)) or end > start:
        raise ValueError('Iteration must follow completed execution')
    trials = prior.get('trials')
    if (not isinstance(trials, list) or len(trials) != 2
            or any(not isinstance(t, dict) or t.get('cleanup_confirmed') is not True for t in trials)):
        raise ValueError('Prior cloud cleanup must be confirmed')
    execution_task = prior.get('execution_task_path')
    # The logical repository/task (and bound idea) above define identity.
    # Independent submissions may use distinct safe staging directory names.
    for execution_path in (execution_task, current.get('execution_task_path')):
        if (not isinstance(execution_path, str)
                or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,90}', execution_path)):
            raise ValueError('Invalid frozen execution task path')
    if digest_tree(state/job/'source'/execution_task) != prior.get('snapshot_sha256'):
        raise ValueError('Prior frozen task changed')
    if prior.get('source_commit') == current.get('source_commit') or prior.get('snapshot_sha256') == current.get('snapshot_sha256'):
        raise ValueError('Result-driven iteration requires a changed committed task')
    return dict(previous_job=job, author_note=note.strip(), prior_execution_completed_at=end,
                changed_committed_task=True, linked_execution_verified=True,
                scientific_acceptance=False, author_note_independently_reviewed=False)
