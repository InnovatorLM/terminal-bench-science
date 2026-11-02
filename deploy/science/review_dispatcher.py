"""Single-writer PR review dispatch. Local Forgejo only; no solver/cloud actions."""
import argparse,fcntl,hashlib,json,math,os,re,stat,time,urllib.request
from pathlib import Path
W=Path('/mnt/data/zhanglinfeng/workspace')
STATE=W/'.state/science-review-dispatcher'
REPO='/repos/team/terminal-bench-science'
MARK='<!-- exclusive-review-v1 -->'
STATES={'reviewing','waiting_author','waiting_platform','completed'}
REVIEW_ACTIVITY_TIMEOUT=300
# Forgejo's pull-list response becomes very slow for this repository when a
# large page is requested: it expands metadata for every open PR, and a page
# of 50 can sit in the HTTP handler until the dispatcher timeout.  Keep the
# polling request bounded so one large or recently-updated PR cannot prevent
# all review assignments from being reconciled.  Pagination still visits the
# complete list, just in small stable batches.
FORGEJO_PAGE_SIZE=5

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
    author=row.get('author_role') or author_for_pr(pr,config)
    if not author: return
    root=Path(config['roles'][author]['root'])
    from selftest_feedback import feedback_lock
    with feedback_lock(root):
        _publish_author_feedback_locked(pr,row,config,root)

def _publish_author_feedback_locked(pr,row,config,root):
    from selftest_feedback import BINDING_FIELDS,current_request,same_binding,current_idea_matches
    key=row.get('key') or str(row.get('pr'))+':'+str(row.get('commit'))
    row=dict(row)
    current_selftest=current_request(root)
    if (current_selftest and row.get('job_id') is not None
            and (row['job_id']!=current_selftest.get('job_id')
                 or (row.get('idea_id') is not None and row['idea_id']!=current_selftest.get('idea_id')))):
        return
    if row.get('feedback_source')=='selftest_handoff':
        if (not same_binding(current_selftest,row)
                or not current_idea_matches(root,row.get('idea_id'))):return
    elif (current_selftest.get('commit')==row.get('commit')
            and current_selftest.get('task')==row.get('task')):
        # Exact-version review can take over a recovered self-test handoff. A
        # different job/idea explicitly supplied by a caller is never replaced.
        for field in BINDING_FIELDS:
            row.setdefault(field,current_selftest.get(field))
    # Never let an older PR revision overwrite a newer exact-commit handoff
    # already owned by this author.  Without this guard, the periodic Forgejo
    # scan can replace a current reviewing assignment with stale waiting data.
    existing_path=root/'review-feedback.json'
    if existing_path.exists():
        try:
            existing=json.loads(existing_path.read_text())
            existing_key=str(existing.get('key',''))
            same_version=(existing.get('task')==row.get('task') and
                          existing.get('commit',existing.get('required_commit'))==row.get('commit'))
            if (existing.get('feedback_source')=='selftest_handoff'
                    and existing.get('state')=='waiting_platform'
                    and same_binding(existing,current_selftest)):
                # Re-scanning an old PR (even the same commit's unqualified
                # preflight) cannot replace the actionable platform ownership.
                handoff_update=row.get('feedback_source')=='selftest_handoff' and same_binding(existing,row)
                real_review=(same_binding(existing,row) and row.get('evaluation_id')
                             and row.get('role') in config['roles'])
                if not handoff_update and not real_review:return
            # A historical direct assignment is still reconciled every cycle.
            # Its completed receipt must not replace the current PR's repair
            # instructions just because that history is visited first.
            if (existing.get('pr') is not None and row.get('pr') is None
                    and existing.get('task') == row.get('task') and not same_version
                    and row.get('state') == 'completed'):
                return
            if (same_version and existing_key == key
                    and int(existing.get('generation',0)) > int(row.get('generation',0))):
                return
            if existing.get('state')=='reviewing' and existing_key and existing_key != key and not same_version:
                return
        except (OSError,ValueError,TypeError):
            pass
    payload={'key':key,'generation':row.get('generation',0), 'commit':row.get('commit'),
             'state':row.get('state'),'stage':row.get('stage'),'reason':row.get('reason',''),
             'task':row.get('task'),'evaluator':row.get('role'),
             # Review ownership needs confirmed evaluator activity. A separate
             # exact-job platform wait also parks the author until repaired.
             'author_must_pause':(row.get('state')=='reviewing'
                                  and row.get('evaluator_activity_confirmed') is True)
                                 or (row.get('state')=='waiting_platform'
                                     and row.get('feedback_source')=='selftest_handoff'),
             'updated_at':time.time()}
    if row.get('state')=='completed':
        payload['review_recommendation']=row.get('review_recommendation','unrecorded')
        payload['formal_decision']='accepted' if row.get('scientific_acceptance') is True else 'not_recorded'
    for field in (*BINDING_FIELDS,'feedback_source','error_type','evidence_gap',
                  'retry_requires_task_change','selftest_rerun_required'):
        if field in row:payload[field]=row[field]
    if pr.get('number') is not None or row.get('pr') is not None:
        payload['pr']=pr.get('number',row.get('pr'))
    if row.get('prior_reviews'):
        payload['prior_reviews']=row['prior_reviews']
        payload['next_action']=row.get('next_action','')
    # An exact self-test platform wait parks the author until the platform
    # repairs this job's handoff. It never asks the author to rerun ACS.
    if row.get('state') == 'waiting_platform':
        payload['next_action'] = ('Platform intervention is required for this exact assignment. '
                                  'Preserve the task, self-test and immutable evidence; '
                                  'do not rebuild or repeat self-tests to work around a platform failure.')
        payload['required_commit'] = row.get('commit')
        payload['required_task'] = row.get('task')
    elif row.get('state') == 'reviewing':
        payload['next_action'] = 'Pause this task and wait for the assigned evaluator result or concrete repair feedback.'
    if row.get('evaluation_id') and row.get('role') in config['roles']:
        evidence=Path(config['roles'][row['role']]['root'])/'evaluation/evidence'/row['evaluation_id']/'selftest-status.json'
        try:
            info=evidence.lstat();proof=json.loads(evidence.read_text())
            if (stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022
                    and proof.get('source_commit')==row.get('commit')
                    and proof.get('snapshot_sha256')==row.get('snapshot_sha256')):
                payload['platform_selftest_evidence']={k:proof.get(k) for k in (
                    'job_id','status','source_commit','snapshot_sha256','source_git_tree_verified')}
                payload['evidence_note']='Use the matching platform self-test receipt above; older repository logs do not invalidate it. Fix scientific contract findings, but do not repeat an unchanged passing self-test solely because a review could not see the platform receipt.'
        except (OSError,ValueError,TypeError):pass
    atomic(root/'review-feedback.json',payload); (root/'review-feedback.json').chmod(0o644)
    # Public exact-current marker consumed by the controller.  It is separate
    # from the evaluator's private assignment and is replaced atomically so an
    # old author feedback file cannot pause a newer session.
    current = {k: payload.get(k) for k in ('key','generation','commit','task','state',
                                         *BINDING_FIELDS,'feedback_source') if k in payload}
    atomic(root/'review-current-assignment.json', current)
    (root/'review-current-assignment.json').chmod(0o644)

