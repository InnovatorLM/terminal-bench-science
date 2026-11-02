"""Reuse a formal acceptance only for the exact same author/commit/task/bytes."""
import hashlib
import json
from pathlib import Path

from author_submission_gate import read_operator_file
from formal_review_decision import name_for, validate
from selftest_review_supplement import immutable_json


def lookup(status, universe):
    if status.get('status')!='selftest_passed' or status.get('source_git_tree_verified') is not True:
        return None
    state=Path(universe)/'.state/gewu-multiverse/review-dispatcher'
    path=state/'ledger.json'
    if not path.exists():return None
    ledger=json.loads(read_operator_file(path))
    matching=[dict(row,key=key) for key,row in ledger.items()
              if row.get('commit')==status.get('source_commit')
              and row.get('task')==status.get('source_task_path')
              and row.get('snapshot_sha256')==status.get('snapshot_sha256')
              and row.get('author_role')==status.get('role')]
    if len(matching)>1:raise ValueError('Ambiguous completed review identity')
    if not matching:return None
    row=matching[0]
    if (row.get('state')!='completed' or row.get('scientific_acceptance') is not True
            or row.get('formal_decision')!='accepted'):
        return None
    receipt=state/'formal-decisions'/name_for(row['key'])
    raw=read_operator_file(receipt);sha=hashlib.sha256(raw).hexdigest()
    validate(row,json.loads(raw))
    if row.get('formal_decision_sha256')!=sha:
        raise ValueError('Formal acceptance receipt digest changed')
    return dict(state='completed',stage='previously_accepted_version',
                review_key=row['key'],review_generation=row['generation'],
                evaluator=row['role'],evaluation_id=row['evaluation_id'],
                snapshot_sha256=row['snapshot_sha256'],formal_decision='accepted',
                formal_decision_sha256=sha,scientific_acceptance=True,
                acceptance_scope='completed_frozen_version_only',
                review_recommendation='accept',selftest_rerun_required=False,
                reason='This exact author, commit, task and snapshot already have a formal acceptance. '
                       'Reuse the existing review; no new qualification, ACS run or evaluator assignment is needed. '
                       'Coordinate handoff of the accepted version with curator, then share lessons and move to the next idea. '
                       'The original review and its evidence limitations remain unchanged.')


def record(work, status, result):
    work=Path(work)
    value=dict(schema_version=1,job_id=status['job_id'],role=status['role'],
               source_commit=status['source_commit'],source_task_path=status['source_task_path'],
               source_status_sha256=hashlib.sha256(read_operator_file(work/'status.json')).hexdigest(),
               original_acceptance=result)
    if result.get('reused_review_feedback') is True:
        # A later reviewer update is a new receipt, never a mutation of history.
        value['original_review']=value.pop('original_acceptance')
        directory=work/'review-feedback-reuse';directory.mkdir(mode=0o700,exist_ok=True)
        path=directory/(result['review_feedback_sha256']+'.json')
    else:path=work/'completed-review-reuse.json'
    if path.exists():
        if json.loads(read_operator_file(path))!=value:
            raise ValueError('Completed review reuse evidence changed')
    else:immutable_json(path,value)


def lookup_feedback(status,universe):
    """An unchanged task retains its review defects after a repeated self-test."""
    if status.get('status')!='selftest_passed' or status.get('source_git_tree_verified') is not True:
        return None
    path=Path(universe)/'.state/gewu-multiverse/review-dispatcher/ledger.json'
    if not path.exists():return None
    ledger=json.loads(read_operator_file(path))
    matching=[dict(row,key=key) for key,row in ledger.items()
              if row.get('commit')==status.get('source_commit')
              and row.get('task')==status.get('source_task_path')
              and row.get('snapshot_sha256')==status.get('snapshot_sha256')
              and row.get('author_role')==status.get('role')]
    if len(matching)>1:raise ValueError('Ambiguous existing review identity')
    if not matching:return None
    row=matching[0]
    if (row.get('state')!='waiting_author' and not (
            row.get('state')=='completed' and row.get('review_recommendation') in ('reject','revise'))):
        return None
    if not row.get('last_activity_at') or not row.get('reason') or not row.get('role','').startswith('evaluator-'):
        return None
    proof={key:row.get(key) for key in ('key','commit','task','snapshot_sha256','evaluation_id',
                                       'role','generation','state','stage','reason','last_activity_at',
                                       'review_recommendation','job_id')}
    return dict(state=row['state'],stage=row['stage'],reason=row['reason'],
                review_key=row['key'],review_generation=row['generation'],evaluator=row['role'],
                evaluation_id=row['evaluation_id'],snapshot_sha256=row['snapshot_sha256'],
                reviewed_job_id=row.get('job_id'),review_recommendation=row.get('review_recommendation','revise'),
                reused_review_feedback=True,review_feedback_sha256=hashlib.sha256(
                    json.dumps(proof,sort_keys=True).encode()).hexdigest(),
                scientific_acceptance=False,selftest_rerun_required=False,
                next_action='The same immutable source already has evaluator feedback. Address the recorded '
                            'task defects or difficulty rejection; another unchanged self-test does not erase '
                            'that review and needs no new budget admission, cloud run or review assignment.')
