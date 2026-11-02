"""Operator-owned clock observations; never copy author notes or command output."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from evaluation_queue import W, atomic_json
from author_submission_gate import read_operator_file
from iteration_budget import number


def observe(directory, role, ledger, live, at):
    directory=Path(directory)
    if not re.fullmatch(r'author-[0-9]+',role) or not directory.resolve().is_relative_to(W):
        raise ValueError('Invalid observation destination')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=directory.lstat()
    if directory.is_symlink() or info.st_uid!=0 or not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077:
        raise ValueError('Observations require operator-private directory')
    # scan_clocks also locks the role ledger. Keep the observation's own
    # read/validate/archive/replace transaction safe for every caller, including
    # an operator recovery tool, rather than relying on that external lock.
    fd=os.open(directory/(role+'.lock'),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        lock=os.fstat(fd)
        if (not stat.S_ISREG(lock.st_mode) or lock.st_uid!=0 or lock.st_nlink!=1
                or lock.st_mode&0o077):
            raise ValueError('Observations require an operator-private lock')
        fcntl.flock(fd,fcntl.LOCK_EX)
        return _observe_locked(directory,role,ledger,live,at)
    finally:os.close(fd)


def _observe_locked(directory, role, ledger, live, at):
    at=number(at);path=directory/(role+'.json')
    previous_raw=read_operator_file(path) if path.exists() else None
    previous=json.loads(previous_raw) if previous_raw else {'role':role,'ideas':{}}
    if previous_raw and at<number(previous['observed_at']):
        raise ValueError('Stale observation cannot rewind the observation boundary')
    ideas=ledger.get('ideas')
    if not isinstance(ideas,dict):raise ValueError('Invalid idea ledger')
    if set(previous['ideas'])-set(ideas):raise ValueError('Previously observed idea removed')
    result={'role':role,'observed_at':at,'runner_live':bool(live),'ideas':{}}
    for idea,row in ideas.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}',idea):raise ValueError('Invalid idea identity')
        events=[]
        for event in row['events']:
            if event.get('action') not in ('start','acs-wait','platform-wait','session-pause','resume','finish','abandon'):
                raise ValueError('Invalid observed event action')
            instant=number(event['at'])
            if instant>at:raise ValueError('Future idea event')
            # Reasons may contain arbitrary role text. Only structural facts persist.
            events.append({'action':event['action'],'at':instant})
        old=previous['ideas'].get(idea)
        if old and events[:len(old['events'])]!=old['events']:
            raise ValueError('Previously observed idea events rewritten')
        if row.get('status') not in ('active','acs_wait','platform_wait','session_paused','finished','abandoned'):
            raise ValueError('Invalid observed idea status')
        result['ideas'][idea]={'events':events,'first_observed_at':old['first_observed_at'] if old else at,
                              'status':row['status']}
    result['previous_observation_sha256']=hashlib.sha256(previous_raw).hexdigest() if previous_raw else None
    if previous.get('recovery_evidence'):result['recovery_evidence']=previous['recovery_evidence']
    # Retain evidence on structural transitions, not on every watchdog poll.
    # Historical first-observed boundaries must survive later migrations.
    if previous_raw and previous['ideas']!=result['ideas']:
        history=directory/'history'/role;history.mkdir(parents=True,exist_ok=True,mode=0o700)
        info=history.lstat()
        if history.is_symlink() or info.st_uid!=0 or not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077:
            raise ValueError('Observation history requires an operator-private directory')
        archive=history/(hashlib.sha256(previous_raw).hexdigest()+'.json')
        if archive.exists():
            if hashlib.sha256(read_operator_file(archive)).hexdigest()!=archive.stem:
                raise ValueError('Observation history digest mismatch')
        else:
            # Preserve original bytes so the filename authenticates the source.
            fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(previous_raw)
    atomic_json(path,result)
    return result
