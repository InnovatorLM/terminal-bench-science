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


def qualify(work, state, observation, template):
    from selftest_registration import prepare
    from selftest_iteration import validate
    from selftest_budget import verify
    status=json.loads(read_operator_file(work/'status.json'))
    qualified=work/'handoff-qualified.json'
    if qualified.exists():
        receipt=json.loads(read_operator_file(qualified))
        validate_receipt(receipt,status['source_commit'],status['source_task_path'])
        prepare(work,status['source_commit'],status['source_task_path'],template)
        validate(json.loads(read_operator_file(work/'request.json')).get('iteration'),status,state,status['role'])
        return receipt
    request=json.loads(read_operator_file(work/'request.json'))
    idea=status.get('idea_id');role=status.get('role')
    if not request.get('request_review') or not idea or request.get('idea_id')!=idea:
        raise ValueError('Explicit idea-bound review request required')
    bridge=prepare(work,status['source_commit'],status['source_task_path'],template)
    # Recheck previous frozen bytes and completed execution; do not trust an
    # earlier derived Boolean or the author's explanation alone.
    if request.get('initial_review') is True:
        linkage={'initial_review':True,'changed_committed_task':False,
                 'linked_execution_verified':False,'scientific_acceptance':False}
    else:
        linkage=validate(request.get('iteration'),status,state,role)
    from check_harbor_task import validate_mechanical_check
    mechanical=work/'mechanical.json'
    validate_mechanical_check(mechanical,Path(bridge['snapshot']),hashlib.sha256(read_operator_file(mechanical)).hexdigest())
    clock=work/'handoff-clock.json'
    atomic_json(clock,json.loads(read_operator_file(observation)))
    if request.get('initial_review') is True:
        budget={'within_budget':True,'active_time_seconds_upper_bound':0,
                'active_time_kind':'initial_review_no_prior_iteration'}
    else:
        budget=verify(state,role,idea,clock,time.time())
        if not budget['within_budget']:
            raise ValueError('Conservative active-time bound needs additional pause evidence')
    proof=dict(bridge=bridge,iteration=linkage,budget=budget,
               author_difficulty_viability_claim='Explicit request for review after iteration; independently assessed by evaluator',
               scientific_acceptance=False)
    evidence=work/'handoff-evidence.json';atomic_json(evidence,proof)
    previous = state/linkage['previous_job']/'status.json' if 'previous_job' in linkage else None
    files=[work/'status.json',work/'request.json',work/'build/receipt.json',mechanical,clock,evidence]
    if previous is not None:
        files.insert(4, previous)
    receipt=dict(schema_version=2,commit=status['source_commit'],task=status['source_task_path'],
                 snapshot_sha256=bridge['snapshot_sha256'],runtime_image=template['environment']['kwargs']['runtime_image'],
                 checks={key:True for key in REQUIRED_ORDER},pre_submission_order=list(REQUIRED_ORDER),
                 iteration_applied=True,active_iteration_seconds=budget['active_iteration_seconds_upper_bound'],
                 active_time_kind='conservative_bound_from_observed_idea_start_minus_operator_ACS_intervals',
                 difficulty_viable=True,difficulty_viability_origin='author_explicit_review_request_not_scientific_attestation',
                 scientific_acceptance=False,
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
        if (registered_template['agents'][0].get('name')!='codex-preinstall' or
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
    return dict(state='ready_for_independent_review',evaluation_id=identifier,
                snapshot_sha256=receipt['snapshot_sha256'],scientific_acceptance=False)


def reconcile(work, state, entry, universe):
    work=Path(work)
    request=work/'request.json'
    if not request.exists():
        return
    request_data=json.loads(read_operator_file(request))
    if not request_data.get('request_review'):
        # A successful author self-test is not silently dropped.  Publish a
        # durable, actionable handoff receipt so the author can explicitly
        # request independent review with the required iteration linkage.
        status=json.loads(read_operator_file(work/'status.json')) if (work/'status.json').exists() else {}
        if status.get('status')=='selftest_passed':
            result={'state':'waiting_author','stage':'review_request_required',
                    'reason':'ACS selftest passed; submit an explicit review request with a stable idea ID and result-driven iteration linkage before evaluator assignment.',
                    'scientific_acceptance':False}
            atomic_json(work/'handoff.json',result)
            status['review_handoff']=result
            destination=Path(entry['root'])/'selftest/results'/(status['job_id']+'.json')
            atomic_json(destination,status,0o640); os.chown(destination,0,entry['gid'])
        return
    output=work/'handoff.json'
    if output.exists() and json.loads(read_operator_file(output)).get('state')=='ready_for_independent_review':return
    status=json.loads(read_operator_file(work/'status.json'))
    if status.get('status') not in ('selftest_passed','selftest_failed','failed','interrupted'):return
    try:result=handoff(work,Path(state),entry,universe)
    except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError) as error:
        progress=work/'handoff-progress.json'
        stage=json.loads(read_operator_file(progress)).get('stage','verify_iteration_and_budget') if progress.exists() else 'verify_iteration_and_budget'
        guidance={'validate_selftest':'Complete and repair the source-bound container selftest before requesting review.',
                  'verify_iteration_and_budget':'Check stable idea ID, linked prior execution, observed idea clock and five-hour budget evidence.',
                  'immutable_execution_registration':'Platform must finish exact-version image and snapshot registration; keep the source commit available.',
                  'publish_review_eligibility':'Platform must publish the verified eligibility receipt.'}
        stage=stage if stage in guidance else 'verify_iteration_and_budget'
        status=json.loads(read_operator_file(work/'status.json'))
        result=dict(state='waiting_author' if status.get('status') in ('selftest_failed','failed') else 'waiting_platform',
                    stage=stage,reason=guidance[stage],error_type=type(error).__name__,scientific_acceptance=False)
    atomic_json(output,result)
    status=json.loads(read_operator_file(work/'status.json'));status['review_handoff']=result
    destination=Path(entry['root'])/'selftest/results'/(status['job_id']+'.json')
    atomic_json(destination,status,0o640);os.chown(destination,0,entry['gid'])
