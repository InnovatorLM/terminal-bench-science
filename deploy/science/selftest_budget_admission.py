"""Completed-version budgets and explicit audits of unrecoverable legacy gaps.

An audit admits an already completed frozen task to independent review. It does
not attest an unknown duration, reset an idea, or permit further author iteration.
"""
import hashlib
import json
from pathlib import Path

from author_submission_gate import read_operator_file
from evaluation_queue import atomic_json
from iteration_budget import number
from selftest_budget import BudgetEvidenceRequired, read_observation, verify

LEGACY_GAPS = {'iteration_observation_missing', 'iteration_observation_history_gap',
               'iteration_original_start_conflict'}
IDENTITY = ('role', 'idea_id', 'job_id', 'source_commit', 'source_task_path', 'snapshot_sha256')


def assert_current_idea_active(work, status, observation=None):
    """A frozen success never overrides a later explicit abandonment.

    Missing history remains a budget evidence problem handled by assess_budget;
    this check only reads current operator observations and never changes clocks.
    Finished ideas remain eligible for review of their completed version.
    """
    work=Path(work)
    path=Path(observation) if observation is not None else (
        work.parent.parent/'command-watchdog/iteration-observations'/(status['role']+'.json'))
    try:observed=json.loads(read_operator_file(path))
    except FileNotFoundError:return
    if observed.get('role')!=status.get('role'):
        raise ValueError('Current idea observation role mismatch')
    row=observed.get('ideas',{}).get(status.get('idea_id'))
    if row and (row.get('status')=='abandoned' or any(
            event.get('action')=='abandon' for event in row.get('events',[]))):
        raise ValueError('Idea abandoned; its completed selftest cannot authorize review')


def read_audit(path, status, error_code=None):
    path=Path(path);raw=read_operator_file(path);audit=json.loads(raw)
    if (audit.get('schema_version')!=1 or audit.get('kind')!='legacy_completed_budget_unknown'
            or audit.get('operator_reviewed') is not True
            or audit.get('scope')!='completed_frozen_version_only'
            or audit.get('budget_error_code') not in LEGACY_GAPS
            or (error_code is not None and audit.get('budget_error_code')!=error_code)
            or not isinstance(audit.get('reason'),str) or len(audit['reason'].strip())<40
            or any(not status.get(key) or audit.get(key)!=status.get(key) for key in IDENTITY)):
        raise ValueError('Invalid exact-version legacy budget audit')
    completed=number(status.get('completed_at'));audited=number(audit.get('audited_at'))
    if (status.get('status')!='selftest_passed' or status.get('source_git_tree_verified') is not True
            or audit.get('completed_at')!=completed or audited<completed):
        raise ValueError('Legacy audit requires a completed source-verified task')
    expected=path.parent/'status.json'
    if (audit.get('status_file')!=str(expected)
            or hashlib.sha256(read_operator_file(expected)).hexdigest()!=audit.get('status_sha256')):
        raise ValueError('Legacy audit source status changed')
    evidence=audit.get('evidence_files')
    if not isinstance(evidence,list) or not evidence:raise ValueError('Legacy audit lacks historical evidence')
    for item in evidence:
        if (not isinstance(item,dict) or not isinstance(item.get('path'),str)
                or hashlib.sha256(read_operator_file(item['path'])).hexdigest()!=item.get('sha256')):
            raise ValueError('Legacy audit historical evidence changed')
    return audit,hashlib.sha256(raw).hexdigest()


def assess_budget(work, state, status, observation, at):
    work=Path(work);completed=number(status.get('completed_at'))
    assert_current_idea_active(work,status,observation)
    if not number(status.get('received_at'))<=completed<=number(at):
        raise ValueError('Invalid completed selftest chronology')
    clock=work/'handoff-clock.json'
    try:
        atomic_json(clock,read_observation(observation,status['role'],status['idea_id']))
        budget=verify(state,status['role'],status['idea_id'],clock,at,completed_at=completed)
    except BudgetEvidenceRequired as error:
        audit_path=work/'legacy-budget-admission.json'
        if error.code not in LEGACY_GAPS or not audit_path.exists():raise
        audit,digest=read_audit(audit_path,status,error.code)
        if not clock.exists():atomic_json(clock,dict(observation_missing=True,role=status['role'],idea_id=status['idea_id']))
        return dict(budget_status='legacy_budget_unknown',within_budget=None,
                    active_iteration_seconds_upper_bound=None,iteration_cutoff_at=completed,
                    historical_gap=error.handoff(),admission_file=str(audit_path),admission_sha256=digest,
                    scope=audit['scope'],scientific_acceptance=False)
    if not budget['within_budget']:
        raise BudgetEvidenceRequired('iteration_pause_evidence_required',
            'The conservative five-hour bound at task completion is exceeded. Establish independently verified excluded intervals or confirm the actual budget outcome; this does not prove five hours of author work.',
            role=status['role'],idea_id=status['idea_id'],
            active_iteration_seconds_upper_bound=budget['active_iteration_seconds_upper_bound'],
            recorded_idea_started_at=budget['recorded_idea_started_at'],total_excluded_seconds=budget['total_excluded_seconds'])
    budget['budget_status']='verified_within_budget'
    return budget


def validate_admission(budget, commit, task, *, expected_status):
    if (budget.get('budget_status')!='legacy_budget_unknown' or budget.get('within_budget') is not None
            or budget.get('active_iteration_seconds_upper_bound') is not None
            or budget.get('scope')!='completed_frozen_version_only'):
        raise ValueError('Unknown historical budget cannot claim a measured duration')
    path=Path(budget.get('admission_file',''))
    status=json.loads(read_operator_file(path.parent/'status.json'))
    audit,digest=read_audit(path,status)
    if (audit['source_commit']!=commit or audit['source_task_path']!=task
            or any(status.get(key)!=expected_status.get(key) for key in IDENTITY)
            or status.get('completed_at')!=expected_status.get('completed_at')
            or budget.get('admission_sha256')!=digest
            or budget.get('iteration_cutoff_at')!=status['completed_at']
            or (budget.get('historical_gap') or {}).get('error_class')!=audit['budget_error_code']):
        raise ValueError('Legacy budget admission does not match the completed version')