def matching_review_status(root, entry, key, generation):
    """Only a matching pair written by the assigned role is activity evidence."""
    values=[]
    for name in ('review-status.json','activity-receipt.json'):
        path=root/'home/role'/name
        try:
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=entry['uid']:
                return None
            value=json.loads(path.read_text())
            if value.get('key')!=key or value.get('generation')!=generation:
                return None
            values.append(value)
        except (OSError,ValueError,TypeError):
            return None
    status,activity=values
    if status.get('state') not in STATES or activity.get('receipt_type')!='evaluator-activity-v1':
        return None
    stamp=status.get('updated_at')
    if (type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0
            or activity.get('updated_at')!=stamp):
        return None
    return status

def advance_review(key,row,config,now=None):
    now=time.time() if now is None else now
    entry=config['roles'][row['role']];root=Path(entry['root'])
    # Legacy exact self-test handoffs have no PR fields or assigned timestamp.
    # Start their watchdog once; subsequent polling never refreshes this clock.
    row.setdefault('assigned_at',row.get('updated_at') or now)
    status=matching_review_status(root,entry,key,row['generation'])
    if status is not None:
        # An assignment alone is not evidence that the evaluator process woke.
        # Only the matching reviewing receipt may pause the author.
        row['evaluator_activity_confirmed'] = status.get('state') == 'reviewing'
        row.update(state=status['state'],stage=str(status.get('stage',''))[:100],
                   reason=str(status.get('reason',''))[:2000],updated_at=status['updated_at'],
                   last_activity_at=status['updated_at'])
        if status['state']=='completed':
            recommendation=status.get('review_recommendation','unrecorded')
            row['review_recommendation']=(recommendation if recommendation in
                                          ('accept','reject','revise') else 'unrecorded')
        else:row.pop('review_recommendation',None)
    elif row['state']=='reviewing' and now-float(row['assigned_at'])>REVIEW_ACTIVITY_TIMEOUT:
        row['evaluator_activity_confirmed'] = False
        row.update(state='waiting_platform',stage='evaluator_runner_startup',
                   reason='Evaluator produced no matching activity and status receipt within the startup window.',
                   evaluator_assigned=False,updated_at=now)

