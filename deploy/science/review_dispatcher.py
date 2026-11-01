"""Single-writer PR review dispatch. Local Forgejo only; no solver/cloud actions."""
import argparse,fcntl,hashlib,json,os,re,time,urllib.request
from pathlib import Path
W=Path('/mnt/data/zhanglinfeng/workspace')
STATE=W/'.state/science-review-dispatcher'
REPO='/repos/team/terminal-bench-science'
MARK='<!-- exclusive-review-v1 -->'
STATES={'reviewing','waiting_author','waiting_platform','completed'}

def atomic(path,data):
    stage=path.with_suffix('.pending');stage.write_text(json.dumps(data,ensure_ascii=False,indent=2));stage.chmod(0o600);stage.replace(path)

def fingerprint(pr,comments):
    # Evaluator self-comments never wake their own waiting task.
    relevant=[(c['id'],c.get('updated_at')) for c in comments if not c.get('user',{}).get('login','').startswith('gewu-evaluator')]
    return hashlib.sha256(json.dumps([pr['head']['sha'],relevant],sort_keys=True).encode()).hexdigest()

def registered_execution(commit, task):
    from deployment_paths import universe_root
    from evaluation_queue import root_file, digest_tree
    queue=json.loads(root_file(universe_root(W)/'.config/science-evaluation/queue.json').read_text())
    registry=json.loads(root_file(queue['registry']).read_text())
    matches=[]
    for identifier,entry in registry.items():
        if (entry.get('enabled',True) is True and entry.get('dispatch','solver')=='solver'
                and entry.get('source_commit')==commit and entry.get('source_task_path')==task):
            if digest_tree(entry['snapshot'])!=entry.get('snapshot_sha256'):
                raise ValueError('Registered task changed')
            matches.append(dict(evaluation_id=identifier,snapshot_sha256=entry['snapshot_sha256']))
    if len(matches)>1:raise ValueError('Ambiguous registered execution for author version')
    return matches[0] if matches else None

def author_for_pr(pr, config):
    login=str((pr.get('user') or {}).get('login',''))
    match=re.search(r'author[-_]?([0-9]{2,4})$', login) or re.search(r'author[-_]?([0-9]{2,4})',str(pr.get('body') or ''))
    if match:
        name='author-'+match.group(1)
        if name in config['roles']: return name
    commit=str((pr.get('head') or {}).get('sha',''))
    task=str(pr.get('body') or '')
    for name,entry in config['roles'].items():
        if not name.startswith('author-'): continue
        root=Path(entry['root'])
        for candidate in (root/'selftest/results').glob('*.json'):
            try:
                data=json.loads(candidate.read_text())
                if data.get('source_commit')==commit or data.get('commit')==commit or (data.get('source_task_path') and data['source_task_path'] in task):
                    return name
            except (OSError,ValueError,TypeError):
                continue
    return None

def publish_author_feedback(pr, row, config):
    author=author_for_pr(pr,config)
    if not author: return
    root=Path(config['roles'][author]['root'])
    payload={'key':str(row.get('pr'))+':'+str(row.get('commit')),'generation':row.get('generation',0),
             'state':row.get('state'),'stage':row.get('stage'),'reason':row.get('reason',''),
             'task':row.get('task'),'evaluator':row.get('role'),
             # The author is paused only after an evaluator owns the exact
             # immutable version. waiting_platform means the author must still
             # supply the missing registration/self-test evidence.
             'author_must_pause':row.get('state')=='reviewing','updated_at':time.time()}
    atomic(root/'review-feedback.json',payload); (root/'review-feedback.json').chmod(0o644)

