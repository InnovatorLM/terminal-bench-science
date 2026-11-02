"""Admit one completed selftest with an unrecoverable legacy clock gap.

This records the unknown duration; it never edits the author clock or certifies
that the iteration was within budget. Run only after inspecting the named job.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from author_submission_gate import read_operator_file
from evaluation_queue import atomic_json
from selftest_budget import BudgetEvidenceRequired, verify
from selftest_budget_admission import IDENTITY, LEGACY_GAPS, read_audit
from selftest_review_supplement import effective_request


def record(work, state, observation, earliest_job, role, idea, *, reviewed=False):
    if not reviewed:
        raise ValueError('Explicit operator review required')
    work, state, observation = Path(work), Path(state), Path(observation)
    status_raw = read_operator_file(work/'status.json')
    status = json.loads(status_raw)
    request, _ = effective_request(work)
    if (work.parent != state or work.name != status.get('job_id')
            or status.get('role') != role or status.get('idea_id') != idea
            or status.get('status') != 'selftest_passed'
            or status.get('source_git_tree_verified') is not True
            or not request.get('request_review') or not request.get('iteration')):
        raise ValueError('Only an exact completed, requested version can be audited')
    handoff = json.loads(read_operator_file(work/'handoff.json'))
    if (handoff.get('state') != 'waiting_platform'
            or handoff.get('error_class') not in LEGACY_GAPS
            or handoff.get('evidence_gap', {}).get('earliest_selftest_job') != earliest_job):
        raise ValueError('Current handoff does not name this historical clock gap')
    first_raw = read_operator_file(state/earliest_job/'status.json')
    first = json.loads(first_raw)
    if (first.get('role') != role or first.get('idea_id') != idea
            or first.get('job_id') != earliest_job
            or first.get('received_at') != handoff['evidence_gap']['earliest_selftest_received_at']
            or first['received_at'] >= status['received_at']):
        raise ValueError('Earliest same-idea job does not match the handoff')
    prior = request['iteration']['previous_job']
    prior_raw = read_operator_file(state/prior/'status.json')
    prior_status = json.loads(prior_raw)
    if (prior_status.get('role') != role or prior_status.get('idea_id') != idea
            or prior_status.get('received_at', float('inf')) >= status['received_at']):
        raise ValueError('Iteration predecessor does not match the completed job')
    observation_raw = read_operator_file(observation)
    observed = json.loads(observation_raw)
    row = observed.get('ideas', {}).get(idea, {})
    if (observed.get('role') != role or row.get('status') == 'abandoned'
            or any(e.get('action') == 'abandon' for e in row.get('events', []))):
        raise ValueError('Current idea observation is missing or abandoned')
    try:
        verify(state, role, idea, observation, time.time(), completed_at=status['completed_at'])
    except BudgetEvidenceRequired as error:
        if error.code != handoff['error_class']:
            raise ValueError('Clock gap changed since the handoff') from error
    else:
        raise ValueError('The completed version has no legacy clock gap')

    audit_path = work/'legacy-budget-admission.json'
    if audit_path.exists():
        read_audit(audit_path, status, handoff['error_class'])
        return audit_path
    evidence = []
    for label, raw in (('observation', observation_raw), ('earliest-job', first_raw),
                       ('previous-job', prior_raw)):
        path = work/('legacy-budget-evidence-'+label+'.json')
        if path.exists():
            if read_operator_file(path) != raw:
                raise ValueError('Existing legacy evidence differs')
        else:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        evidence.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest()))
    audit = dict(schema_version=1, kind='legacy_completed_budget_unknown',
                 operator_reviewed=True, scope='completed_frozen_version_only',
                 budget_error_code=handoff['error_class'],
                 reason='Original operator clock history cannot establish active iteration time; this completed version may proceed to independent review with duration unknown.',
                 completed_at=status['completed_at'], audited_at=time.time(),
                 status_file=str(work/'status.json'),
                 status_sha256=hashlib.sha256(status_raw).hexdigest(),
                 evidence_files=evidence,
                 **{key: status[key] for key in IDENTITY})
    atomic_json(audit_path, audit)
    read_audit(audit_path, status, handoff['error_class'])
    return audit_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('work', 'state', 'observation'):
        parser.add_argument('--'+name, type=Path, required=True)
    for name in ('earliest-job', 'role', 'idea'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--reviewed', action='store_true')
    args = parser.parse_args()
    print(record(args.work, args.state, args.observation, args.earliest_job,
                 args.role, args.idea, reviewed=args.reviewed))


if __name__ == '__main__':
    main()
