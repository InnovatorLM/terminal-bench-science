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
    from post_acceptance import accepted, template
    key=row.get('key') or str(row.get('pr'))+':'+str(row.get('commit'))
    row=dict(row)
    current_selftest=current_request(root)
    is_accepted = accepted(row)
    selected_acceptance = is_accepted and acceptance_is_current(root,row)
    if is_accepted and not selected_acceptance:
        return
    if selected_acceptance and current_selftest:
        # Reusing an accepted frozen version can have a newer self-test job.
        # Rebind only its delivery; the immutable review/decision stays intact.
        row['accepted_review_job_id']=row.get('job_id')
        row.update({field:current_selftest.get(field) for field in BINDING_FIELDS})
    if (current_selftest and row.get('job_id') is not None
            and (row['job_id']!=current_selftest.get('job_id')
                 or (row.get('idea_id') is not None and row['idea_id']!=current_selftest.get('idea_id')))):
        return
    if row.get('feedback_source')=='selftest_handoff':
        if (not same_binding(current_selftest,row)
                or (not is_accepted and not current_idea_matches(root,row.get('idea_id')))):return
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
    existing={}
    if existing_path.exists():
        try:
            existing=json.loads(existing_path.read_text())
            existing_key=str(existing.get('key',''))
            same_version=(existing.get('task')==row.get('task') and
                          existing.get('commit',existing.get('required_commit'))==row.get('commit'))
            if (accepted(existing) and acceptance_is_current(root,existing)
                    and (same_version or existing.get('task')==row.get('task'))
                    and not is_accepted):
                # A stale PR scan cannot revoke an accepted frozen version.
                return
            if (existing.get('feedback_source')=='selftest_handoff'
                    and existing.get('state')=='waiting_platform'
                    and same_binding(existing,current_selftest)):
                # Re-scanning an old PR (even the same commit's unqualified
                # preflight) cannot replace the actionable platform ownership.
                handoff_update=row.get('feedback_source')=='selftest_handoff' and same_binding(existing,row)
                real_review=(same_binding(existing,row) and row.get('evaluation_id')
                             and (row.get('role') in config['roles']
                                  or row.get('feedback_source')=='review_result_reuse'))
                if not handoff_update and not real_review and not selected_acceptance:return
            if (existing.get('state')=='waiting_platform' and existing.get('stage')=='five_rollouts'
                    and same_binding(existing,current_selftest) and not selected_acceptance):
                # The exact frozen review owns this wait. An older PR HEAD
                # cannot turn it into an instruction to rebuild or self-test.
                real_review=(same_binding(row,current_selftest)
                             and row.get('evaluation_id')==existing.get('evaluation_id')
                             and row.get('role') in config['roles'])
                if not real_review:return
            # A historical direct assignment is still reconciled every cycle.
            # Its completed receipt must not replace the current PR's repair
            # instructions just because that history is visited first.
            if (existing.get('pr') is not None and row.get('pr') is None
                    and existing.get('task') == row.get('task') and not same_version
                    and row.get('state') == 'completed' and not selected_acceptance):
                return
            if (same_version and existing_key == key
                    and int(existing.get('generation',0)) > int(row.get('generation',0))):
                return
            if (existing.get('state')=='reviewing' and existing_key and existing_key != key
                    and not same_version and not selected_acceptance):
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
                                     and (row.get('feedback_source')=='selftest_handoff'
                                          or row.get('stage')=='five_rollouts')),
             'updated_at':time.time()}
    if row.get('state')=='completed':
        payload['review_recommendation']=row.get('review_recommendation','unrecorded')
        payload['formal_decision']='accepted' if row.get('scientific_acceptance') is True else row.get('formal_decision','not_recorded')
        payload['scientific_acceptance']=row.get('scientific_acceptance') is True
        if row.get('review_recommendation')=='accept' and not payload['scientific_acceptance']:
            payload.update(state='waiting_platform',stage='formal_acceptance_evidence',
                           formal_decision='pending_evidence',author_must_pause=True,
                           acceptance_blockers=row.get('acceptance_blockers',['verification_pending']),
                           next_action='The evaluator recommended acceptance; platform evidence checks are pending. '
                           'Preserve this frozen task and wait for the specific evidence repair; do not rebuild or repeat self-tests.')
        if payload['scientific_acceptance']:
            payload['post_acceptance_required']=True
            prior=existing if existing.get('key')==key and accepted(existing) else {}
            for field in ('proposal_thread','proposal_url','lessons_comment',
                          'post_acceptance_state','post_acceptance_blocker'):
                if field in row:payload[field]=row[field]
                elif field in prior:payload[field]=prior[field]
            if prior.get('post_acceptance_state')=='completed' and prior.get('lessons_comment'):
                payload.update(post_acceptance_state='completed',lessons_comment=prior['lessons_comment'])
            payload.setdefault('post_acceptance_state','pending')
            payload['lessons_template']=template(dict(row,key=key))
            payload['next_action']=(
                'The accepted version has no bound proposal thread. The platform must attach the original '
                'proposal/PR number before the author can publish lessons; do not create a new issue or guess '
                'from a task path.'
                if payload.get('post_acceptance_state')=='proposal_thread_required' else
                'The frozen version is accepted. Reply on the same proposal/PR thread with a Markdown '
                'experience post using lessons_template and proposal_thread. Describe real results, '
                'known limitations and transferable patterns. Do not resume obsolete version reconciliation. '
                'Publication is recorded automatically; no separate receipt or approval is needed.'
                if payload['post_acceptance_state']!='completed' else
                'The accepted-version lessons reply is public on the proposal thread; use its transferable '
                'patterns to begin the next idea. Do not wait for another review of this accepted version.')
    for field in (*BINDING_FIELDS,'evaluation_id','snapshot_sha256','feedback_source','error_type','evidence_gap',
                  'retry_requires_task_change','selftest_rerun_required','accepted_review_job_id',
                  'reused_review_feedback','review_feedback_sha256','reviewed_job_id'):
        if field in row:payload[field]=row[field]
    if row.get('rollout_replacement_error'):
        error=row['rollout_replacement_error']
        payload['rollout_replacement_error']={k:error[k] for k in ('code','owner','next_action')}
    if row.get('reused_review_feedback'):payload['next_action']=row['next_action']
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
        if row.get('stage') == 'evaluator_capacity':
            payload['next_action'] = ('Self-test and immutable registration passed. Preserve this frozen version '
                                      'while it waits for an available evaluator; dispatch resumes automatically. '
                                      'Do not rebuild, repeat self-tests, or resubmit because of this queue wait.')
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
    current = {k: payload.get(k) for k in ('key','generation','commit','task','state','stage','evaluation_id','snapshot_sha256',
                                         *BINDING_FIELDS,'feedback_source') if k in payload}
    for field in ('formal_decision','scientific_acceptance','post_acceptance_required',
                  'post_acceptance_state','proposal_thread','proposal_url','lessons_comment'):
        if field in payload:
            current[field]=payload[field]
    atomic(root/'review-current-assignment.json', current)
    (root/'review-current-assignment.json').chmod(0o644)

