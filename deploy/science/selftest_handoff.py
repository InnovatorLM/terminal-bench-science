"""Operator handoff from verified author iteration to immutable review execution."""
import hashlib
import json
import os
from pathlib import Path
import time
import subprocess
import sys
from author_submission_gate import read_operator_file, validate_receipt, REQUIRED_ORDER
from evaluation_queue import W, atomic_json, root_file, validated_template


class AuthorEvidenceRequired(ValueError):
    """An author can fix this without requesting another platform intervention."""


def qualify(work, state, observation, template):
    from selftest_registration import prepare
    from selftest_iteration import validate
    from selftest_budget_admission import assess_budget, assert_current_idea_active
    from selftest_review_supplement import effective_request
    status=json.loads(read_operator_file(work/'status.json'))
    request,supplements=effective_request(work)
    try:
        assert_current_idea_active(work,status,observation)
    except ValueError as error:
        if str(error).startswith('Idea abandoned;'):
            raise AuthorEvidenceRequired(str(error)) from error
        raise
    idea=status.get('idea_id');role=status.get('role')
    if not request.get('request_review') or not idea or request.get('idea_id')!=idea:
        raise AuthorEvidenceRequired('Explicit idea-bound review request required')
    if request.get('initial_review') or not request.get('iteration'):
        raise AuthorEvidenceRequired('Prior selftest, changed committed task and iteration explanation required')
    try:
        linkage=validate(request['iteration'],status,state,role)
    except (ValueError,FileNotFoundError) as error:
        raise AuthorEvidenceRequired('Fix prior selftest linkage, same-task commit and iteration explanation') from error
    qualified=work/'handoff-qualified.json'
    if qualified.exists():
        receipt=json.loads(read_operator_file(qualified))
        if receipt.get('schema_version') in (3,4):
            validate_receipt(receipt,status['source_commit'],status['source_task_path'])
            if any(str(path) not in {item['path'] for item in receipt['evidence_files']} for path in supplements):
                raise ValueError('Qualified receipt lacks immutable review supplement')
            prepare(work,status['source_commit'],status['source_task_path'],template)
            return receipt
        legacy=work/'handoff-qualified-legacy.json'
        if not legacy.exists():atomic_json(legacy,receipt)
    bridge=prepare(work,status['source_commit'],status['source_task_path'],template)
    # Recheck previous frozen bytes and completed execution; do not trust an
    # earlier derived Boolean or the author's explanation alone.
    from check_harbor_task import validate_mechanical_check
    mechanical=work/'mechanical.json'
    validate_mechanical_check(mechanical,Path(bridge['snapshot']),hashlib.sha256(read_operator_file(mechanical)).hexdigest())
    clock=work/'handoff-clock.json'
    budget=assess_budget(work,state,status,observation,time.time())
    # The author-owned solution is part of the verified frozen Git tree and was
    # executed by the passing oracle control. Bind its actual bytes explicitly;
    # this proves execution, not scientific quality or independent model solving.
    solution=Path(bridge['snapshot'])/'solution/solve.sh'
    if not read_operator_file(solution).strip():
        raise AuthorEvidenceRequired('Missing executed author solution')
    proof=dict(bridge=bridge,iteration=linkage,budget=budget,
               author_difficulty_viability_claim='Explicit request for review after iteration; independently assessed by evaluator',
               scientific_acceptance=False)
    evidence=work/'handoff-evidence.json';atomic_json(evidence,proof)
    previous = state/linkage['previous_job']/'status.json'
    files=[work/'status.json',work/'request.json',work/'build/receipt.json',mechanical,previous,clock,evidence,solution,*supplements]
    if budget.get('admission_file'):files.append(Path(budget['admission_file']))
    check_files={'schema':[mechanical], 'container_build':[work/'build/receipt.json'],
                 'container_start':[work/'status.json'], 'author_solve':[solution,work/'status.json'],
                 'verifier_positive':[work/'status.json'], 'verifier_negative':[work/'status.json'],
                 'post_iteration_rerun':[previous,work/'request.json',work/'status.json',evidence,*supplements]}
    receipt=dict(schema_version=4 if budget['budget_status']=='legacy_budget_unknown' else 3,origin='automatic_selftest_handoff',commit=status['source_commit'],task=status['source_task_path'],
                 snapshot_sha256=bridge['snapshot_sha256'],runtime_image=template['environment']['kwargs']['runtime_image'],
                 checks={key:True for key in REQUIRED_ORDER},pre_submission_order=list(REQUIRED_ORDER),
                 iteration_applied=True,active_iteration_seconds=budget['active_iteration_seconds_upper_bound'],
                 budget_status=budget['budget_status'],
                 active_time_kind='unknown_legacy_history_completed_version_only' if budget['budget_status']=='legacy_budget_unknown' else 'conservative_bound_at_selftest_completion_minus_operator_ACS_intervals',
                 difficulty_viable=True,difficulty_viability_origin='author_explicit_review_request_not_scientific_attestation',
                 scientific_acceptance=False,
                 handoff_evidence=str(evidence),
                 check_evidence={key:[str(p) for p in paths] for key,paths in check_files.items()},
                 evidence_files=[dict(path=str(p),sha256=hashlib.sha256(read_operator_file(p)).hexdigest()) for p in files])
    validate_receipt(receipt,status['source_commit'],status['source_task_path'])
    atomic_json(qualified,receipt)
    return receipt


