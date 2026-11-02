"""Conservative idea budget from ACS and independently observed maintenance.

Unverified pauses receive no time refund. Passing this upper bound proves the
recorded idea stays within budget; an excessive upper bound requires additional
pause evidence, not an automatic claim that the author worked over five hours.
"""
import json
from pathlib import Path
from author_submission_gate import read_operator_file
from iteration_budget import number


class BudgetEvidenceRequired(ValueError):
    """An operator evidence gap, never an instruction to edit or rerun a task."""

    def __init__(self, code, reason, **evidence):
        super().__init__(reason)
        self.code=code;self.reason=reason;self.evidence=evidence

    def handoff(self):
        return dict(state='waiting_platform',stage='verify_iteration_and_budget',
                    failure_owner='platform',error_type=type(self).__name__,
                    error_class=self.code,reason=self.reason,evidence_gap=self.evidence,
                    retry_requires_task_change=False,selftest_rerun_required=False,
                    author_action='Keep the idea ID, original clock, source commit and successful selftest. Wait for operator evidence recovery; do not search platform source code, reset the clock, rename the idea or rerun ACS to repair this gap.',
                    scientific_acceptance=False)


def read_observation(path, role, idea):
    try:return json.loads(read_operator_file(path))
    except FileNotFoundError as error:
        raise BudgetEvidenceRequired('iteration_observation_missing',
            'Operator clock observation is missing; recover the original observation from verified history before review.',
            role=role,idea_id=idea) from error


def merge(intervals, start, end):
    merged=[]
    for lower,upper in sorted(intervals):
        lower=number(lower);upper=number(upper)
        if upper<lower:raise ValueError('Invalid ACS interval ordering')
        lower=max(start,lower);upper=min(end,upper)
        if upper<lower:continue
        if merged and lower<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],upper)
        else:merged.append([lower,upper])
    return merged


def maintenance_intervals(state, role, start, end):
    """Only the isolated operator record can attest maintenance, never a role ledger."""
    state_root=Path(state).resolve().parent
    path=state_root/'maintenance/verified-pauses.json'
    try:record=json.loads(read_operator_file(path))
    except FileNotFoundError:return []
    if (record.get('schema_version')!=1 or record.get('state_root')!=str(state_root)
            or not isinstance(record.get('intervals'),list)):
        raise ValueError('Invalid operator maintenance evidence')
    observed=number(record.get('observed_at'));intervals=[]
    for item in record['intervals']:
        if (not isinstance(item,dict) or item.get('origin')!='operator_confirmed_maintenance'
                or item.get('iteration_paused') is not True or not isinstance(item.get('roles'),list)
                or not all(isinstance(name,str) for name in item['roles'])):
            raise ValueError('Unconfirmed maintenance interval')
        lower=number(item.get('started_at'));upper=number(item.get('finished_at'))
        if not 0<=lower<=upper<=observed:raise ValueError('Invalid maintenance interval chronology')
        if role in item['roles']:intervals.append((lower,upper))
    return merge(intervals,start,end)


def verify(state, role, idea, observation, at, *, completed_at=None):
    at=number(at);end=at if completed_at is None else number(completed_at)
    if end>at:raise ValueError('Completion is in the future')
    observed=read_observation(observation,role,idea)
    if observed.get('role')!=role:raise ValueError('Clock observation role mismatch')
    row=observed.get('ideas',{}).get(idea)
    if not row or row.get('status')=='abandoned':raise ValueError('Missing or abandoned observed idea')
    events=row.get('events')
    if not events or events[0].get('action')!='start':raise ValueError('Missing original observed idea start')
    start=number(events[0]['at']);last=start
    if end<start:raise ValueError('Completion precedes original idea start')
    for event in events:
        instant=number(event['at'])
        if instant<last or instant>at:raise ValueError('Invalid observed idea chronology')
        if event['action']=='abandon':raise ValueError('Idea abandoned')
        last=instant
    first=number(row.get('first_observed_at'));latest=number(observed.get('observed_at'))
    if not start<=first<=latest<=at:raise ValueError('Invalid observation chronology')
    from iteration_observation_recovery import validate_recovery
    validate_recovery(observed,role,idea)
    intervals=[];jobs=[];statuses=[]
    for path in Path(state).glob('*/status.json'):
        status=json.loads(read_operator_file(path,1024*1024))
        if status.get('role')!=role or status.get('idea_id')!=idea:continue
        received=number(status.get('received_at'))
        statuses.append((received,status))
    if statuses:
        received,earliest=min(statuses,key=lambda pair:(pair[0],pair[1]['job_id']))
        if first>received:
            conflict=start>received
            raise BudgetEvidenceRequired(
                'iteration_original_start_conflict' if conflict else 'iteration_observation_history_gap',
                'Idea not observed before its first selftest: '+(
                    'the current recorded start is later than an existing same-idea job. Recover and reconcile the original history; resetting or backdating the clock is not valid evidence.' if conflict else
                    'the observer began after an existing same-idea job. Recover an authentic earlier observation with a matching event prefix; do not infer elapsed time from job counts.'),
                role=role,idea_id=idea,recorded_idea_started_at=start,first_observed_at=first,
                earliest_selftest_job=earliest['job_id'],earliest_selftest_received_at=received)
    for received,status in statuses:
        # Later independent jobs cannot lengthen or refund this completed
        # version's authoring budget, nor make its cleanup uncertain again.
        if received>end:continue
        for trial in status.get('trials',[]):
            if trial.get('cleanup_confirmed') is not True:raise ValueError('ACS cleanup not confirmed for idea')
            lower=number(trial.get('acs_started_at'));upper=number(trial.get('acs_finished_at'))
            if not received<=lower<=upper<=at:raise ValueError('Invalid ACS interval')
            intervals.append((lower,upper))
        jobs.append(status['job_id'])
    if not jobs:raise ValueError('No source-bound selftest for observed idea')
    acs=merge(intervals,start,end)
    maintenance=maintenance_intervals(state,role,start,end)
    excluded=merge([*acs,*maintenance],start,end);refund=sum(b-a for a,b in excluded)
    active=end-start-refund
    return dict(idea_id=idea,active_iteration_seconds_upper_bound=active,
                recorded_idea_started_at=start,observed_before_selftest=True,
                acs_excluded_seconds=sum(b-a for a,b in acs),verified_acs_intervals=acs,
                maintenance_excluded_seconds=sum(b-a for a,b in maintenance),
                verified_maintenance_intervals=maintenance,
                total_excluded_seconds=refund,verified_excluded_intervals=excluded,
                selftest_jobs=sorted(jobs),within_budget=active<=18000,
                iteration_cutoff_at=end,observed_at=latest,
                unverified_pauses_refunded=False,event_origin_attested=False,
                scientific_acceptance=False)