def acceptance_is_current(root,row):
    """Never send an old acceptance over a different current task or idea."""
    from selftest_feedback import current_request
    current=current_request(root)
    if current and any(current.get(k)!=row.get(k) for k in ('commit','task')):
        return False
    try:
        clock=json.loads((root/'home/role/.state/author-iteration/ledger.json').read_text())
        active=[idea for idea,value in clock.get('ideas',{}).items()
                if value.get('status') in ('active','acs_wait','platform_wait','session_paused')]
        idea=current.get('idea_id') or row.get('idea_id')
        if active and idea and active != [idea]:return False
    except (OSError,ValueError,TypeError):pass
    return True

def retrospective_work_finished(root,row):
    """A published retrospective and closed idea retire old PR repair prompts.

    A failed revision submitted before acceptance may remain the latest self-test.
    Its PR HEAD must not pull an author back after finishing the accepted task.
    A deliberately active revision of the same task remains eligible for review.
    """
    from post_acceptance import accepted
    from selftest_feedback import current_request
    if (not accepted(row) or row.get('post_acceptance_state')!='completed'
            or not row.get('lessons_comment')):
        return False
    current=current_request(root)
    idea=(current.get('idea_id') if current.get('task')==row.get('task')
          else row.get('idea_id'))
    if not idea:return False
    try:
        clock=json.loads((root/'home/role/.state/author-iteration/ledger.json').read_text())
        return clock.get('ideas',{}).get(idea,{}).get('status')=='finished'
    except (OSError,ValueError,TypeError,AttributeError):
        return False

