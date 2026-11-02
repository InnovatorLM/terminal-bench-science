"""Prepare a clock recovery proposal from genuine, matching operator history.

This command never changes a live clock. The operator must review its output and
apply it while the observer is stopped. A later/reset start is a conflict, not a
reason to invent a new 48-hour budget or stitch unrelated event sequences.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from author_submission_gate import read_operator_file
from evaluation_queue import W, atomic_json
from iteration_budget import number
from selftest_budget import BudgetEvidenceRequired


def proposal(current, historical, role, idea):
    paths=[Path(current),Path(historical)]
    raw=[read_operator_file(path) for path in paths]
    latest,previous=[json.loads(value) for value in raw]
    if any(value.get('role')!=role for value in (latest,previous)):
        raise ValueError('Clock recovery role mismatch')
    now=latest.get('ideas',{}).get(idea);old=previous.get('ideas',{}).get(idea)
    if not now or not old:raise ValueError('Clock recovery requires the same observed idea')
    for observed,row in [(latest,now),(previous,old)]:
        events=row.get('events')
        if not events or events[0].get('action')!='start':raise ValueError('Missing original observed start')
        start=number(events[0]['at']);instant=start
        for event in events:
            when=number(event['at'])
            if when<instant or event.get('action')=='abandon':raise ValueError('Invalid or abandoned recovery history')
            instant=when
        if not start<=number(row['first_observed_at'])<=number(observed['observed_at']):
            raise ValueError('Invalid recovery observation chronology')
        if instant>number(observed['observed_at']):raise ValueError('Future recovery event')
        if row.get('status')=='abandoned':raise ValueError('Abandoned idea cannot be recovered')
    if number(previous['observed_at'])>number(latest['observed_at']):
        raise ValueError('Recovery history must precede the current observation')
    if now['events'][:len(old['events'])]!=old['events']:
        raise BudgetEvidenceRequired('iteration_historical_prefix_conflict',
            'Historical and current clock events conflict. Operator must reconcile the original history; no budget reset, backdating or event stitching is permitted.',
            role=role,idea_id=idea,historical_idea_started_at=old['events'][0]['at'],
            current_idea_started_at=now['events'][0]['at'])
    if old['first_observed_at']>now['first_observed_at']:
        raise ValueError('Historical evidence does not improve the observation boundary')
    result=copy.deepcopy(latest)
    result['ideas'][idea]['first_observed_at']=old['first_observed_at']
    evidence=[dict(path=str(path),sha256=hashlib.sha256(value).hexdigest()) for path,value in zip(paths,raw)]
    result.setdefault('recovery_evidence',{})[idea]=dict(
        origin='matching_operator_observation_prefix',sources=[evidence[1]],
        replaced_observation_sha256=evidence[0]['sha256'],
        original_started_at_preserved=True,unverified_pauses_refunded=False)
    return dict(schema_version=1,state='operator_recovery_proposal',role=role,idea_id=idea,
                live_state_modified=False,original_started_at=now['events'][0]['at'],
                current_observation_sha256=evidence[0]['sha256'],
                candidate_observation=result,scientific_acceptance=False)


def validate_recovery(observed, role, idea):
    """A recovered boundary remains backed by the original operator snapshot."""
    proof=observed.get('recovery_evidence',{}).get(idea)
    if not proof:return
    if (proof.get('origin')!='matching_operator_observation_prefix'
            or proof.get('original_started_at_preserved') is not True
            or proof.get('unverified_pauses_refunded') is not False
            or len(proof.get('sources',[]))!=1):
        raise ValueError('Invalid clock recovery proof')
    source=proof['sources'][0]
    try:raw=read_operator_file(source['path'])
    except FileNotFoundError as error:
        raise BudgetEvidenceRequired('iteration_recovery_source_missing',
            'The authentic historical observation backing clock recovery is missing; restore that snapshot before review.',
            role=role,idea_id=idea) from error
    if hashlib.sha256(raw).hexdigest()!=source['sha256']:raise ValueError('Clock recovery source digest mismatch')
    history=json.loads(raw);old=history.get('ideas',{}).get(idea)
    row=observed['ideas'][idea]
    if (history.get('role')!=role or not old or old.get('status')=='abandoned'
            or row['events'][:len(old['events'])]!=old['events']
            or row['first_observed_at']!=old['first_observed_at']):
        raise ValueError('Clock recovery no longer matches its historical prefix')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current',type=Path,required=True)
    parser.add_argument('--historical',type=Path,required=True)
    parser.add_argument('--role',required=True)
    parser.add_argument('--idea',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if (not args.output.is_absolute() or not args.output.resolve().is_relative_to(W)
            or args.output.exists() or args.output.is_symlink()):
        raise ValueError('Use a new proposal file under the workspace; never overwrite a live clock')
    atomic_json(args.output,proposal(args.current,args.historical,args.role,args.idea))


if __name__=='__main__':main()
