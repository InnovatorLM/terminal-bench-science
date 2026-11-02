#!/usr/local/bin/python3
"""Persistent author idea clock; operator must attest ACS exclusions at submission.

Role-local logs are author workflow evidence, not trusted execution attestations.
Explicit ACS, platform, and stopped-session intervals can pause active time. Switching to
another idea requires closing the previous idea, and closed ideas stay closed.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
from iteration_budget import LIMIT

EXHAUSTED_REASON = '48_hour_active_iteration_budget_exceeded'

def reconcile_session(state, live, now, observed_end=None):
    """Reconcile an observed runner lifecycle without clearing an idea budget.

    Only pauses created by this hook resume automatically. Explicit ACS/platform
    waits remain paused until the author acknowledges returning to active work.
    No historical interval is silently refunded.
    """
    for idea,row in state.get('ideas',{}).items():
        if live and row['status']=='session_paused' and row.get('automatic_session_pause'):
            transition(state,'resume',idea,now,reason='runner observed live')
            row.pop('automatic_session_pause',None)
        elif live and row['status']=='active':
            transition(state,'status',idea,now)
        elif not live and row['status']=='active':
            end=now if observed_end is None else max(row['last_at'],min(now,observed_end))
            try:
                transition(state,'session-pause',idea,end,reason='runner stopped; automatic lifecycle observation')
            except ValueError:
                if row['status']!='abandoned':raise
            if row['status']=='session_paused':row['automatic_session_pause']=True
    return state

def transition(state, action, idea, now, run_id=None, reason=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', idea):
        raise ValueError('Use a stable lowercase idea ID across branches and sessions')
    if not math.isfinite(now):
        raise ValueError('Invalid clock')
    ideas = state.setdefault('ideas', {})
    if idea not in ideas:
        if action != 'start':
            raise ValueError('Unknown idea')
        if any(row['status'] in ('active', 'acs_wait', 'platform_wait', 'session_paused') for row in ideas.values()):
            raise ValueError('Close the current idea before starting another')
        ideas[idea] = dict(active_seconds=0.0, last_at=now, status='active', events=[])
    row = ideas[idea]
    if now < row['last_at']:
        raise ValueError('Clock moved backwards; operator reconciliation required')
    if row['status'] == 'active':
        row['active_seconds'] += now - row['last_at']
    row['last_at'] = now
    if row['active_seconds'] > LIMIT and row['status'] not in ('finished', 'abandoned'):
        row['status'] = 'abandoned'
        row['reason'] = EXHAUSTED_REASON
        row['events'].append(dict(action='abandon', at=now, run_id=None, reason=EXHAUSTED_REASON))
    if action == 'status':
        return row
    if row['status'] in ('finished', 'abandoned'):
        raise ValueError('Idea is closed; preserve findings and switch direction')
    if action == 'start':
        if row['status'] != 'active':
            raise ValueError('Use resume after ACS waiting')
    elif action == 'acs-wait':
        if row['status'] != 'active' or not run_id or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}', run_id):
            raise ValueError('An actual ACS run ID is required')
        row.update(status='acs_wait', acs_run_id=run_id)
    elif action in ('platform-wait', 'session-pause'):
        if row['status'] != 'active' or not reason:
            raise ValueError('Pause requires an active idea and explicit evidence/reason')
        row.update(status='platform_wait' if action == 'platform-wait' else 'session_paused', pause_reason=reason)
    elif action == 'resume':
        if row['status'] not in ('acs_wait', 'platform_wait', 'session_paused'):
            raise ValueError('Idea is not paused')
        row['status'] = 'active'
    elif action in ('finish', 'abandon'):
        if not reason:
            raise ValueError('Save handoff paths and a reason')
        row.update(status='finished' if action == 'finish' else 'abandoned', reason=reason)
    else:
        raise ValueError('Unknown action')
    row['events'].append(dict(action=action, at=now, run_id=run_id, reason=reason))
    return row

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'acs-wait', 'platform-wait', 'session-pause', 'resume', 'finish', 'abandon'])
    parser.add_argument('idea')
    parser.add_argument('--acs-run-id')
    parser.add_argument('--reason')
    args = parser.parse_args()
    directory = Path('/home/role/.state/author-iteration')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory/'ledger.json'
    with (directory/'clock.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {'ideas': {}}
        error = None
        try:
            row = transition(state, args.action, args.idea, time.time(), args.acs_run_id, args.reason)
        except ValueError as exc:
            error = str(exc)
        pending = path.with_suffix('.pending')
        pending.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        pending.chmod(0o600)
        pending.replace(path)
        if error:
            raise SystemExit(error)
        print(json.dumps(dict(idea=args.idea, remaining_active_seconds=max(0,LIMIT-row['active_seconds']), **row),ensure_ascii=False))

if __name__ == '__main__':
    main()