def retire_proposal_reconciliation(root,row,thread):
    """Archive only the obsolete original-thread prompt; preserve other work."""
    from selftest_feedback import feedback_lock, read_json
    with feedback_lock(root):
        if not retrospective_work_finished(root,row):return
        feedback=read_json(root/'review-feedback.json')
        assignment=read_json(root/'review-current-assignment.json')
        if (feedback.get('stage')!='version_reconciliation'
                or feedback.get('pr')!=thread or feedback.get('task')!=row.get('task')):
            return
        identity=('key','generation','commit','task')
        same_assignment=bool(assignment) and all(assignment.get(k)==feedback.get(k) for k in identity)
        archive=STATE/'retired-author-feedback'
        archive.mkdir(mode=0o700,exist_ok=True)
        archive_name=hashlib.sha256(row['key'].encode()).hexdigest()+'.json'
        atomic(archive/archive_name,dict(
            retired_at=time.time(), accepted_key=row.get('key'),
            lessons_comment=row['lessons_comment'],feedback=feedback,
            assignment=assignment if same_assignment else None,
            reason='Original proposal retrospective is public and its author idea is finished.'))
        (root/'review-feedback.json').unlink()
        if same_assignment:(root/'review-current-assignment.json').unlink()

def sync_post_acceptance(api,config,ledger,pulls):
    """Find the proposal thread and remember one published author retrospective."""
    from post_acceptance import accepted, public_lessons_comment, comment_summary, template
    comments_by_thread={};followups={};all_pulls=None
    for key,row in ledger.items():
        author=row.get('author_role')
        if not accepted(row) or author not in config['roles']:continue
        thread=row.get('proposal_thread') or row.get('pr')
        if not thread:
            candidates=[pr for pr in pulls if author_for_pr(pr,config)==author
                        and row.get('task') in re.findall(r'tasks/[a-zA-Z0-9_./-]+',pr.get('body') or '')]
            if not candidates:
                if all_pulls is None:all_pulls=api.pages('/pulls?state=all')
                candidates=[pr for pr in all_pulls if author_for_pr(pr,config)==author
                            and row.get('task') in re.findall(r'tasks/[a-zA-Z0-9_./-]+',pr.get('body') or '')]
            if len(candidates)==1:
                thread=candidates[0]['number']
                row['proposal_url']=candidates[0].get('html_url')
        if thread:
            row['proposal_thread']=thread
            # Keep the actual pull URL when it is available. Falling back to
            # an issue URL for a PR loses the original proposal context.
            pull = next((item for item in (pulls or all_pulls or [])
                         if str(item.get('number')) == str(thread)), None)
            if not row.get('proposal_url') and pull and pull.get('html_url'):
                row['proposal_url'] = pull['html_url']
            if not row.get('proposal_url'):
                base=(getattr(api,'base_url',None) or 'http://127.0.0.1:3000').rstrip('/')
                suffix='pulls' if row.get('pr') is not None else 'issues'
                row['proposal_url']=base+'/team/terminal-bench-science/'+suffix+'/'+str(thread)
            if row.get('post_acceptance_state')!='completed':
                if thread not in comments_by_thread:
                    comments_by_thread[thread]=api.pages('/issues/'+str(thread)+'/comments')
                comment=public_lessons_comment(comments_by_thread[thread],dict(row,key=key),author,thread)
                if comment:
                    row.update(post_acceptance_state='completed',lessons_comment=comment_summary(comment,thread))
        elif row.get('post_acceptance_state')!='completed':
            # A direct evaluator assignment may have no PR number. Do not
            # guess from a task path: one author can have multiple proposals
            # for the same task. The operator must bind this accepted version
            # to its original proposal before lessons can be published.
            row.update(post_acceptance_state='proposal_thread_required',
                       post_acceptance_blocker='accepted version has no bound proposal thread')
        row.setdefault('post_acceptance_state','pending')
        public={field:row.get(field) for field in (
            'commit','task','snapshot_sha256','evaluation_id','state','formal_decision',
            'scientific_acceptance','reason','proposal_thread','proposal_url',
            'post_acceptance_state','post_acceptance_blocker','lessons_comment')}
        public.update(key=key,post_acceptance_required=True,lessons_template=template(dict(row,key=key)))
        followups.setdefault(author,[]).append(public)
        publish_author_feedback({},dict(row,key=key),config)
    # Keep retrospective work separate from a newer self-test or PR's status.
    # An author may already have submitted an unsuccessful revision before the
    # acceptance was delivered. That must neither erase the old acceptance nor
    # falsely label the new revision accepted.
    for author,items in followups.items():
        payload=dict(pending=[r for r in items if r['post_acceptance_state']!='completed'],
                     completed=[r for r in items if r['post_acceptance_state']=='completed'])
        path=Path(config['roles'][author]['root'])/'post-acceptance.json'
        atomic(path,payload);path.chmod(0o644)

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

