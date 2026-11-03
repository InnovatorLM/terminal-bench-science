import copy,json,tempfile,unittest
from pathlib import Path
from evaluation_queue import W,aggregate
from execution_evidence import inspect,materialize_private_trajectory
from acs_helpers.pod_evidence import execution_identity,observe,validate,capture,merge_captured
from publish_evidence import publish

class EvidenceTests(unittest.TestCase):
 def config(self):
  return {'job_name':'tbs-universe-linfeng-test','datasets':[{'task_names':['task']}], 'environment':{'kwargs':{'runtime_image':'acr/runtime@sha256:'+'a'*64,'verifier_image':'acr/verifier@sha256:'+'b'*64}},'agents':[{'env':{'OPENAI_API_KEY':'SECRET-CANARY'}}]}
 def pod(self,c,role):
  return {'metadata':{'name':role,'uid':role+'-uid','labels':{'evaluation-run':c['job_name'],'universe-role':role},'annotations':{'secret':'SECRET-CANARY'}},'spec':{'nodeName':'virtual-kubelet-cn-beijing-i','containers':[{'name':'main','image':c['environment']['kwargs'][role+'_image'],'env':[{'name':'key','value':'SECRET-CANARY'}],'command':['SECRET-CANARY'],'resources':{'requests':{'cpu':'4','memory':'8Gi'},'limits':{'cpu':'4','memory':'8Gi'}}}]},'status':{'phase':'Running','containerStatuses':[{'name':'main','imageID':'containerd://sha256:'+'c'*64,'state':{'terminated':{'exitCode':0,'message':'SECRET-CANARY'}}}]}}
 def report(self,c):
  r={'run_id':c['job_name'],'execution_identity':execution_identity(c),'cleanup':{'confirmed':True},'harbor_exit_code':0,'stats':{'n_completed_trials':1,'n_errored_trials':0,'evals':{'test':{'n_errors':0,'reward_stats':{'reward':{'1.0':['trial']}}}}}}
  observe({'pods':[self.pod(c,role) for role in ('runtime','verifier')]},r);return r
 def fixture(self,root,c):
  run=root/c['job_name'];trial=run/'task__trial'
  for name,value in {'result.json':{'finished_at':'now','verifier_result':{'rewards':{'reward':1}},'exception_info':None},'config.json':{'agent':{'name':'codex','model_name':'openai/deepseek-v4.1-flash-1017','env':{'OPENAI_API_KEY':'SECRET-CANARY'}}},'agent/trajectory.json':{'schema_version':'ATIF-v1.7','steps':[{'source':'agent','message':'SECRET-CANARY'}]}}.items():
   path=trial/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
  (trial/'verifier').mkdir();(trial/'verifier/reward.txt').write_text('1');(trial/'verifier/test-stdout.txt').write_text('passed SECRET-CANARY');(run/'result.json').write_text('{}');return run/'result.json',trial
 def test_pod_allowlist_and_live_status_required(self):
  c=self.config();r=self.report(c);self.assertTrue(validate(r,c)['complete']);self.assertNotIn('SECRET-CANARY',json.dumps(r))
  wrong=copy.deepcopy(c);wrong['job_name']='other';self.assertFalse(validate(r,wrong)['complete'])
  for field in ('image_id','resources'):
   changed=copy.deepcopy(r)
   for record in changed['pod_observations'].values():
    for state in record['states']:state['pod']['containers'][0].pop(field)
   self.assertFalse(validate(changed,c)['complete'])
  foreign=self.pod(c,'runtime');foreign['metadata']['labels']['evaluation-run']='other'
  with self.assertRaises(ValueError):observe({'pods':[foreign]},r)
 def test_lifecycle_journal_preserves_short_lived_pods_without_polling(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();r={'run_id':c['job_name'],'execution_identity':execution_identity(c)};known={}
   for role in ('runtime','verifier'):capture(self.pod(c,role),root,'before_stop')
   merge_captured(r,root,known);self.assertTrue(validate(r,c)['complete']);self.assertEqual(len(known),2)
   self.assertNotIn('SECRET-CANARY',json.dumps(r))
   merge_captured(r,root,known);self.assertTrue(all(len(v['states'])==1 for v in r['pod_observations'].values()))
   r['run_id']='foreign'
   with self.assertRaises(ValueError):merge_captured(r,root,known)

 def test_shared_verifier_uses_observed_runtime_not_unlaunched_image(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);task=root/'task';task.mkdir();(task/'task.toml').write_text('version="1.0"\n[verifier]\ntimeout_sec=600\n')
   c=self.config();c['datasets']=[{'path':str(root),'task_names':['task']}]
   r={'run_id':c['job_name'],'execution_identity':execution_identity(c)}
   observe({'pods':[self.pod(c,'runtime')]},r)
   self.assertEqual(r['execution_identity']['verifier_mode'],'shared')
   self.assertTrue(validate(r,c)['complete'])

 def test_private_trajectory_bridge_binds_identity_hashes_and_job(self):
  import hashlib
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();c['job_name']='tbs-universe-linfeng-queue-'+'f'*32
   path,trial=self.fixture(root,c);(trial/'agent/trajectory.json').unlink()
   private=root/('f'*32);private.mkdir(mode=0o700)
   entry={'snapshot_sha256':'a'*64,'snapshot':str(root/'snapshot')}
   identity={**entry,'snapshot_path':entry['snapshot'],'provider':'dawnfm','model':'deepseek-v4.1-flash-1017',**c['environment']['kwargs']}
   data={'identity.json':json.dumps(identity).encode(),'trajectory.json':json.dumps({'schema_version':'ATIF-v1.7','steps':[{'source':'agent'}]}).encode(),'mini-swe-agent.trajectory.json':b'{"messages":[]}'}
   for name,value in data.items():(private/name).write_bytes(value)
   retention={'review_ready':True,'snapshot_sha256':entry['snapshot_sha256'],**{key:hashlib.sha256(data[name]).hexdigest() for name,key in [('identity.json','identity_sha256'),('trajectory.json','atif_sha256'),('mini-swe-agent.trajectory.json','raw_sha256')]}}
   (private/'retention.json').write_text(json.dumps(retention))
   self.assertTrue(materialize_private_trajectory(private,path,entry,c))
   self.assertEqual((trial/'agent/trajectory.json').read_bytes(),data['trajectory.json'])
   (private/'trajectory.json').write_text('{}')
   with self.assertRaises(ValueError):materialize_private_trajectory(private,path,entry,c)

 def test_complete_and_missing_evidence_cannot_score_as_complete(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();r=self.report(c);path,trial=self.fixture(root,c)
   self.assertTrue(inspect(r,path,c,'f'*64,True)['complete']);answer=aggregate(r,path,c,'f'*64,True);self.assertEqual(answer['status'],'solver_complete')
   (trial/'agent/trajectory.json').unlink();answer=aggregate(r,path,c,'f'*64,True);self.assertEqual(answer['status'],'incomplete_evidence');self.assertFalse(answer['evidence_complete']);self.assertNotIn(str(root),json.dumps(answer))
 def test_legacy_guard_does_not_fabricate_pod_observations(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   c=self.config();r=self.report(c);path,_=self.fixture(Path(raw),c);del r['pod_observations'];e=inspect(r,path,c,'f'*64,True)
   self.assertFalse(e['complete']);self.assertTrue(e['checks']['trajectory_retained']);self.assertIn('pod_evidence_complete',e['missing'])
 def test_silent_verifier_retains_valid_score_but_missing_log_is_incomplete(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   c=self.config();r=self.report(c);path,trial=self.fixture(Path(raw),c)
   log=trial/'verifier/test-stdout.txt';log.write_text('')
   self.assertTrue(inspect(r,path,c,'f'*64,True)['complete'])
   log.unlink();self.assertFalse(inspect(r,path,c,'f'*64,True)['complete'])
 def test_public_json_fractional_rewards_preserve_real_values(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   c=self.config();c['agents'][0]['import_path']='public_codex:PublicCodex'
   r=self.report(c);path,trial=self.fixture(Path(raw),c)
   result=json.loads((trial/'result.json').read_text());result['verifier_result']['rewards']={'reward':0.25,'accuracy':0.75}
   (trial/'result.json').write_text(json.dumps(result));(trial/'verifier/reward.txt').unlink()
   (trial/'verifier/reward.json').write_text(json.dumps(result['verifier_result']['rewards']))
   (trial/'verifier/test-stdout.txt').write_text('')
   r['stats']['evals']['test']['reward_stats']={'reward':{'0.25':['trial']},'accuracy':{'0.75':['trial']}}
   answer=aggregate(r,path,c,'f'*64,True)
   self.assertEqual(answer['status'],'solver_complete');self.assertEqual(answer['reward'],0.25)
   self.assertEqual(answer['rewards'],{'reward':0.25,'accuracy':0.75})
   receipt=publish(trial,Path(raw)/'packet',['SECRET-CANARY'])
   self.assertTrue(receipt['verifier_available'])
 def test_agent_timeout_remains_pending_cause_review_and_batch_unscored(self):
  from role_evaluation_batch import requests,summary
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();r=self.report(c);path,trial=self.fixture(root,c)
   assignment=dict(key='30:'+'a'*40,generation=0,evaluation_id='example-v1',snapshot_sha256='b'*64,state='reviewing')
   results=root/'results';results.mkdir();jobs=requests(assignment)
   for request in jobs[1:]:
    (results/(request['job_id']+'.json')).write_text(json.dumps(dict(request,status='solver_complete',cleanup_confirmed=True,evidence_complete=True,source_version_bound=True,n_errors=0,reward=1)))
   for exception_type in ('AgentTimeoutError','AgentExecutionTimeoutError'):
    for verifier in (None,{'rewards':{'reward':0}}):
     # Even stale successful aggregate statistics or partial reward must not
     # turn an unadjudicated timeout into an infrastructure or model failure.
     (trial/'result.json').write_text(json.dumps({'finished_at':'now','verifier_result':verifier,'exception_info':{'exception_type':exception_type}}))
     answer=aggregate(r,path,c,'b'*64,True)
     self.assertEqual(answer['status'],'agent_timeout_requires_cause_review')
     self.assertIsNone(answer['reward']);self.assertFalse(answer['quality_acceptance'])
     (results/(jobs[0]['job_id']+'.json')).write_text(json.dumps(dict(jobs[0],**answer)))
     batch=summary(assignment,results)
     self.assertFalse(batch['complete']);self.assertIsNone(batch['pass_rate']);self.assertEqual(batch['valid_trials'],4)
 def test_publish_redacts_and_never_counts_arbitrary_log_as_trajectory(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();_,trial=self.fixture(root,c);packet=root/'published';receipt=publish(trial,packet,['SECRET-CANARY'],gid=21002)
   self.assertTrue(receipt['trajectory_available']);self.assertTrue(receipt['verifier_available'])
   for p in packet.rglob('*'):
    if p.is_file():self.assertNotIn('SECRET-CANARY',p.read_text());self.assertEqual(p.stat().st_mode&0o777,0o640)
   self.assertNotIn('env',json.loads((packet/'config.json').read_text())['agent'])
   (trial/'agent/trajectory.json').unlink();(trial/'agent/mini-swe-agent.txt').write_text('a log');self.assertFalse(publish(trial,root/'log-only')['trajectory_available'])

 def test_timeout_flows_from_harbor_result_through_packet_and_five_run_score(self):
  import hashlib
  from role_evaluation_batch import requests,summary
  import timeout_review
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();c['agents'][0]['override_timeout_sec']=14400
   r=self.report(c);path,trial=self.fixture(root,c)
   value=json.loads((trial/'result.json').read_text())
   value.update(exception_info={'exception_type':'AgentTimeoutError'},
                agent_execution={'started_at':'2026-11-02T10:00:00Z','finished_at':'2026-11-02T14:00:00Z'})
   (trial/'result.json').write_text(json.dumps(value));raw_result=(trial/'result.json').read_bytes()
   r['stats'].update(n_completed_trials=0,n_errored_trials=1)
   r['stats']['evals']['test']['n_errors']=1
   assignment=dict(key='a'*40+':tasks/example',commit='a'*40,task='tasks/example',generation=0,
                   evaluation_id='example',snapshot_sha256='b'*64)
   jobs=requests(assignment);results=root/'results';results.mkdir();(root/'timeout-reviews').mkdir()
   answer=dict(jobs[0],provider='dawnfm',model='deepseek-v4.1-flash-1017',
               **aggregate(r,path,c,'b'*64,True))
   self.assertEqual(answer['status'],'agent_timeout_requires_cause_review')
   self.assertIsNone(answer['reward']);self.assertTrue(answer['pod_evidence_complete'])
   packet=root/'evidence'/jobs[0]['job_id'];manifest=publish(trial,packet,['SECRET-CANARY'])
   answer['published_evidence_manifest_sha256']=hashlib.sha256((packet/'manifest.json').read_bytes()).hexdigest()
   (results/(jobs[0]['job_id']+'.json')).write_text(json.dumps(answer))
   for index,job in enumerate(jobs[1:]):
    (results/(job['job_id']+'.json')).write_text(json.dumps(dict(job,status='solver_complete',
      reward=int(index<3),n_errors=0,evidence_complete=True,source_version_bound=True,cleanup_confirmed=True)))
   self.assertEqual(summary(assignment,results)['valid_trials'],4)
   assessment=dict(classification='difficulty',reason='Fixture represents sustained solving over the entire four-hour budget.',
      observations={k:'Verified from fixture evidence.' for k in timeout_review.CHECKS},
      evidence=[dict(path='agent/trajectory.json',sha256=manifest['files']['agent/trajectory.json']['sha256'],
                     finding='Retained model trace; no platform interruption in this fixture.')])
   timeout_review.record(assignment,jobs[0],results,assessment)
   final=summary(assignment,results)
   self.assertTrue(final['complete']);self.assertEqual(final['pass_rate'],.6)
   self.assertEqual(final['jobs'][0]['score_source'],'timeout_review')
   self.assertIsNone(final['jobs'][0]['raw_reward'])
   self.assertEqual((trial/'result.json').read_bytes(),raw_result)
   self.assertNotIn('SECRET-CANARY',(packet/'agent/trajectory.json').read_text())
 def test_cancelled_trial_keeps_native_session_without_result_or_verifier(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();r=self.report(c);path,trial=self.fixture(root,c)
   (trial/'result.json').unlink();(trial/'agent/trajectory.json').unlink()
   session=trial/'agent/sessions/day/run.jsonl';session.parent.mkdir(parents=True)
   session.write_text('{"type":"message","text":"SECRET-CANARY"}\n{"type":"partial","text":"SECRET-CANARY')
   r['error_type']='TimeoutError';r['stats']={};r['harbor_exit_code']=None
   evidence=inspect(r,path,c,'f'*64,True)
   self.assertFalse(evidence['complete']);self.assertTrue(evidence['agent_logs_retained'])
   self.assertIn(str(session.relative_to(path.parent)),evidence['files'])
   answer=aggregate(r,path,c,'f'*64,True)
   self.assertEqual(answer['status'],'agent_timeout_requires_cause_review')
   self.assertIsNone(answer['reward'])
   packet=root/'partial-packet';receipt=publish(trial,packet,['SECRET-CANARY'])
   self.assertFalse(receipt['trajectory_available'])
   self.assertEqual(receipt['partial_jsonl_lines'],{'agent/sessions/day/run.jsonl':[2]})
   self.assertNotIn('SECRET-CANARY',(packet/'agent/sessions/day/run.jsonl').read_text())
 def test_symlinks_and_corrupt_or_empty_trajectory_are_incomplete(self):
  with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
   root=Path(raw);c=self.config();r=self.report(c);path,trial=self.fixture(root,c);trace=trial/'agent/trajectory.json'
   for content in ('{}','{bad','{"schema_version":"ATIF-v1.7","steps":[]}'):
    trace.write_text(content);self.assertFalse(inspect(r,path,c,'f'*64,True)['complete'])
   trace.unlink();trace.symlink_to(trial/'result.json');self.assertFalse(inspect(r,path,c,'f'*64,True)['complete'])
   with self.assertRaises(ValueError):publish(trial,root/'linked')
   self.assertFalse((root/'linked').exists())
if __name__=='__main__':unittest.main()
