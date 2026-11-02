#!/usr/local/bin/python3
"""Report one exact review generation; never starts cloud jobs."""
import argparse
import json
from pathlib import Path
import time

TERMINAL={'waiting_author','waiting_platform','completed'}
RECOMMENDATIONS={'accept','reject','revise'}

def write(path,value,mode=0o600):
    pending=path.with_suffix('.pending')
    pending.unlink(missing_ok=True)
    with pending.open('x') as output:json.dump(value,output,ensure_ascii=False)
    pending.chmod(mode);pending.replace(path)

def report(home,workspace,state,stage,reason,recommendation=None):
    if recommendation is not None and (state!='completed' or recommendation not in RECOMMENDATIONS):
        raise ValueError('A review recommendation is only valid for a completed review')
    home,workspace=Path(home),Path(workspace)
    assignment=json.loads((home/'review-assignment.json').read_text())
    record=dict(key=assignment['key'],generation=assignment['generation'],state=state,
                stage=stage,reason=reason,updated_at=time.time())
    if state=='completed':record['review_recommendation']=recommendation or 'unrecorded'
    terminal=home/'review-terminal-status.json'
    terminal_record=False
    if terminal.is_file() and not terminal.is_symlink():
        previous=json.loads(terminal.read_text())
        if (previous.get('key')==record['key'] and previous.get('generation')==record['generation']
                and previous.get('state') in TERMINAL):
            record=previous
            terminal_record=True
    if not terminal_record:
        if state=='waiting_platform' and stage in ('rollouts','five_rollouts','acs_rollouts'):
            from role_evaluation_batch import summary
            evaluation=home.parents[1]/'evaluation'
            batch=summary(assignment,evaluation/'results',evaluation/'inbox')
            if not batch['batch_submitted']:
                raise ValueError('Batch has not been submitted. Run role-evaluation-batch submit before waiting. '
                                 'For known task defects, report waiting_author instead.')
            record['stage']='five_rollouts'
    write(home/'review-status.json',record)
    if record['state'] in TERMINAL:write(terminal,record)
    activity={k:record[k] for k in ('key','generation','state','stage','updated_at')}
    write(home/'activity-receipt.json',dict(activity,receipt_type='evaluator-activity-v1'))
    # This non-secret mirror closes the dispatcher polling race. The private
    # role home remains inaccessible to the controller.
    write(workspace/'.gewu-review-status.json',record,0o644)
    return record

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state',choices=['reviewing',*sorted(TERMINAL)])
    parser.add_argument('--stage',required=True);parser.add_argument('--reason',required=True)
    parser.add_argument('--recommendation',choices=sorted(RECOMMENDATIONS),
                        help='Explicit evaluator recommendation for a completed review; this is not formal acceptance')
    args=parser.parse_args()
    print(json.dumps(report('/home/role','/workspace',args.state,args.stage,args.reason,
                            args.recommendation),ensure_ascii=False))

if __name__=='__main__':main()