class Api:
    def __init__(self,token,base_url=None):self.token=token;self.base_url=base_url
    def call(self,method,path,data=None):
        base=(self.base_url or os.environ.get('GEWU_FORGEJO_URL','http://127.0.0.1:3000')).rstrip('/')
        req=urllib.request.Request(base+'/api/v1'+REPO+path,data=json.dumps(data).encode() if data is not None else None,method=method,headers={'Authorization':'token '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
    def pages(self,path):
        out=[]
        for page in range(1,1001):
            batch=self.call('GET',path+('&' if '?' in path else '?')+'limit=50&page='+str(page))
            if not batch:return out
            out+=batch
            # Forgejo may spend a long time answering pages beyond the end of
            # a large repository. A short page is authoritative termination.
            if len(batch) < 50:
                return out
        raise ValueError('Pagination did not terminate')

def reconcile(api,config,ledger):
    pulls=api.pages('/pulls?state=open')
    pending={}
    for pr in pulls:
        if pr.get('draft') or 'draft' in pr['title'].lower():continue
        match=re.search(r'tasks/[a-zA-Z0-9_./-]+',pr.get('body') or '')
        if not match:continue
        task=match.group(0).rstrip('./');key=str(pr['number'])+':'+pr['head']['sha']
        from author_submission_gate import approved
        gate_error=None
        try:eligible=approved(pr['head']['sha'],task)
        except (OSError,ValueError,KeyError,TypeError) as error:
            eligible=False;gate_error=type(error).__name__
        if not eligible:
            pending[key]=dict(pr=pr['number'],commit=pr['head']['sha'],task=task,
                state='waiting_author',stage='author_container_preflight',
                reason='Container selftest and result-driven iteration evidence required before evaluator assignment',
                evaluator_assigned=False)
            if gate_error:pending[key]['validation_error_type']=gate_error
            publish_author_feedback(pr,pending[key],config)
            old=ledger.get(key)
            if old and old.get('issue'):
                api.call('PATCH','/issues/'+str(old['issue']),{'state':'closed'})
                old.update(state='waiting_author',stage='author_container_preflight',reason='Author pre-submission container checks and iteration evidence required')
                wake=Path(config['roles'][old['role']]['root'])/'review-dispatch.json'
                atomic(wake,dict(key=key,generation=old['generation'],state=old['state']));wake.chmod(0o644)
            continue
        try:execution=registered_execution(pr['head']['sha'],task)
        except (OSError,ValueError,KeyError,TypeError):execution=None
        if execution is None:
            pending[key]=dict(pr=pr['number'],commit=pr['head']['sha'],task=task,
                state='waiting_platform',stage='immutable_execution_registration',
                reason='Exact author version requires a registered immutable solver execution',evaluator_assigned=False)
            publish_author_feedback(pr,pending[key],config)
            continue
        comments=api.pages('/issues/'+str(pr['number'])+'/comments');event=fingerprint(pr,comments)
        if any(v['pr']==pr['number'] and k!=key and v['state']=='reviewing' for k,v in ledger.items()):
            continue  # Finish or park prior revision before assigning the new one.
        row=ledger.get(key)
        if row is None:
            busy={v['role'] for v in ledger.values() if v['state']=='reviewing'}
            roles=[n for n in config['roles'] if n.startswith('evaluator-') and n not in busy]
            if not roles:continue
            role=sorted(roles)[0]
            row={'pr':pr['number'],'commit':pr['head']['sha'],'task':task,**execution,'snapshot_kind':'registered-task-content-sha256','role':role,'state':'reviewing','stage':'completeness','event':event,'generation':0,'issue':None,'updated_at':time.time()}
            ledger[key]=row;atomic(STATE/'ledger.json',ledger)
        elif any(row.get(k)!=execution[k] for k in ('evaluation_id','snapshot_sha256')):
            # Never silently change an active assignment to a different image/task.
            pending[key]=dict(pr=pr['number'],state='waiting_platform',stage='assignment_binding_mismatch',evaluator_assigned=False)
            continue
        root=Path(config['roles'][row['role']]['root']);receipt=root/'home/role/review-status.json'
        if receipt.exists():
            try:
                info=receipt.lstat();status=json.loads(receipt.read_text())
                if info.st_uid==config['roles'][row['role']]['uid'] and not receipt.is_symlink() and status.get('key')==key and status.get('generation')==row['generation'] and status.get('state') in STATES:
                    row.update(state=status['state'],stage=str(status.get('stage',''))[:100],reason=str(status.get('reason',''))[:2000],updated_at=time.time())
            except (OSError,ValueError):pass
        if row['state'] in ('waiting_author','waiting_platform','completed') and row.get('issue'):
            issue=api.call('GET','/issues/'+str(row['issue']))
            if issue['state']=='open':api.call('PATCH','/issues/'+str(row['issue']),{'state':'closed'})
        if row['state'] in ('waiting_author','waiting_platform') and event!=row['event']:
            if not any(v['state']=='reviewing' and v['role']==row['role'] for k,v in ledger.items() if k!=key):
                row.update(state='reviewing',event=event,generation=row['generation']+1,issue=None)
        wake=root/'review-dispatch.json'
        if row['state']!='reviewing':
            atomic(wake,dict(key=key,generation=row['generation'],state=row['state']));wake.chmod(0o644)
            publish_author_feedback(pr,row,config)
            continue
        marker=MARK+' '+key+' generation='+str(row['generation'])
        if not row.get('issue'):
            existing=[i for i in api.pages('/issues?state=all&type=issues') if marker in (i.get('body') or '')]
            if len(existing)>1:raise ValueError('Duplicate review issue')
            if existing:issue=existing[0]
            else:
                body=marker+chr(10)+json.dumps(row,ensure_ascii=False,indent=2)+chr(10)+'''Only this evaluator owns this exact version. Resume saved stages; do not re-review unchanged evidence. Start with completeness, report defects on the PR, then use role-review-status to enter waiting_author/waiting_platform/completed and exit. Never infer another task from recent branches. Five solver rollouts are subtasks of this review, not five evaluator assignments. Do not claim acceptance without evidence. No automatic failover while solver cleanup is unknown.'''
                issue=api.call('POST','/issues',{'title':'[独占评测] PR #'+str(row['pr'])+' '+row['commit'][:8],'body':body,'assignees':['gewu-'+row['role']]})
            row['issue']=issue['number']
        assignment=root/'home/role/review-assignment.json';atomic(assignment,dict(row,key=key));os.chown(assignment,config['roles'][row['role']]['uid'],config['roles'][row['role']]['gid'])
        atomic(wake,dict(key=key,generation=row['generation'],state=row['state']));wake.chmod(0o644)
        publish_author_feedback(pr,row,config)
    atomic(STATE/'pending-submissions.json',pending)
    atomic(STATE/'ledger.json',ledger)

def main():
    from controller_credentials import load_controller_credentials
    global STATE
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--config',type=Path,default=W/'.state/science-role-broker/config.json');p.add_argument('--state',type=Path,default=STATE);a=p.parse_args()
    STATE=a.state
    STATE.mkdir(mode=0o700,exist_ok=True)
    with (STATE/'dispatcher.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config=json.loads(a.config.read_text())
        api=Api(load_controller_credentials()['forgejo_admin_token'],config.get('_forgejo_url'))
        while True:
            ledger=json.loads((STATE/'ledger.json').read_text()) if (STATE/'ledger.json').exists() else {}
            try:reconcile(api,config,ledger)
            except Exception as e:print(type(e).__name__+': '+str(e),flush=True)
            if a.once:break
            time.sleep(60)
if __name__=='__main__':main()