def advance_review(key,row,config,now=None,role_available=True):
    from post_acceptance import accepted
    if accepted(row):return  # A finalized decision is immutable, not a live status receipt.
    if row.get('rollout_replacement_error'):return
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
            # Retain the authenticated completion before the role takes a new task.
            row['review_completion']=dict(key=key,generation=row['generation'],role=row['role'],
                review_recommendation=row['review_recommendation'],
                reason_sha256=hashlib.sha256(row['reason'].encode()).hexdigest(),
                status_sha256=hashlib.sha256(json.dumps(status,sort_keys=True).encode()).hexdigest(),
                updated_at=status['updated_at'])
        else:row.pop('review_recommendation',None)
    elif row['state']=='reviewing' and now-float(row['assigned_at'])>REVIEW_ACTIVITY_TIMEOUT:
        row['evaluator_activity_confirmed'] = False
        row.update(state='waiting_platform',stage='evaluator_runner_startup',
                   reason='Evaluator produced no matching activity and status receipt within the startup window.',
                   evaluator_assigned=False,updated_at=now)
    publication_updates=[]
    if row['state']=='waiting_author' and row.get('delivered_rollout_fingerprint'):
        from recover_completed_evaluation import pending_review_updates
        publication_updates=pending_review_updates(dict(row,key=key))
    if (row['state']=='waiting_platform' and row.get('stage')=='five_rollouts') or publication_updates:
        from role_evaluation_batch import requests,summary
        batch_generation=row.setdefault('batch_generation',row['generation'])
        assignment=dict(row,key=key,generation=batch_generation)
        results=Path(entry['root'])/'evaluation/results'
        jobs=requests(assignment)
        if not all((results/(job['job_id']+'.json')).exists() for job in jobs):
            return
        try:
            batch=summary(assignment,results)
        except (OSError,ValueError,KeyError,TypeError):
            batch=None
            stage='rollout_results_invalid'
            reason='Five rollout files exist, but result identity or content needs diagnosis.'
        else:
            stage='rollout_results_ready' if batch['complete'] else 'rollout_results_invalid'
            reason=('Five rollout results are available for diagnosis and review; '
                    f"{batch['valid_trials']} of five meet evidence requirements."
                    + ('' if batch['complete'] else ' Invalid runs cannot establish model accuracy.'))
        # A reviewer may diagnose an invalid batch and park it again. Wake
        # only for changed result bytes, new jobs, or a new timeout judgment;
        # unchanged failures cannot create an endless review/wait loop.
        result_fingerprint=hashlib.sha256(json.dumps([
            [(job['job_id'],hashlib.sha256((results/(job['job_id']+'.json')).read_bytes()).hexdigest())
             for job in jobs],batch],sort_keys=True).encode()).hexdigest()
        if row.get('delivered_rollout_fingerprint')==result_fingerprint:
            return
        if not role_available:
            if publication_updates:
                row['pending_publication_updates']=publication_updates
            else:row.update(reason=reason,rollout_result_status=stage)
            return
        if publication_updates:
            row.setdefault('reviews_before_publication_recovery',[]).append(
                {field:row.get(field) for field in ('generation','state','stage','reason','updated_at')})
            row.setdefault('applied_publication_updates',[]).extend(publication_updates)
            row.pop('pending_publication_updates',None)
            reason += (' Platform restored completed-run evidence without rerunning the model. '
                       'Refresh the assessment using these same five jobs. Preserve the prior task defects '
                       'in reviews_before_publication_recovery; do not repeat package checks or launch a new batch.')
        row.update(state='reviewing',stage=stage,reason=reason,
                   generation=row['generation']+1,assigned_at=now,updated_at=now,
                   evaluator_assigned=True,evaluator_activity_confirmed=False,
                   last_activity_at=None,delivered_rollout_fingerprint=result_fingerprint)

def role_available_for(ledger,key,role):
    return not any(other_key!=key and other.get('role')==role and other.get('state')=='reviewing'
                   for other_key,other in ledger.items())

