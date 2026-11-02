"""Interrupt overlong local role commands; preserve the agent and its transcript."""
import argparse,json,os,signal,time
from pathlib import Path
W=Path('/mnt/data/zhanglinfeng/workspace')
CONFIG=W/'.state/science-role-broker/config.json'
STATE=W/'.state/science-command-watchdog'

def process_info(pid):
    p=Path('/proc')/str(pid)
    raw=(p/'stat').read_text(); fields=raw[raw.rfind(')')+2:].split()
    return dict(pid=int(pid),uid=p.stat().st_uid,state=fields[0],pgid=int(fields[2]),
                start_ticks=int(fields[19]),comm=(p/'comm').read_text().strip())

def owner_alive(expected):
    if expected is None:return True
    try:
        current=process_info(expected['pid'])
        return current['state']!='Z' and all(current[k]==expected[k] for k in ('uid','start_ticks'))
    except (OSError,ValueError,IndexError):return False

def overdue(info,uptime,hz,limit):
    return info['state']!='Z' and info['pid']==info['pgid'] and info['comm']=='bash' and uptime-info['start_ticks']/hz>=limit

def stop_verified(expected, sig=signal.SIGTERM):
    try:
        fd=os.pidfd_open(expected['pid'])
        try:
            now=process_info(expected['pid'])
            if (now['uid'],now['start_ticks'],now['pgid'])!=(expected['uid'],expected['start_ticks'],expected['pgid']):return False
            signal.pidfd_send_signal(fd,sig)
            return True
        finally:os.close(fd)
    except (FileNotFoundError,ProcessLookupError):return False

def stop_command_group(leader, members, grace=2):
    targets=[*members,leader]
    terminated=sum(stop_verified(member) for member in targets)
    deadline=time.monotonic()+grace
    while time.monotonic()<deadline:
        survivors=[]
        for member in targets:
            try:
                current=process_info(member['pid'])
                if current['state']!='Z' and (current['uid'],current['start_ticks'],current['pgid'])==(member['uid'],member['start_ticks'],member['pgid']):survivors.append(member)
            except (FileNotFoundError,ProcessLookupError):pass
        if not survivors:return dict(term_signals=terminated,kill_signals=0)
        time.sleep(.05)
    return dict(term_signals=terminated,kill_signals=sum(stop_verified(member,signal.SIGKILL) for member in survivors))

def scan(config,limit=3600):
    hz=os.sysconf('SC_CLK_TCK');uptime=float(Path('/proc/uptime').read_text().split()[0]);roles={v['uid']:name for name,v in config['roles'].items()}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:info=process_info(p.name)
        except (OSError,ValueError,IndexError):continue
        if info['uid'] not in roles or not overdue(info,uptime,hz,limit):continue
        role=roles[info['uid']];key=str(info['pid'])+'-'+str(info['start_ticks']);record=STATE/(key+'.json')
        if record.exists():continue
        payload=dict(role=role,**info,elapsed_seconds=uptime-info['start_ticks']/hz,limit_seconds=limit,at=time.time(),reason='local_command_exceeded_one_hour')
        # No argv/environment/stdout is copied into operator diagnostics.
        record.write_text(json.dumps(payload,indent=2));record.chmod(0o600)
        notice=Path(config['roles'][role]['root'])/'workspace/COMMAND_WATCHDOG.json'
        notice.write_text(json.dumps(dict(reason=payload['reason'],at=payload['at'],elapsed_seconds=payload['elapsed_seconds'],guidance='Your local command exceeded one hour and was interrupted. Inspect partial artifacts; estimate complexity, use a small input or checkpoint, and split or optimize the computation. This is not a model-answer failure. ACS evaluation deadlines remain four hours.'),indent=2))
        os.chown(notice,info['uid'],info['uid']);notice.chmod(0o600)
        # Stop descendants too: an orphan retaining stdout would keep the broker waiting.
        children=[]
        for child in Path('/proc').iterdir():
            if not child.name.isdigit():continue
            try: member=process_info(child.name)
            except (OSError,ValueError,IndexError):continue
            if member['uid']==info['uid'] and member['pgid']==info['pgid'] and member['pid']!=info['pid']:children.append(member)
        payload['termination']=stop_command_group(info,children)
        record.write_text(json.dumps(payload,indent=2));record.chmod(0o600)
        print(json.dumps(payload),flush=True)

def main():
    import fcntl
    global CONFIG,STATE
    from deployment_paths import universe_root
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--config',type=Path,default=CONFIG);p.add_argument('--state',type=Path,default=STATE);p.add_argument('--session-state',type=Path,default=universe_root()/'.state/gewu-multiverse');p.add_argument('--owner-pid',type=int);a=p.parse_args()
    CONFIG,STATE=a.config,a.state
    owner=process_info(a.owner_pid) if a.owner_pid is not None else None
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (STATE/'watchdog.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from iteration_budget import LIMIT
        print(json.dumps(dict(event='watchdog_started',pid=os.getpid(),budget_seconds=LIMIT,owner_pid=a.owner_pid)),flush=True)
        while owner_alive(owner):
            config=json.loads(CONFIG.read_text())
            scan(config)
            from iteration_lifecycle import scan_clocks
            observations=STATE/'iteration-observations'
            scan_clocks(config,state_root=a.session_state,observation_root=observations)
            from author_budget_watchdog import scan as scan_author_budgets
            scan_author_budgets(config,a.session_state,observations,STATE/'budget-resets')
            from dead_end_watchdog import scan as scan_dead_ends
            scan_dead_ends(config, a.session_state, STATE/'dead-end-observations')
            if a.once:return
            time.sleep(30)
if __name__=='__main__':main()