def handoff(work, state, entry, universe):
    from codex_evaluation_template import prepare
    work=Path(work);state=Path(state);universe=Path(universe)
    atomic_json(work/'handoff-progress.json',{'stage':'validate_selftest'})
    status=json.loads(read_operator_file(work/'status.json'))
    if status.get('status')!='selftest_passed':raise ValueError('Completed successful selftest required')
    base=json.loads(root_file(W/'.config/science-evaluation/calibration-v1-template.json').read_text())
    build=json.loads(read_operator_file(work/'build/receipt.json'))
    template=prepare(base,build)
    atomic_json(work/'handoff-progress.json',{'stage':'verify_iteration_and_budget'})
    receipt=qualify(work,state,state.parent/'command-watchdog/iteration-observations'/(status['role']+'.json'),template)
    template_path=work/'review-template.json';atomic_json(template_path,template)
    settings=universe/'.config/science-evaluation'
    queue=json.loads(root_file(settings/'queue.json').read_text())
    registry=json.loads(root_file(queue['registry']).read_text())
    task=status['source_task_path'];commit=status['source_commit']
    digest=hashlib.sha256((commit+'\0'+task).encode()).hexdigest()[:32]
    identifier=Path(task).name[:20]+'-'+digest
    existing=[(key,row) for key,row in registry.items() if row.get('enabled',True) and row.get('source_commit')==commit and row.get('source_task_path')==task and row.get('dispatch','solver')=='solver']
    if len(existing)>1:raise ValueError('Ambiguous existing source execution')
    atomic_json(work/'handoff-progress.json',{'stage':'immutable_execution_registration'})
    if existing:
        identifier,registered=existing[0]
        registered_template=validated_template(registered,{'snapshot_sha256':receipt['snapshot_sha256']})
        agent=registered_template['agents'][0]
        valid_codex=(agent.get('name')=='codex-preinstall' or
                     (agent.get('name') is None and agent.get('import_path')=='universe_codex:UniverseCodex'))
        if (not valid_codex or
                any(registered_template['environment']['kwargs'].get(k)!=template['environment']['kwargs'][k] for k in ('runtime_image','verifier_image'))):
            raise ValueError('Existing source registration uses another runtime publication')
    else:
        repository=(Path(entry['root'])/'workspace'/status['source_repository']).resolve(strict=True)
        if not repository.is_relative_to((Path(entry['root'])/'workspace').resolve()):raise ValueError('Source repository escaped author workspace')
        git_config=work/'handoff-git.config'
        git_config.write_text('[safe]\n\tdirectory = '+json.dumps(str(repository))+'\n');git_config.chmod(0o600)
        env=os.environ.copy();env.update(GEWU_UNIVERSE_ROOT=str(universe),GIT_CONFIG_GLOBAL=str(git_config),GIT_CONFIG_NOSYSTEM='1',GIT_NO_REPLACE_OBJECTS='1')
        command=[sys.executable,str(Path(__file__).with_name('register_evaluation.py')),
                 '--evaluation-id',identifier,'--repo',str(repository),'--commit',commit,'--task-path',task,
                 '--template',str(template_path),'--build-receipt',str(work/'build/receipt.json'),'--selftest-work',str(work)]
        with (work/'handoff-registration.log').open('w') as log:
            os.chmod(log.name,0o600)
            subprocess.run(command,env=env,stdout=log,stderr=log,timeout=120,check=True)
    # Publish eligibility last: dispatcher must never see qualified-but-not-
    # registered execution during a successful automatic handoff.
    atomic_json(work/'handoff-progress.json',{'stage':'publish_review_eligibility'})
    directory=universe/'.state/science-author-preflight';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    key=commit+'-'+hashlib.sha256(task.encode()).hexdigest()[:16]
    atomic_json(directory/(key+'.json'),receipt)
    # Direct local review queue is the durable fallback when Forgejo has no
    # corresponding PR yet. It still binds the evaluator to the immutable
    # commit, task path and snapshot; no review result is inferred here.
    review_queue=universe/'.state/gewu-multiverse/review-requests'
    review_queue.mkdir(mode=0o700,parents=True,exist_ok=True)
    budget=json.loads(read_operator_file(work/'handoff-evidence.json'))['budget']
    budget_summary=dict(budget_status=receipt.get('budget_status',budget.get('budget_status','verified_within_budget')),
                        active_iteration_seconds=receipt['active_iteration_seconds'],
                        iteration_cutoff_at=budget.get('iteration_cutoff_at',status['completed_at']),
                        scope=budget.get('scope','completed_frozen_version_only'))
    atomic_json(review_queue/(key+'.json'),dict(schema_version=1,state='ready_for_independent_review',
        task=task,commit=commit,snapshot_sha256=receipt['snapshot_sha256'],evaluation_id=identifier,
        author_role=status['role'],job_id=status['job_id'],idea_id=status['idea_id'],
        received_at=status['received_at'],budget_summary=budget_summary,scientific_acceptance=False))
    return dict(state='ready_for_independent_review',evaluation_id=identifier,
                snapshot_sha256=receipt['snapshot_sha256'],budget_summary=budget_summary,scientific_acceptance=False)