def publish_wake(root,key,row):
    # The Rust scheduler refuses an identity-free wake. PR-based assignments
    # must carry the same binding as direct self-test handoffs.
    wake=dict(key=key,generation=row['generation'],state=row['state'])
    wake.update({field:row[field] for field in ('task','commit','evaluation_id','snapshot_sha256') if field in row})
    path=root/'review-dispatch.json';atomic(path,wake);path.chmod(0o644)
    if row['state']=='reviewing':row['wake_schema_version']=1

def deliver_selftest_evidence(row,config,role_available=True):
    if not all(row.get(k) for k in ('commit','task','evaluation_id','snapshot_sha256')):return True
    from deployment_paths import universe_root
    from selftest_review_evidence import publish
    try:
        manifest=publish(config['roles'][row['role']],row,universe_root(W))
    except (OSError,ValueError,KeyError,TypeError) as error:
        row['selftest_evidence_error']=type(error).__name__
        if row.get('state')=='reviewing':
            row.update(state='waiting_platform',stage='selftest_evidence_delivery',
                       reason='Platform must deliver exact-version selftest controls; do not repeat ACS.')
        return False
    if manifest is not None:
        row['selftest_evidence_packet']='/evaluation/evidence/'+row['evaluation_id']
        row['selftest_evidence_job_id']=manifest['job_id']
    row.pop('selftest_evidence_error',None)
    if row.get('stage')=='selftest_evidence_delivery' and role_available:
        row.update(state='reviewing',stage='completeness',generation=row.get('generation',0)+1,
                   assigned_at=time.time(),updated_at=time.time(),last_activity_at=None,
                   evaluator_activity_confirmed=False)
    return True

def sync_direct_assignments(config,ledger):
    """Consume persisted exact assignments even when no PR supplied them."""
    from formal_review_decision import apply
    apply(STATE, ledger)
    from rollout_replacement import apply as apply_replacements
    apply_replacements(STATE,ledger,config)
    active_by_role={}
    for key,row in ledger.items():
        if row.get('state')!='reviewing':continue
        role=row.get('role')
        if role in active_by_role:
            raise ValueError('Two active reviews assigned to the same evaluator')
        active_by_role[role]=key
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
                and row.get('stage') == 'immutable_execution_registration'
                and role_available_for(ledger,key,row['role'])):
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
        role=row['role']
        role_available=active_by_role.get(role) in (None,key)
        if deliver_selftest_evidence(row,config,role_available):
            advance_review(key,row,config,role_available=role_available)
        if row['state']=='reviewing':
            active_by_role[role]=key
        root=Path(config['roles'][row['role']]['root'])
        if row['state']=='reviewing':
            assignment=root/'home/role/review-assignment.json'
            try:delivered=json.loads(assignment.read_text())
            except (OSError,ValueError,TypeError):delivered={}
            if delivered.get('key')!=key or delivered.get('generation')!=row['generation']:
                atomic(assignment,dict(row,key=key))
                os.chown(assignment,config['roles'][row['role']]['uid'],config['roles'][row['role']]['gid'])
        if active_by_role.get(role) in (None,key):
            publish_wake(root,key,row)
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
        out=[];seen=set()
        for page in range(1,1001):
            batch=self.call('GET',path+('&' if '?' in path else '?')+'limit='+str(FORGEJO_PAGE_SIZE)+'&page='+str(page))
            if not batch:return out
            if '/comments' in path:
                # The deployed Forgejo comments endpoint returns the complete
                # list and ignores page/limit. Also support paginated servers.
                fresh=[item for item in batch if item['id'] not in seen]
                if not fresh:return out
                seen.update(item['id'] for item in fresh)
                batch=fresh
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

def finalize_acceptances(config,ledger):
    from review_acceptance import finalize
    from deployment_paths import universe_root
    from evaluation_queue import root_file
    candidates=any(row.get('state')=='completed' and row.get('review_recommendation')=='accept'
                   and row.get('scientific_acceptance') is not True for row in ledger.values())
    blocked={}
    if candidates:
        queue=json.loads(root_file(universe_root(W)/'.config/science-evaluation/queue.json').read_text())
        # record() reads the durable completed state; receipt-first publication
        # recovers through apply() after an interrupted dispatcher cycle.
        atomic(STATE/'ledger.json',ledger)
        blocked=finalize(STATE,ledger,config,Path(queue['state']),registered_execution)
    atomic(STATE/'pending-acceptances.json',blocked)
    if 'curator' in config['roles']:
        path=Path(config['roles']['curator']['root'])/'review-acceptance-handoffs.json'
        atomic(path,dict(owner='platform',pending=blocked));path.chmod(0o644)
    for key,row in ledger.items():
        if row.get('review_recommendation')=='accept':
            publish_author_feedback({},dict(row,key=key),config)
    atomic(STATE/'ledger.json',ledger)

