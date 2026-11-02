"""Attest a stopped recovery interval; never infer pauses from author claims."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from author_submission_gate import read_operator_file
from author_iteration_clock import reconcile_session
from deployment_paths import WORKSPACE
from iteration_budget import number
from prepare_repaired_runtime import replace_private_json
from role_exec import role_processes


def record(runtime, proof, *, continuity_confirmed, pause_clocks=False, now=None):
    if os.geteuid()!=0 or not continuity_confirmed:
        raise PermissionError('Root operator must confirm no authoring sessions during maintenance')
    runtime=Path(runtime).resolve(strict=True)
    if not runtime.is_relative_to(WORKSPACE) or runtime==WORKSPACE:
        raise ValueError('Maintenance target must be a workspace runtime')
    raw=read_operator_file(proof);source=json.loads(raw)
    if (source.get('runtime')!=str(runtime) or source.get('production_started') is not False
            or source.get('status')!='software_ready_payload_pending'
            or not isinstance(source.get('roles'),dict) or not source['roles']):
        raise ValueError('Require the completed, stopped software migration observation')
    started=number(source.get('started_at'));now=number(time.time() if now is None else now)
    if not 0<=started<=now:
        raise ValueError('Invalid first observed maintenance time')
    for role,entry in source['roles'].items():
        if role_processes(entry['uid']):raise RuntimeError('Role process still active: '+role)
    state=runtime/'.state/gewu-multiverse';path=state/'maintenance/verified-pauses.json'
    receipt=json.loads(read_operator_file(path)) if path.exists() else {
        'schema_version':1,'state_root':str(state),'intervals':[]}
    if receipt.get('state_root')!=str(state) or receipt.get('schema_version')!=1:
        raise ValueError('Maintenance evidence belongs to another runtime')
    identity=hashlib.sha256((str(runtime)+':'+str(started)).encode()).hexdigest()
    interval=dict(id=identity,origin='operator_confirmed_maintenance',iteration_paused=True,
                  roles=sorted(source['roles']),started_at=started,finished_at=now,
                  first_stopped_observation=str(Path(proof)),source_proof_sha256=hashlib.sha256(raw).hexdigest(),
                  scope='No authoring/model sessions; operator capability/import/read probes are maintenance.',
                  earlier_unverified_downtime_refunded=False)
    old=next((item for item in receipt['intervals'] if item.get('id')==identity),None)
    if old and old['finished_at']>now:raise ValueError('Maintenance endpoint cannot move backwards')
    receipt['intervals']=[item for item in receipt['intervals'] if item.get('id')!=identity]+[interval]
    receipt['observed_at']=now
    replace_private_json(path,receipt)
    paused=[]
    if pause_clocks:
        for role,entry in source['roles'].items():
            if not role.startswith('author-'):continue
            clock=runtime/'.config/gewu-multiverse/agents'/role/'home/role/.state/author-iteration/ledger.json'
            if not clock.exists():continue
            if clock.resolve()!=clock or any(p.is_symlink() for p in clock.parents if p.is_relative_to(runtime)):
                raise ValueError('Linked role clock refused')
            info=clock.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0,entry['uid']) or info.st_mode&0o022:
                raise ValueError('Untrusted role clock path or ownership')
            ledger=json.loads(clock.read_text())
            previous={name:item['status'] for name,item in ledger.get('ideas',{}).items()}
            # The first proven stop is the earliest supported endpoint. Never
            # refund the older, unobserved part or reset an original idea start.
            reconcile_session(ledger,False,now,observed_end=started)
            replace_private_json(clock,ledger);os.chown(clock,info.st_uid,info.st_gid)
            for idea,item in ledger.get('ideas',{}).items():
                if previous[idea]!=item['status']:
                    paused.append({'role':role,'idea':idea,'before':previous[idea],'after':item['status']})
    result={'status':'operator_maintenance_recorded','interval':interval,
            'role_clock_transitions':paused,'automatic_resume':'Only the runner observing a real active session resumes automatic pauses.',
            'historical_unverified_pauses_refunded':False,'production_started':False}
    replace_private_json(state/'maintenance/latest.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime',type=Path,required=True);p.add_argument('--proof',type=Path,required=True)
    p.add_argument('--operator-confirm-no-authoring-sessions',action='store_true')
    p.add_argument('--pause-clocks',action='store_true')
    args=p.parse_args()
    result=record(args.runtime,args.proof,continuity_confirmed=args.operator_confirm_no_authoring_sessions,
                  pause_clocks=args.pause_clocks)
    print(json.dumps({'status':result['status'],'started_at':result['interval']['started_at'],
                      'finished_at':result['interval']['finished_at'],'clock_transitions':len(result['role_clock_transitions'])}))


if __name__=='__main__':main()
