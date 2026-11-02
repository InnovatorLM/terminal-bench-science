import copy,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from evaluation_queue import W
from role_evaluation_batch import requests,summary,install_clients
import timeout_review as m

class TimeoutReviewTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir=W/'.tmp');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.results=self.root/'results';self.results.mkdir()
        (self.root/'timeout-reviews').mkdir()
        self.a=dict(key='review:key',commit='a'*40,task='tasks/science/example',generation=0,
                    evaluation_id='one',snapshot_sha256='b'*64)
        self.jobs=requests(self.a);self.job=self.jobs[0]
        self.packet=self.root/'evidence'/self.job['job_id'];(self.packet/'agent').mkdir(parents=True)
        self.raw=b'{"type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"solve"}}\n'
        (self.packet/'agent/session.jsonl').write_bytes(self.raw)
        self.manifest=json.dumps({'files':{'agent/session.jsonl':{'sha256':m.sha(self.raw)}}}).encode()
        (self.packet/'manifest.json').write_bytes(self.manifest)
        self.result=dict(self.job,status='agent_timeout_requires_cause_review',provider='dawnfm',
                         model='deepseek-v4.1-flash-1017',reward=None,n_errors=1,
                         evidence_complete=False,missing_evidence=['trial_complete','verifier_retained'],
                         source_version_bound=True,cleanup_confirmed=True,pod_evidence_complete=True,agent_logs_retained=True,
                         published_evidence_manifest_sha256=m.sha(self.manifest),
                         timeout_review_context=dict(exception_type='AgentTimeoutError',agent_budget_seconds=14400,agent_elapsed_seconds=14400.1))
        self.path=self.results/(self.job['job_id']+'.json');self.write_result(self.result)
        self.assessment=dict(classification='difficulty',reason='Trajectory shows sustained solving until the complete budget elapsed.',
                             observations={k:'Reviewed the corresponding task, runtime or session evidence.' for k in m.CHECKS},
                             evidence=[dict(path='agent/session.jsonl',sha256=m.sha(self.raw),finding='Last tool attempts continue the solution with no platform error.')])

    def write_result(self,value):self.path.write_text(json.dumps(value))

    def test_difficulty_timeout_counts_as_wrong_without_rewriting_raw_reward(self):
        raw=self.path.read_bytes()
        self.assertEqual(summary(self.a,self.results)['valid_trials'],0)
        path=m.record(self.a,self.job,self.results,self.assessment)
        self.assertEqual(m.record(self.a,self.job,self.results,self.assessment),path)
        for i,job in enumerate(self.jobs[1:]):
            (self.results/(job['job_id']+'.json')).write_text(json.dumps(dict(job,status='solver_complete',
                     cleanup_confirmed=True,evidence_complete=True,source_version_bound=True,n_errors=0,reward=int(i<3))))
        batch=summary(self.a,self.results)
        self.assertEqual(batch['valid_trials'],5);self.assertEqual(batch['pass_rate'],.6)
        self.assertEqual(batch['jobs'][0]['reward'],0);self.assertIsNone(batch['jobs'][0]['raw_reward'])
        self.assertEqual(batch['jobs'][0]['score_source'],'timeout_review')
        self.assertEqual(self.path.read_bytes(),raw)

    def test_defects_platform_and_missing_evidence_stay_unscored(self):
        for kind in ['task_bug','platform_error','insufficient_evidence']:
            with self.subTest(kind=kind):
                assessment=dict(self.assessment,classification=kind)
                if kind=='insufficient_evidence':assessment['evidence']=[]
                p=m.record(self.a,self.job,self.results,assessment)
                result=summary(self.a,self.results)
                self.assertEqual(result['valid_trials'],0)
                diagnosis=result['jobs'][0]['timeout_review']
                self.assertFalse(diagnosis['count_as_model_failure'])
                self.assertEqual(diagnosis['retry_recommended'],kind=='platform_error')
                p.unlink()

    def test_cancelled_connection_failure_can_be_diagnosed_but_never_scored(self):
        self.write_result(dict(self.result,status='cancelled'))
        with self.assertRaises(ValueError):m.record(self.a,self.job,self.results,self.assessment)
        m.record(self.a,self.job,self.results,dict(self.assessment,classification='platform_error'))
        batch=summary(self.a,self.results)
        self.assertEqual(batch['valid_trials'],0)
        self.assertTrue(batch['jobs'][0]['timeout_review']['retry_recommended'])
        self.assertIsNone(batch['jobs'][0]['reward'])

    def test_early_death_missing_resources_other_model_and_wrong_budget_cannot_score(self):
        for changes in [dict(status='infrastructure_failed'),dict(provider='other'),dict(pod_evidence_complete=False),
                        dict(cleanup_confirmed=False),dict(source_version_bound=False),dict(agent_logs_retained=False),
                        dict(timeout_review_context=dict(exception_type='TimeoutError',agent_budget_seconds=14400,agent_elapsed_seconds=14400)),
                        dict(timeout_review_context=dict(exception_type='AgentTimeoutError',agent_budget_seconds=1800,agent_elapsed_seconds=1800)),
                        dict(timeout_review_context=dict(exception_type='AgentTimeoutError',agent_budget_seconds=14400,agent_elapsed_seconds=9000)),
                        dict(published_evidence_manifest_sha256='0'*64)]:
            with self.subTest(changes=changes):
                self.write_result(dict(self.result,**changes))
                with self.assertRaises(ValueError):m.record(self.a,self.job,self.results,self.assessment)

    def test_empty_observations_or_missing_native_trace_are_not_difficulty_evidence(self):
        with self.assertRaises(ValueError):m.record(self.a,self.job,self.results,dict(self.assessment,observations={}))
        with self.assertRaises(ValueError):m.record(self.a,self.job,self.results,dict(self.assessment,evidence=[]))
        changed=copy.deepcopy(self.assessment);changed['evidence'][0]['path']='../other/agent/session.jsonl'
        with self.assertRaises(ValueError):m.record(self.a,self.job,self.results,changed)

    def test_result_trajectory_and_version_changes_invalidate_judgment(self):
        m.record(self.a,self.job,self.results,self.assessment)
        with self.assertRaises(ValueError):m.judgment(dict(self.a,commit='f'*40),self.job,self.results)
        self.write_result(dict(self.result,n_errors=2))
        with self.assertRaises(ValueError):summary(self.a,self.results)
        self.write_result(self.result)
        (self.packet/'agent/session.jsonl').write_bytes(b'changed')
        with self.assertRaises(ValueError):summary(self.a,self.results)

    def test_actual_agent_phase_drives_context_not_pod_age(self):
        trial={'exception_info':{'exception_type':'AgentTimeoutError'},'agent_execution':{
            'started_at':'2026-11-02T10:00:00Z','finished_at':'2026-11-02T14:00:00Z'}}
        context=m.timeout_context(trial,{'agents':[{'override_timeout_sec':14400}]})
        self.assertEqual(context['agent_elapsed_seconds'],14400)
        self.assertIsNone(m.timeout_context({},None)['agent_elapsed_seconds'])

    def test_installed_status_and_timeout_dependencies_import_outside_repo(self):
        root=self.root/'role';directory=root/'usr/local/bin';directory.mkdir(parents=True)
        (root/'evaluation').mkdir()
        install_clients({'roles':{'evaluator-01':{'root':str(root),'uid':os.getuid(),'gid':os.getgid()}}})
        for executable in ['role-review-status','role-evaluation-batch']:
            p=subprocess.run([sys.executable,str(directory/executable),'--help'],cwd=directory,
                 env={'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
        p=subprocess.run([sys.executable,'-c','import timeout_review,role_evaluation_batch'],cwd=directory,
             env={'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)

if __name__=='__main__':unittest.main()