def reconcile(api,config,ledger):
    from deployment_paths import universe_root
    from selftest_review_dispatch import consume
    from formal_review_decision import apply
    apply(STATE, ledger)
    # Local completion must progress even when Forgejo is temporarily slow.
    finalize_acceptances(config,ledger)
    bound={key:row for key,row in ledger.items() if row.get('proposal_thread') or row.get('pr')}
    sync_post_acceptance(api,config,bound,[])
    atomic(STATE/'ledger.json',ledger)
    consume(config,ledger,universe_root(W),STATE)
    sync_direct_assignments(config,ledger)
    pulls=api.pages('/pulls?state=open')
    sync_post_acceptance(api,config,ledger,pulls)
    pending={};handoffs={}
    for pr in pulls:
        if pr.get('draft') or 'draft' in pr['title'].lower():continue
        match=re.search(r'tasks/[a-zA-Z0-9_./-]+',pr.get('body') or '')
        if not match:continue
        task=match.group(0).rstrip('./');key=str(pr['number'])+':'+pr['head']['sha']
        from post_acceptance import accepted
        from selftest_feedback import current_request
        author=author_for_pr(pr,config)
        selected=current_request(Path(config['roles'][author]['root'])) if author else {}
        finished=[dict(row,key=k) for k,row in ledger.items()
                  if author and row.get('author_role')==author and row.get('task')==task
                  and row.get('proposal_thread')==pr['number']
                  and retrospective_work_finished(Path(config['roles'][author]['root']),row)]
        if finished:
            retire_proposal_reconciliation(Path(config['roles'][author]['root']),finished[0],pr['number'])
            continue
        if author and any(accepted(row) and row.get('author_role')==author
                          and row.get('task')==task and row.get('proposal_thread')==pr['number']
                          and (not selected or all(selected.get(k)==row.get(k) for k in ('commit','task')))
                          for row in ledger.values()):
            # The author explicitly selected the accepted version. Preserve
            # that decision without transferring it to this PR's different HEAD.
            continue
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
                if role_available_for(ledger,key,old['role']):
                    publish_wake(Path(config['roles'][old['role']]['root']),key,old)
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
              and role_available_for(ledger,key,row['role'])
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
        evidence_delivered=deliver_selftest_evidence(row,config,role_available_for(ledger,key,row['role']))
        if (row.get('stage')=='evaluator_runner_startup' and row.get('wake_schema_version')!=1
                and row['state'] in ('reviewing','waiting_platform')
                and role_available_for(ledger,key,row['role'])):
            # One-time recovery of assignments published before version-bound
            # PR wakes existed. A genuine failure after this delivery does not
            # receive repeated automatic restarts.
            row.update(state='reviewing',stage='completeness',reason='Recovered the missing immutable binding in the evaluator wake.',
                       generation=row['generation']+1,assigned_at=time.time(),updated_at=time.time(),
                       evaluator_assigned=True,evaluator_activity_confirmed=False,last_activity_at=None,
                       wake_schema_version=1,issue=None)
        if evidence_delivered:
            advance_review(key,row,config,role_available=role_available_for(ledger,key,row['role']))
        if row['state'] in ('waiting_author','waiting_platform','completed') and row.get('issue'):
            issue=api.call('GET','/issues/'+str(row['issue']))
            if issue['state']=='open':api.call('PATCH','/issues/'+str(row['issue']),{'state':'closed'})
        if (row['state'] in ('waiting_author','waiting_platform') and event!=row['event']
                and not row.get('rollout_replacement_error')
                and row.get('stage') not in ('five_rollouts','selftest_evidence_delivery')):
            if not any(v['state']=='reviewing' and v['role']==row['role'] for k,v in ledger.items() if k!=key):
                row.update(state='reviewing',event=event,generation=row['generation']+1,issue=None,assigned_at=time.time())
        if row['state']!='reviewing':
            if role_available_for(ledger,key,row['role']):
                publish_wake(root,key,row)
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
        publish_wake(root,key,row)
        publish_author_feedback(pr,row,config)
    atomic(STATE/'pending-submissions.json',pending)
    publish_version_handoffs(config,handoffs)
    atomic(STATE/'ledger.json',ledger)
    finalize_acceptances(config,ledger)
    sync_post_acceptance(api,config,ledger,pulls)
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
