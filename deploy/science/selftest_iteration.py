"""Verify chronological, same-author result-driven resubmission evidence.

The note is an author claim. This module proves linked execution and changed
task bytes, not that the scientific change is good or sufficiently difficult.
"""
import json
import math
import re
from author_submission_gate import read_operator_file
from evaluation_queue import digest_tree


class IterationEvidenceError(ValueError):
    """An invalid submission link, distinct from a failed build or ACS run."""

    def __init__(self, code, message, remediation, *, previous_job=None,
                 current_idea=None, previous_idea=None, failure_owner='request',
                 task_change_required=False):
        super().__init__(message)
        self.report = dict(
            error_class=('author_iteration_link_invalid' if failure_owner == 'request'
                         else 'platform_iteration_evidence_invalid'),
            iteration_error_code=code, stage='iteration_preflight',
            failure_owner=failure_owner, retry_requires_task_change=task_change_required,
            cloud_execution_started=False, prior_execution_rerun_required=False,
            remediation=remediation + ' Preserve prior job receipts and idea clocks. '
                'For an already successful current job, correct the handoff with '
                'role-acs-selftest request-review CURRENT_JOB --previous-job PRIOR_JOB '
                '--iteration-note NOTE; do not rebuild or rerun it solely to repair review metadata.')
        # Only credential-free, validated identifiers enter role-visible feedback.
        for key, value, pattern in (
                ('previous_job', previous_job, '[a-f0-9]{32}'),
                ('current_idea_id', current_idea, '[a-z0-9][a-z0-9-]{0,95}'),
                ('previous_idea_id', previous_idea, '[a-z0-9][a-z0-9-]{0,95}')):
            if isinstance(value, str) and re.fullmatch(pattern, value):
                self.report[key] = value


def validate(iteration, current, state, role):
    if not isinstance(iteration, dict) or set(iteration) != {'previous_job', 'note'}:
        raise IterationEvidenceError('invalid_linkage', 'Invalid iteration linkage',
            'Supply previous_job and a result-driven iteration note together.')
    job, note = iteration['previous_job'], iteration['note']
    if (not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job)
            or not isinstance(note, str) or not 20 <= len(note.strip()) <= 4000):
        raise IterationEvidenceError('invalid_previous_job_or_note',
            'Invalid previous job or iteration explanation',
            'Use the full 32-character prior job ID and a 20-4000 character iteration note.')
    try:
        prior = json.loads(read_operator_file(state/job/'status.json', 1024*1024))
    except FileNotFoundError:
        raise IterationEvidenceError('prior_receipt_unavailable', 'Prior selftest receipt is unavailable',
            'Select a retained completed selftest belonging to this author and task. '
            'If the original receipt was lost, ask the platform to restore it; do not invent a job ID.',
            previous_job=job) from None
    except ValueError:
        raise IterationEvidenceError('prior_receipt_invalid', 'Prior selftest evidence is invalid',
            'The platform must inspect and restore the original immutable receipt.',
            previous_job=job, failure_owner='platform') from None
    if (not isinstance(prior, dict) or prior.get('schema_version') != 2 or prior.get('role') != role
            or prior.get('job_id') != job or prior.get('status') not in ('selftest_passed', 'selftest_failed')
            or prior.get('source_git_tree_verified') is not True):
        raise IterationEvidenceError('prior_execution_not_eligible',
            'Prior job must be this author completed container selftest',
            'Select an existing source-verified selftest_passed/selftest_failed receipt for this author. '
            'An interrupted, queued or preflight-rejected attempt is not iteration evidence. '
            'Retrying an interrupted unchanged snapshot is an execution retry, not a new iteration link.',
            previous_job=job)
    for key in ('source_repository', 'source_task_path'):
        if prior.get(key) != current.get(key):
            raise IterationEvidenceError('task_identity_mismatch',
                'Iteration must refer to the same task/repository',
                'Use a prior job from the same source repository and task path; do not relabel a receipt.',
                previous_job=job)
    if current.get('idea_id') is not None and prior.get('idea_id') != current['idea_id']:
        raise IterationEvidenceError('idea_identity_mismatch',
            'Iteration cannot change or introduce an unbound idea ID',
            'Use the original stable idea ID across revisions and link a prior job already bound to that idea. '
            'Do not create a revision-specific idea ID or rewrite an existing job. '
            'If a successful execution was bound incorrectly, request platform reconciliation of the retained '
            'lineage instead of repeatedly resubmitting it.', previous_job=job,
            current_idea=current.get('idea_id'), previous_idea=prior.get('idea_id'))
    end, start = prior.get('completed_at'), current.get('received_at')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (end, start)) or end > start:
        raise IterationEvidenceError('execution_chronology_invalid',
            'Iteration must follow completed execution',
            'Link an execution completed before this submission; preserve the recorded timestamps.',
            previous_job=job)
    trials = prior.get('trials')
    if (not isinstance(trials, list) or len(trials) != 2
            or any(not isinstance(t, dict) or t.get('cleanup_confirmed') is not True for t in trials)):
        raise IterationEvidenceError('prior_cleanup_unconfirmed',
            'Prior cloud cleanup must be confirmed',
            'The platform must verify cleanup for both controls of the retained prior job before reuse.',
            previous_job=job, failure_owner='platform')
    execution_task = prior.get('execution_task_path')
    # The logical repository/task (and bound idea) above define identity.
    # Independent submissions may use distinct safe staging directory names.
    for execution_path in (execution_task, current.get('execution_task_path')):
        if (not isinstance(execution_path, str)
                or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,90}', execution_path)):
            raise IterationEvidenceError('execution_path_invalid',
                'Invalid frozen execution task path',
                'Use a valid source-bound task staging path; ask the platform to inspect malformed frozen evidence.',
                previous_job=job)
    if digest_tree(state/job/'source'/execution_task) != prior.get('snapshot_sha256'):
        raise IterationEvidenceError('prior_snapshot_changed', 'Prior frozen task changed',
            'The platform must recover the exact prior frozen snapshot; do not replace its receipt or hash.',
            previous_job=job, failure_owner='platform')
    if prior.get('source_commit') == current.get('source_commit') or prior.get('snapshot_sha256') == current.get('snapshot_sha256'):
        raise IterationEvidenceError('unchanged_task',
            'Result-driven iteration requires a changed committed task',
            'For review qualification, link the earlier genuinely different executed version that informed '
            'this change. An unchanged retry is not a result-driven iteration; do not make dummy edits.',
            previous_job=job, task_change_required=True)
    return dict(previous_job=job, author_note=note.strip(), prior_execution_completed_at=end,
                changed_committed_task=True, linked_execution_verified=True,
                scientific_acceptance=False, author_note_independently_reviewed=False)
