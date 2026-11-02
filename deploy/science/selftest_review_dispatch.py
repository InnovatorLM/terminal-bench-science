"""Consume source-bound selftest handoffs without requiring a matching PR."""
import json
import os
from pathlib import Path
import time


def consume(config, ledger, universe, state):
    import review_dispatcher as dispatcher
    from author_submission_gate import approved, read_operator_file, validate_identity
    from evaluation_queue import atomic_json
    directory=Path(universe)/'.state/gewu-multiverse/review-requests'
    pending={}
    for path in sorted(directory.glob('*.json')):
        try:
            request=json.loads(read_operator_file(path,1024*1024))
            if request.get('state')!='ready_for_independent_review':continue
            commit,task=request['commit'],request['task'];validate_identity(commit,task)
            author=request['author_role']
            if author not in config['roles'] or not author.startswith('author-'):
                raise ValueError('Unknown requesting author')
            key=commit+':'+task
            if not approved(commit,task):
                raise ValueError('Author selftest/iteration qualification missing')
            execution=dispatcher.registered_execution(commit,task)
            if (execution is None or execution.get('evaluation_id')!=request.get('evaluation_id')
                    or execution.get('snapshot_sha256')!=request.get('snapshot_sha256')):
                pending[key]={'state':'waiting_platform','stage':'immutable_execution_registration',
                              'commit':commit,'task':task,'author_role':author}
                dispatcher.publish_author_feedback({},pending[key],config)
                continue
            existing=[(k,row) for k,row in ledger.items() if row.get('commit')==commit and row.get('task')==task]
            if len(existing)>1:raise ValueError('Duplicate exact-version assignment')
            if existing:
                key,row=existing[0]
                # An existing PR assignment or completed review already owns
                # this version; never reset its state because the request persists.
                if row.get('pr') is not None or row.get('state')!='reviewing':continue
            else:
                busy={row.get('role') for row in ledger.values() if row.get('state')=='reviewing'}
                available=sorted(role for role in config['roles'] if role.startswith('evaluator-') and role not in busy)
                if not available:
                    pending[key]={'state':'queued','stage':'evaluator_capacity','commit':commit,'task':task}
                    continue
                now=time.time()
                row=dict(key=key,pr=None,role=available[0],author_role=author,
                         commit=commit,task=task,**execution,job_id=request['job_id'],
                         generation=0,state='reviewing',stage='completeness',
                         assigned_at=now,updated_at=now,evaluator_assigned=True,
                         evaluator_activity_confirmed=False,snapshot_kind='registered-task-content-sha256')
                ledger[key]=row
                atomic_json(Path(state)/'ledger.json',ledger)
            entry=config['roles'][row['role']]
            destination=Path(entry['root'])/'home/role/review-assignment.json'
            destination.parent.mkdir(parents=True,exist_ok=True)
            payload=dict(row,key=key)
            # Preserve an already delivered file, but recover a crash between
            # durable allocation and delivery to the selected evaluator.
            try:delivered=json.loads(destination.read_text())
            except (OSError,ValueError):delivered={}
            if any(delivered.get(k)!=payload.get(k) for k in ('key','generation','commit','task','evaluation_id','snapshot_sha256')):
                atomic_json(destination,payload)
                os.chown(destination,entry['uid'],entry['gid'])
            wake=Path(entry['root'])/'review-dispatch.json'
            atomic_json(wake,{k:payload[k] for k in ('key','generation','state','task','commit','evaluation_id','snapshot_sha256')},0o644)
            dispatcher.publish_author_feedback({},payload,config)
        except (OSError,ValueError,KeyError,TypeError) as error:
            pending[path.stem]={'state':'waiting_author','stage':'author_container_preflight',
                                'validation_error_type':type(error).__name__}
    atomic_json(Path(state)/'pending-selftest-reviews.json',pending)
    return pending