def reconcile(work, state, entry, universe):
    from selftest_review_supplement import operator_lock
    work=Path(work)
    with operator_lock(work/'handoff.lock'):
        return _reconcile(work,state,entry,universe)


def _reconcile(work, state, entry, universe):
    from selftest_feedback import publish_handoff
    work=Path(work)
    request=work/'request.json'
    if not request.exists():
        return
    from selftest_review_supplement import effective_request
    request_data,supplements=effective_request(work)
    if not request_data.get('request_review'):
        # A successful author self-test is not silently dropped.  Publish a
        # durable, actionable handoff receipt so the author can explicitly
        # request independent review with the required iteration linkage.
        status=json.loads(read_operator_file(work/'status.json')) if (work/'status.json').exists() else {}
        if status.get('status')=='selftest_passed':
            result={'state':'waiting_author','stage':'review_request_required',
                    'reason':'ACS selftest passed; use role-acs-selftest request-review JOB --previous-job PRIOR --iteration-note NOTE to request review of this frozen execution without rerunning ACS. The original job must already bind a stable idea and verified iteration.',
                    'scientific_acceptance':False}
            atomic_json(work/'handoff.json',result)
            status['review_handoff']=result
            destination=Path(entry['root'])/'selftest/results'/(status['job_id']+'.json')
            atomic_json(destination,status,0o640); os.chown(destination,0,entry['gid'])
            publish_handoff(work,state,entry,status,result)
        return
    output=work/'handoff.json'
    # Restoring an already accepted commit is a handoff, not another review.
    # Check the original signed identity before budget/registration gates; do
    # not manufacture a new clock exemption or repeat cloud evaluations.
    from completed_review_reuse import lookup, record as record_reuse
    current=json.loads(read_operator_file(work/'status.json'))
    completed=lookup(current,universe)
    if completed is not None:
        record_reuse(work,current,completed)
        atomic_json(output,completed)
        visible=dict(current,review_handoff=completed)
        destination=Path(entry['root'])/'selftest/results'/(current['job_id']+'.json')
        atomic_json(destination,visible,0o640);os.chown(destination,0,entry['gid'])
        publish_handoff(work,state,entry,current,completed)
        return
    if output.exists() and json.loads(read_operator_file(output)).get('state')=='ready_for_independent_review':
        # A cached success from the legacy first-review bypass is not evidence.
        try:
            current=json.loads(read_operator_file(work/'status.json'))
            from selftest_budget_admission import assert_current_idea_active
            assert_current_idea_active(work,current)
            validate_receipt(json.loads(read_operator_file(work/'handoff-qualified.json')),current['source_commit'],current['source_task_path'])
        except (OSError,ValueError,KeyError,TypeError):pass
        else:
            publish_handoff(work,state,entry,current,json.loads(read_operator_file(output)))
            return
    status=json.loads(read_operator_file(work/'status.json'))
    if status.get('status') not in ('selftest_passed','selftest_failed','failed','interrupted'):return
    try:
        if status.get('failure_owner')=='platform' and status.get('error_class')=='platform_base_image_resolution':
            result=dict(state='waiting_platform',stage='base_image_resolution',
                        reason=status['remediation'],scientific_acceptance=False)
        else:result=handoff(work,Path(state),entry,universe)
    except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError) as error:
        from selftest_budget import BudgetEvidenceRequired
        progress=work/'handoff-progress.json'
        stage=json.loads(read_operator_file(progress)).get('stage','verify_iteration_and_budget') if progress.exists() else 'verify_iteration_and_budget'
        guidance={'validate_selftest':'Complete and repair the source-bound container selftest before requesting review.',
                  'verify_iteration_and_budget':'Check stable idea ID, linked prior execution, observed idea clock and 48-hour budget evidence.',
                  'immutable_execution_registration':'Platform must finish exact-version image and snapshot registration; keep the source commit available.',
                  'publish_review_eligibility':'Platform must publish the verified eligibility receipt.'}
        stage=stage if stage in guidance else 'verify_iteration_and_budget'
        status=json.loads(read_operator_file(work/'status.json'))
        result=dict(state='waiting_author' if isinstance(error,AuthorEvidenceRequired) or status.get('status') in ('selftest_failed','failed') else 'waiting_platform',
                    stage=stage,reason=guidance[stage],error_type=type(error).__name__,scientific_acceptance=False)
        if isinstance(error,BudgetEvidenceRequired):result=error.handoff()
    atomic_json(output,result)
    status=json.loads(read_operator_file(work/'status.json'));status['review_handoff']=result
    destination=Path(entry['root'])/'selftest/results'/(status['job_id']+'.json')
    atomic_json(destination,status,0o640);os.chown(destination,0,entry['gid'])
    publish_handoff(work,state,entry,status,result)