def sync_direct_assignments(config,ledger):
    """Consume persisted exact assignments even when no PR supplied them."""
    for key,row in ledger.items():
        if row.get('pr') is not None:
            continue
        if row.get('role') not in config['roles']:
            continue
        # Direct handoffs have no PR comment whose fingerprint can create a
        # new wake edge. When the platform was previously missing the exact
        # immutable registration, resume that same assignment as a new
        # generation as soon as the registration appears. Keep the identity
        # binding strict so a different task/version can never be substituted.
        if (row.get('state') == 'waiting_platform'
                and row.get('stage') == 'immutable_execution_registration'):
            try:
                execution = registered_execution(row['commit'], row['task'])
            except (OSError, ValueError, KeyError, TypeError):
                execution = None
            if (execution is not None
                    and execution.get('evaluation_id') == row.get('evaluation_id')
                    and execution.get('snapshot_sha256') == row.get('snapshot_sha256')):
                now = time.time()
                row.update(state='reviewing', stage='completeness',
                           generation=row.get('generation', 0) + 1,
                           assigned_at=now, updated_at=now,
                           evaluator_assigned=True,
                           evaluator_activity_confirmed=False,
                           last_activity_at=None)
        advance_review(key,row,config)
        root=Path(config['roles'][row['role']]['root'])
        wake=dict(key=key,generation=row['generation'],state=row['state'])
        wake.update({field:row[field] for field in ('task','commit','evaluation_id','snapshot_sha256') if field in row})
        atomic(root/'review-dispatch.json',wake);(root/'review-dispatch.json').chmod(0o644)
        # Keep the authoritative identity and existing role-local assignment;
        # only the operator ledger/wake/author feedback consume result state.
        publish_author_feedback({},dict(row,key=key),config)
    atomic(STATE/'ledger.json',ledger)

