#!/usr/bin/env python3
"""Out-of-universe observer for trajectory and service health.

This process deliberately runs outside the universe controller.  It only reads
bounded snapshots and writes recommendations; lifecycle changes require an
operator to approve the generated action plan.
"""
import argparse, json, os, re, time
from pathlib import Path

from deployment_paths import universe_root

DEFAULT_ROOT = Path('/mnt/data/zhanglinfeng/workspace')

def tail(path, limit=65536):
    try:
        with path.open('rb') as f:
            f.seek(0, 2); n=f.tell(); f.seek(max(0,n-limit)); data=f.read(limit)
        return data.decode(errors='replace')
    except OSError:
        return ''

def _state_root(root=None):
    """Resolve exactly one deployment; never inspect a guessed old runtime."""
    if root is not None:
        path = Path(root)
        if not path.is_absolute():
            raise ValueError('Deployment root must be absolute')
        return path.resolve() / '.state/gewu-multiverse'
    return universe_root(DEFAULT_ROOT) / '.state/gewu-multiverse'


def scan(root=None):
    state = _state_root(root)
    roles=[]; findings=[]
    if not state.is_dir():
        return {'generated_at':time.time(),'universe_root':str(state),'roles':[],'findings':[{'severity':'high','kind':'deployment_missing','action':'verify GEWU_UNIVERSE_ROOT and deployment'}],'skill_recommendations':[], 'actions':{'mode':'proposal_only','requires_operator_approval':True,'allowed':['hot_update','stop_universe','restart_universe']}}
    for role_dir in sorted(state.glob('author-*')) + sorted(state.glob('evaluator-*')) + sorted(state.glob('curator')):
        runs=sorted(role_dir.glob('run-*'), key=lambda p:p.stat().st_mtime, reverse=True)
        if not runs: continue
        run=runs[0]; text=tail(run/'run.log') + '\n' + tail(run/'transcript.jsonl')
        patterns={
          'credential_exposure': r'(?i)(api[_-]?key|authorization|password|acr.*token)\s*[:=]',
          'infra_error': r'(?i)(rewards=null|ValueError|ImagePullBackOff|network.?policy|NonZeroAgentExitCode)',
          'stalled': r'(?i)(waiting|blocked|timeout|卡住|无法)',
        }
        hits=[name for name,pat in patterns.items() if re.search(pat,text)]
        age=max(0,time.time()-run.stat().st_mtime)
        roles.append({'role':role_dir.name,'run':run.name,'age_seconds':int(age),'signals':hits})
        if 'credential_exposure' in hits:
            findings.append({'severity':'critical','kind':'credential_exposure','role':role_dir.name,'action':'stop_log_publication_and_rotate_credentials'})
        if 'infra_error' in hits:
            findings.append({'severity':'high','kind':'infrastructure_signal','role':role_dir.name,'action':'inspect_recent_verifier_and_pod_evidence'})
        # Waiting/one exception is intentionally insufficient for reset.  This
        # is only a review proposal; ACS waits and the 48-hour idea ledger are
        # excluded from any reset decision.
        if age>3600 and 'stalled' in hits and 'infra_error' in hits:
            findings.append({'severity':'medium','kind':'stalled_role','role':role_dir.name,'action':'request_operator_review_or_reset','reset_eligible':False,'reason':'observer cannot distinguish ACS/platform wait from dead end'})
    skills=[]
    if any(f['kind']=='infrastructure_signal' for f in findings):
        skills.append({'skill':'acs-task-pipeline','recipients':['author-*','evaluator-*','curator'],'reason':'recurring ACS evidence/failure classification'})
    return {'generated_at':time.time(),'universe_root':str(state),'roles':roles,'findings':findings,'skill_recommendations':skills,
            'actions':{'mode':'proposal_only','requires_operator_approval':True,'allowed':['hot_update','stop_universe','restart_universe'],'automatic_role_reset':False}}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--workspace',type=Path,default=DEFAULT_ROOT)
    p.add_argument('--universe-root',type=Path,help='Explicit deployment root; overrides GEWU_UNIVERSE_ROOT')
    p.add_argument('--output',type=Path); p.add_argument('--once',action='store_true'); p.add_argument('--interval',type=int,default=3600)
    a=p.parse_args(); out=a.output or a.workspace/'.state/universe-observer/latest.json'; out.parent.mkdir(parents=True,exist_ok=True)
    while True:
        result=scan(a.universe_root); tmp=out.with_suffix('.json.pending'); tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)); os.replace(tmp,out)
        if a.once: return
        time.sleep(max(60,a.interval))
if __name__=='__main__': main()
