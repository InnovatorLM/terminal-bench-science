"""Real local evidence/receipt writes; synthesized cloud controls, no API calls."""
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from evaluation_queue import W,digest_tree
from build_task_images import REGISTRY,HARNESS,receipt_labels
from selftest_handoff import qualify,reconcile
from author_submission_gate import validate_receipt
from selftest_clients import install


class HandoffTests(unittest.TestCase):
    def fixture(self,root):
        state=root/'state';work=state/('b'*32);task=work/'source/proof'
        for name in ['environment','tests']:
            (task/name).mkdir(parents=True);(task/name/'data').write_text(name)
        (task/'solution').mkdir();(task/'solution/solve.sh').write_text('#!/bin/sh\nexit 0\n')
        prior=state/('a'*32);before=prior/'source/proof';before.mkdir(parents=True);(before/'data').write_text('old task')
        old=dict(schema_version=2,role='author-01',idea_id='idea',job_id=prior.name,status='selftest_failed',source_git_tree_verified=True,source_repository='repo',source_task_path='tasks/proof',received_at=20,completed_at=50,execution_task_path='proof',source_commit='d'*40,snapshot_sha256=digest_tree(before),trials=[dict(cleanup_confirmed=True,acs_started_at=30,acs_finished_at=40),dict(cleanup_confirmed=True,acs_started_at=40,acs_finished_at=50)])
        (prior/'status.json').write_text(json.dumps(old))
        sha=digest_tree(task);execution='e'*40
        receipt=dict(source_commit=execution,source_task_path='proof',task_name='proof',snapshot_sha256=sha,published=True,registry_config_linkage_verified=True,scientific_acceptance=False,harness_versions=HARNESS,environment_context_sha256=digest_tree(task/'environment'),tests_context_sha256=digest_tree(task/'tests'),runtime_adapter_context_sha256='c'*64,verifier_effective_context_sha256='f'*64,images={})
        for role,context in [('official',receipt['environment_context_sha256']),('runtime','c'*64),('verifier','f'*64)]:
            receipt['images'][role]=dict(published_digest=REGISTRY+'/proof'+('-verifier' if role=='verifier' else '')+'@sha256:'+'a'*64,image_config_id='sha256:'+'b'*64,labels=receipt_labels(execution,sha,context,role))
        image=receipt['images']['runtime']['published_digest'];template={'environment':{'kwargs':dict(runtime_image=image,verifier_image=receipt['images']['verifier']['published_digest'],pod_overrides={'spec':{'containers':[{'image':image}]}})}}
        status=dict(schema_version=2,status='selftest_passed',role='author-01',idea_id='idea',job_id=work.name,source_commit='c'*40,source_task_path='tasks/proof',source_repository='repo',source_git_tree_verified=True,quality_acceptance=False,execution_commit=execution,execution_task_path='proof',snapshot_sha256=sha,received_at=60,completed_at=100,images={k:v['published_digest'] for k,v in receipt['images'].items()},trials=[dict(agent=a,exit_code=0,cleanup_confirmed=True,acs_started_at=70+i*10,acs_finished_at=80+i*10,results=[dict(exception_type=None,rewards={'reward':reward})]) for i,(a,reward) in enumerate([('oracle',1),('nop',0)])])
        (work/'build').mkdir();(work/'build/receipt.json').write_text(json.dumps(receipt));(work/'status.json').write_text(json.dumps(status))
        request=dict(request_review=True,idea_id='idea',iteration=dict(previous_job=prior.name,note='Fixed the verifier using the prior execution diagnostic.'))
        (work/'request.json').write_text(json.dumps(request))
        (work/'mechanical.json').write_text(json.dumps(dict(snapshot_sha256=sha,schema_parsed=True,required_files_nonempty=True,shell_syntax_valid=True,task_code_executed=False,scientific_acceptance=False)))
        obs=root/'observed.json';obs.write_text(json.dumps(dict(role='author-01',observed_at=95,ideas={'idea':dict(first_observed_at=10,status='active',events=[dict(action='start',at=0)])})))
        return state,work,obs,template,status

    def test_verified_iteration_qualifies_and_retry_preserves_signed_evidence(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status=self.fixture(Path(raw))
            with patch('selftest_handoff.time.time',return_value=100):receipt=qualify(work,state,obs,template)
            self.assertEqual(receipt['active_iteration_seconds'],60)
            self.assertFalse(receipt['scientific_acceptance'])
            self.assertEqual(receipt['difficulty_viability_origin'],'author_explicit_review_request_not_scientific_attestation')
            before=(work/'handoff-clock.json').read_bytes();obs.write_text('{}')
            self.assertEqual(qualify(work,state,obs,template),receipt)
            self.assertEqual((work/'handoff-clock.json').read_bytes(),before)
            validate_receipt(receipt,status['source_commit'],status['source_task_path'])
            (work/'source/proof/tests/data').write_text('tampered')
            with self.assertRaises(ValueError):qualify(work,state,obs,template)

    def test_missing_observation_or_wrong_idea_never_qualifies(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status=self.fixture(Path(raw))
            from selftest_budget import BudgetEvidenceRequired
            with self.assertRaises(BudgetEvidenceRequired):qualify(work,state,Path(raw)/'missing',template)
            status['idea_id']='another';(work/'status.json').write_text(json.dumps(status))
            with self.assertRaisesRegex(ValueError,'idea-bound'):qualify(work,state,obs,template)

    def test_initial_review_cannot_invent_iteration_or_zero_budget(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status=self.fixture(root)
            request=json.loads((work/'request.json').read_text())
            request.pop('iteration');request['initial_review']=True
            (work/'request.json').write_text(json.dumps(request))
            with self.assertRaisesRegex(ValueError,'Prior selftest'):qualify(work,state,obs,template)
            self.assertFalse((work/'handoff-qualified.json').exists())
            role=root/'role';(role/'selftest/results').mkdir(parents=True)
            from selftest_handoff import AuthorEvidenceRequired
            with patch('selftest_handoff.handoff',side_effect=AuthorEvidenceRequired('missing linkage')):
                reconcile(work,state,dict(root=str(role),gid=0),root/'universe')
            self.assertEqual(json.loads((work/'handoff.json').read_text())['state'],'waiting_author')

    def test_automatic_checks_carry_solution_and_iteration_evidence(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status=self.fixture(Path(raw))
            with patch('selftest_handoff.time.time',return_value=100):receipt=qualify(work,state,obs,template)
            self.assertIn(str(work/'source/proof/solution/solve.sh'),receipt['check_evidence']['author_solve'])
            self.assertIn(str(state/('a'*32)/'status.json'),receipt['check_evidence']['post_iteration_rerun'])
            receipt['check_evidence']['post_iteration_rerun']=[]
            with self.assertRaisesRegex(ValueError,'Each automatic check'):validate_receipt(receipt,status['source_commit'],status['source_task_path'])

    def test_legacy_automatic_receipt_must_be_requalified(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status=self.fixture(Path(raw))
            with patch('selftest_handoff.time.time',return_value=100):receipt=qualify(work,state,obs,template)
            receipt['schema_version']=2
            with self.assertRaisesRegex(ValueError,'Legacy automatic'):validate_receipt(receipt,status['source_commit'],status['source_task_path'])
            (work/'handoff-qualified.json').write_text(json.dumps(receipt))
            with patch('selftest_handoff.time.time',return_value=100):updated=qualify(work,state,obs,template)
            self.assertEqual(updated['schema_version'],3)
            self.assertEqual(json.loads((work/'handoff-qualified-legacy.json').read_text())['schema_version'],2)

    def test_reconcile_reports_retry_without_rewriting_source_status(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status=self.fixture(root)
            role=root/'role';(role/'selftest/results').mkdir(parents=True);entry=dict(root=str(role),gid=0)
            original=(work/'status.json').read_bytes()
            with patch('selftest_handoff.handoff',side_effect=ValueError('no clock yet')):reconcile(work,state,entry,root/'universe')
            self.assertEqual(json.loads((work/'handoff.json').read_text())['state'],'waiting_platform')
            with patch('selftest_handoff.time.time',return_value=100):qualify(work,state,obs,template)
            with patch('selftest_handoff.handoff',return_value=dict(state='ready_for_independent_review',scientific_acceptance=False)) as run:
                reconcile(work,state,entry,root/'universe');reconcile(work,state,entry,root/'universe');self.assertEqual(run.call_count,1)
            self.assertEqual((work/'status.json').read_bytes(),original)
            self.assertEqual((role/'selftest/results'/(work.name+'.json')).stat().st_mode&0o777,0o640)

    def test_platform_image_resolution_failure_is_not_sent_back_for_author_edits(self):
        from build_task_images import BASE_RESOLUTION_FAILURE
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status=self.fixture(root)
            role=root/'role';(role/'selftest/results').mkdir(parents=True)
            status.update(BASE_RESOLUTION_FAILURE,status='failed');(work/'status.json').write_text(json.dumps(status))
            with patch('selftest_handoff.handoff') as handoff:
                reconcile(work,state,dict(root=str(role),gid=0),root/'universe');handoff.assert_not_called()
            result=json.loads((role/'selftest/results'/(work.name+'.json')).read_text())
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['review_handoff']['state'],'waiting_platform')
            self.assertFalse(result['retry_requires_task_change'])

    def test_clock_history_gap_is_actionable_platform_work_not_an_author_rerun(self):
        from selftest_budget import BudgetEvidenceRequired
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status=self.fixture(root)
            role=root/'role';(role/'selftest/results').mkdir(parents=True)
            original=(work/'status.json').read_bytes()
            error=BudgetEvidenceRequired('iteration_observation_history_gap',
                'Recover authentic clock history.',first_observed_at=80,earliest_selftest_received_at=20)
            with patch('selftest_handoff.handoff',side_effect=error):
                reconcile(work,state,dict(root=str(role),gid=0),root/'universe')
            result=json.loads((role/'selftest/results'/(work.name+'.json')).read_text())['review_handoff']
            self.assertEqual(result['state'],'waiting_platform')
            self.assertEqual(result['reason'],'Recover authentic clock history.')
            self.assertEqual(result['evidence_gap']['earliest_selftest_received_at'],20)
            self.assertFalse(result['retry_requires_task_change']);self.assertFalse(result['selftest_rerun_required'])
            self.assertEqual((work/'status.json').read_bytes(),original)

    def test_incomplete_root_status_does_not_overwrite_interruption_receipt(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status=self.fixture(root)
            role=root/'role';results=role/'selftest/results';results.mkdir(parents=True)
            status['status']='building';(work/'status.json').write_text(json.dumps(status))
            destination=results/(work.name+'.json');destination.write_text('{"status":"interrupted"}')
            reconcile(work,state,dict(root=str(role),gid=0),root/'universe')
            self.assertEqual(json.loads(destination.read_text())['status'],'interrupted')
            self.assertFalse((work/'handoff.json').exists())

    def test_actual_registration_then_gate_publication_and_idempotent_retry(self):
        from selftest_handoff import handoff
        from check_harbor_task import inspect_task
        from author_submission_gate import approved
        from review_dispatcher import registered_execution
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status=self.fixture(root)
            snapshot=work/'source/proof'
            # Existing upstream simple fixture; no new scientific question.
            shutil.copytree(W/'acs-harbor-test/tasks/openssl-selfsigned-cert',snapshot,dirs_exist_ok=True)
            (snapshot/'solution/solve.sh').chmod(0o700)
            role=root/'role';repo=role/'workspace/repo';destination=repo/'tasks/proof'
            shutil.copytree(snapshot,destination)
            env=dict(os.environ,GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null')
            for command in [['init','-q'],['add','.'],['-c','user.name=fixture','-c','user.email=fixture@localhost','commit','-qm','Upstream platform fixture']]:
                subprocess.run(['git','-C',str(repo),*command],env=env,check=True,stdout=subprocess.DEVNULL)
            commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],env=env,text=True).strip()
            status.update(source_commit=commit,snapshot_sha256=digest_tree(snapshot))
            build=json.loads((work/'build/receipt.json').read_text())
            build.update(harness='codex',harness_versions={'codex':'0.164.0','rg':'14.1.1'},snapshot_sha256=status['snapshot_sha256'],environment_context_sha256=digest_tree(snapshot/'environment'),tests_context_sha256=digest_tree(snapshot/'tests'))
            for kind,context in [('official',build['environment_context_sha256']),('runtime',build['runtime_adapter_context_sha256']),('verifier',build['verifier_effective_context_sha256'])]:
                build['images'][kind]['labels']=receipt_labels(status['execution_commit'],status['snapshot_sha256'],context,kind)
            (work/'build/receipt.json').write_text(json.dumps(build));(work/'status.json').write_text(json.dumps(status))
            (work/'mechanical.json').write_text(json.dumps(inspect_task(snapshot)))
            observed=state.parent/'command-watchdog/iteration-observations/author-01.json';observed.parent.mkdir(parents=True);observed.write_bytes(obs.read_bytes())
            universe=root/'universe';settings=universe/'.config/science-evaluation';settings.mkdir(parents=True)
            queue_state=universe/'.state/evaluation';(queue_state/'registered').mkdir(parents=True)
            inbox=root/'inbox';inbox.mkdir();registry=root/'registry.json';registry.write_text('{}')
            (settings/'queue.json').write_text(json.dumps(dict(state=str(queue_state),registry=str(registry),role_queues=[dict(inbox=str(inbox),evaluator_uid=21012)])))
            entry=dict(root=str(role),gid=0)
            with patch('selftest_handoff.time.time',return_value=100),patch('selftest_handoff.subprocess.run',side_effect=subprocess.CalledProcessError(1,'mock registration failure')):
                with self.assertRaises(subprocess.CalledProcessError):handoff(work,state,entry,universe)
            with patch.dict(os.environ,{'GEWU_UNIVERSE_ROOT':str(universe)}):
                self.assertFalse(approved(commit,'tasks/proof'))
                # Actual subprocess, Git freeze, schema checks, immutable catalog,
                # identity bridge and final eligibility file. Cloud controls in
                # this fixture are synthesized, not real ACS/model results.
                result=handoff(work,state,entry,universe)
                self.assertTrue(approved(commit,'tasks/proof'))
                self.assertEqual(registered_execution(commit,'tasks/proof')['evaluation_id'],result['evaluation_id'])
                self.assertEqual(handoff(work,state,entry,universe),result)
                from selftest_review_dispatch import consume
                evaluator=root/'evaluator';(evaluator/'home/role').mkdir(parents=True)
                config={'roles':{'author-01':dict(entry,uid=os.getuid()),
                                 'evaluator-01':dict(root=str(evaluator),uid=os.getuid(),gid=os.getgid())}}
                dispatcher_state=universe/'.state/gewu-multiverse/review-dispatcher';dispatcher_state.mkdir()
                ledger={};consume(config,ledger,universe,dispatcher_state)
                self.assertEqual(len(ledger),1)
                assignment=json.loads((evaluator/'home/role/review-assignment.json').read_text())
                self.assertEqual(assignment['evaluation_id'],result['evaluation_id'])
                self.assertEqual(assignment['commit'],commit)
                consume(config,ledger,universe,dispatcher_state)
                self.assertEqual(len(ledger),1)
            self.assertEqual(len(json.loads(registry.read_text())),1)
            self.assertFalse(result['scientific_acceptance'])

    def test_installs_both_current_author_clients(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);(root/'usr/local/bin').mkdir(parents=True)
            install({'roles':{'author-01':{'root':str(root)},'evaluator-01':{}}})
            self.assertEqual((root/'usr/local/bin/role-acs-selftest').read_bytes(),Path(__file__).with_name('role_acs_selftest.py').read_bytes())
            self.assertTrue((root/'usr/local/bin/author-iteration-clock').exists())


if __name__=='__main__':unittest.main()