class Api:
    def __init__(self,token,base_url=None):self.token=token;self.base_url=base_url
    def call(self,method,path,data=None):
        base=(self.base_url or os.environ.get('GEWU_FORGEJO_URL','http://127.0.0.1:3000')).rstrip('/')
        req=urllib.request.Request(base+'/api/v1'+REPO+path,data=json.dumps(data).encode() if data is not None else None,method=method,headers={'Authorization':'token '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
    def pages(self,path):
        out=[]
        for page in range(1,1001):
            batch=self.call('GET',path+('&' if '?' in path else '?')+'limit='+str(FORGEJO_PAGE_SIZE)+'&page='+str(page))
            if not batch:return out
            out+=batch
            # Forgejo may spend a long time answering pages beyond the end of
            # a large repository. A short page is authoritative termination.
            if len(batch) < FORGEJO_PAGE_SIZE:
                return out
        raise ValueError('Pagination did not terminate')

def completed_versions(ledger, author, task, current_commit):
    """Historical review is evidence for its version, never approval of HEAD."""
    return [dict(key=key, **{field:row.get(field) for field in (
                'commit','task','snapshot_sha256','evaluation_id','role',
                'generation','state','stage','reason')},
                review_recommendation=row.get('review_recommendation','unrecorded'),
                formal_decision='accepted' if row.get('scientific_acceptance') is True else 'not_recorded')
            for key,row in sorted(ledger.items())
            if row.get('state')=='completed' and row.get('task')==task
            and row.get('commit')!=current_commit
            and author is not None and row.get('author_role')==author]

def publish_version_handoffs(config, handoffs):
    """Give the curator the same current-version ownership that authors see."""
    payload={'schema_version':1,'handoffs':handoffs,'updated_at':time.time(),
             'note':'Historical completed reviews remain valid for their frozen versions; '
                    'this queue neither accepts a task nor transfers old scores to a new commit.'}
    atomic(STATE/'review-version-handoffs.json',payload)
    if 'curator' in config['roles']:
        path=Path(config['roles']['curator']['root'])/'review-version-handoffs.json'
        atomic(path,payload);path.chmod(0o644)

def reconcile(api,config,ledger):
    from deployment_paths import universe_root
    from selftest_review_dispatch import consume
    consume(config,ledger,universe_root(W),STATE)
    sync_direct_assignments(config,ledger)
    pulls=api.pages('/pulls?state=open')
    pending={};handoffs={}
    for pr in pulls:
        if pr.get('draft') or 'draft' in pr['title'].lower():continue
        match=re.search(r'tasks/[a-zA-Z0-9_./-]+',pr.get('body') or '')
        if not match:continue
        task=match.group(0).rstrip('./');key=str(pr['number'])+':'+pr['head']['sha']
        direct=[(k,row) for k,row in ledger.items() if row.get('pr') is None
                and row.get('commit')==pr['head']['sha'] and row.get('task')==task]
        if direct:
            # A restored immutable handoff already owns this version. A PR
            # appearing later must not allocate a second evaluator to it.
            if len(direct)!=1:raise ValueError('Duplicate exact review assignment')
            direct_key,row=direct[0]
            publish_author_feedback(pr,dict(row,key=direct_key),config)
            continue
        from author_submission_gate import approved, review_budget_summary
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
            author=author_for_pr(pr,config)
            prior=completed_versions(ledger,author,task,pr['head']['sha'])
            if prior:
                pending[key].update(author_role=author,stage='version_reconciliation',
                    prior_reviews=prior,
                    reason='This task has a completed review on a different commit; current PR is not covered.',
                    next_action='Read prior_reviews and compare only this task path between the reviewed commit and current PR. '
                    'Send curator the exact version choice and meaningful changes before more tests. '
                    'Do not requalify or rerun the completed version just to migrate receipt schemas. '
                    'Do not archive or checkout the whole repository. A deliberately revised version still requires its own qualification.')
                handoffs[key]=dict(pending[key],owner=author,decision_owner='curator')
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
        if any(v.get('pr')==pr['number'] and k!=key and v['state']=='reviewing' for k,v in ledger.items()):
            continue  # Finish or park prior revision before assigning the new one.
        row=ledger.get(key)
        if row is None:
            busy={v['role'] for v in ledger.values() if v['state']=='reviewing'}
            roles=[n for n in config['roles'] if n.startswith('evaluator-') and n not in busy]
            if not roles:continue
            role=sorted(roles)[0]
            row={'pr':pr['number'],'author_role':author_for_pr(pr,config),'commit':pr['head']['sha'],'task':task,**execution,'snapshot_kind':'registered-task-content-sha256','role':role,'state':'reviewing','stage':'completeness','event':event,'generation':0,'issue':None,'updated_at':time.time()}
            ledger[key]=row;atomic(STATE/'ledger.json',ledger)
        elif any(row.get(k)!=execution[k] for k in ('evaluation_id','snapshot_sha256')):
            # Never silently change an active assignment to a different image/task.
            pending[key]=dict(pr=pr['number'],state='waiting_platform',stage='assignment_binding_mismatch',evaluator_assigned=False)
            continue
        elif (row.get('state') == 'waiting_platform'
              and row.get('stage') == 'immutable_execution_registration'
              and execution is not None):
            # A registration may be completed after the dispatcher parked the
            # exact author version. Resume it as a new generation without
            # requiring a misleading PR edit just to create an event edge.
            row.update(state='reviewing', event=event,
                       generation=row.get('generation', 0) + 1,
                       issue=None, assigned_at=time.time(),
                       evaluator_assigned=True,
                       evaluator_activity_confirmed=False)
        summary=review_budget_summary(pr['head']['sha'],task)
        if summary is not None:row['budget_summary']=summary
        root=Path(config['roles'][row['role']]['root'])
        advance_review(key,row,config)
        if row['state'] in ('waiting_author','waiting_platform','completed') and row.get('issue'):
            issue=api.call('GET','/issues/'+str(row['issue']))
            if issue['state']=='open':api.call('PATCH','/issues/'+str(row['issue']),{'state':'closed'})
        if row['state'] in ('waiting_author','waiting_platform') and event!=row['event']:
            if not any(v['state']=='reviewing' and v['role']==row['role'] for k,v in ledger.items() if k!=key):
                row.update(state='reviewing',event=event,generation=row['generation']+1,issue=None,assigned_at=time.time())
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
    publish_version_handoffs(config,handoffs)
    atomic(STATE/'ledger.json',ledger)

def main():
    from controller_credentials import load_controller_credentials
    global STATE
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--config',type=Path,default=W/'.state/science-role-broker/config.json');p.add_argument('--state',type=Path,default=STATE);a=p.parse_args()
    STATE=a.state
    STATE.mkdir(mode=0o700,exist_ok=True)
    with (STATE/'dispatcher.lock').open('a') as lock:
        # The launcher may briefly overlap an older dispatcher during a
        # restart. Wait for the existing instance instead of exiting and
        # leaving review dispatch silently disabled.
        fcntl.flock(lock,fcntl.LOCK_EX)
        config=json.loads(a.config.read_text())
        api=Api(load_controller_credentials()['forgejo_admin_token'],config.get('_forgejo_url'))
        while True:
            ledger=json.loads((STATE/'ledger.json').read_text()) if (STATE/'ledger.json').exists() else {}
            try:reconcile(api,config,ledger)
            except Exception as e:print(type(e).__name__+': '+str(e),flush=True)
            if a.once:break
            time.sleep(60)
if __name__=='__main__':main()
