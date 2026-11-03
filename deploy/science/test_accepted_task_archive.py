import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import accepted_task_archive as ar


class AcceptedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='accepted-archive-test-', dir=ar.WORKSPACE/'.tmp')
        self.base = Path(self.temp.name); self.universe=self.base/'universe'; self.root=self.base/'accepted'
        self.state=self.universe/'.state/gewu-multiverse'; self.task='tasks/science/test-task'
        self.commit='a'*40; self.eid='test-eval'; self.secret='unit-test-secret-NOT-A-REAL-KEY-12345'
        snap=self.state/'evaluation-queue/registered'/self.eid/'test-task'
        for name in ar.REQUIRED:
            p=snap/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture '+name+'\n')
            p.chmod(0o700 if name.endswith('.sh') else 0o600)
        digest=ar.tree_hash(snap);key=self.commit+':'+self.task
        self.row=dict(key=key,state='completed',formal_decision='accepted',scientific_acceptance=True,
            commit=self.commit,task=self.task,snapshot_sha256=digest,evaluation_id=self.eid,role='evaluator-01',
            author_role='author-01',generation=0,reason='Five exact frozen scored trials, operator historical acceptance.',job_id='b'*32)
        receipt=dict(schema_version=1,decision='accepted',operator='test',rationale='Explicit test-only historical acceptance.',
            review_reason_sha256=ar.sha(self.row['reason'].encode()),**{k:self.row[k] for k in ['key','commit','task','snapshot_sha256','evaluation_id','role','generation']})
        self.decision=self.state/'review-dispatcher/formal-decisions'/(ar.sha(key.encode())+'.json');ar.atomic(self.decision,receipt)
        self.row['formal_decision_sha256']=ar.sha(ar.read(self.decision))
        ar.atomic(self.state/'review-dispatcher/ledger.json',{key:self.row})
        ar.atomic(self.universe/'.config/science-evaluation/registry.json',{self.eid:dict(snapshot=str(snap),snapshot_sha256=digest,source_commit=self.commit,source_task_path=self.task)})
        for n,reward in enumerate([1,0,1,0,1],1):self.make_job(n,reward)
        extra=self.state/'evaluation-queue'/('f'*32);ar.atomic(extra/'result.json',dict(job_id='f'*32,evaluation_id=self.eid,snapshot_sha256=digest,status='cancelled',reward=None))
        ar.register(self.root,self.universe,'universe-one')

    def tearDown(self):self.temp.cleanup()

    def make_job(self,n,reward):
        jid=str(n).zfill(32);work=self.state/'evaluation-queue'/jid;run='run-'+jid;trial=work/'runs'/run/'trial';trial.mkdir(parents=True,exist_ok=True)
        ar.atomic(work/'result.json',dict(job_id=jid,evaluation_id=self.eid,snapshot_sha256=self.row['snapshot_sha256'],status='solver_complete',reward=reward,n_errors=0,cleanup_confirmed=True,provider='dawnfm',model='test-model',missing_evidence=['historical_actual_pod_observations']))
        ar.atomic(work/'report.json',dict(run_id=run,cleanup=dict(confirmed=True)))
        ar.atomic(work/'config.json',dict(n_attempts=1,n_concurrent_trials=1,agents=[dict(env=dict(OPENAI_API_KEY=self.secret))]))
        ar.atomic(trial/'config.json',dict(task=dict(source=self.eid),environment=dict(override_cpus=4,override_memory_mb=8192,kwargs={})))
        ar.atomic(trial/'result.json',dict(id='id-'+str(n),trial_name='trial',exception_info=None,verifier_result=dict(rewards=dict(reward=reward))))
        ar.atomic(trial/'agent/trajectory.json',dict(steps=[dict(content='reasoning '+self.secret)],session_id=str(n)))
        ar.atomic(trial/'agent/sessions/session.jsonl',(json.dumps(dict(role='assistant',text='solved '+self.secret))+'\n').encode())
        ar.atomic(trial/'verifier/reward.txt',str(reward).encode())

    def bundle(self):return self.root/ar.load(self.root/'index.json')['entries'][0]['path']

    def test_import_self_contained_redacted_and_idempotent(self):
        result=ar.sync(self.root);self.assertFalse(result['pending']);self.assertEqual(result['accepted_archives'],1)
        bundle=self.bundle();meta=ar.verify_bundle(bundle);self.assertEqual(meta['statistics']['pass_rate'],0.6)
        self.assertEqual(meta['statistics']['excluded_attempts'],1);self.assertFalse(meta['evidence']['complete'])
        for p in ar.files(bundle):self.assertNotIn(self.secret.encode(),p.read_bytes())
        self.assertTrue(any(x['credential_redacted'] for x in ar.load(bundle/'manifest.json')['files'].values()))
        self.assertTrue(ar.sync(self.root)['processed'][0]['already_present'])
        self.universe.rename(self.base/'source-offline')
        self.assertEqual(ar.verify_bundle(bundle)['statistics']['passed'],3)

    def test_unaccepted_is_never_published(self):
        row=dict(self.row,scientific_acceptance=False,formal_decision='pending_evidence')
        ar.atomic(self.state/'review-dispatcher/ledger.json',{row['key']:row})
        self.assertEqual(ar.sync(self.root)['accepted_archives'],0)

    def test_wrong_acceptance_binding_is_blocked(self):
        receipt=ar.load(self.decision);receipt['commit']='c'*40;ar.atomic(self.decision,receipt)
        result=ar.sync(self.root);self.assertEqual(result['accepted_archives'],0);self.assertTrue(result['pending'])

    def test_six_scored_runs_cannot_be_cherry_picked(self):
        self.make_job(6,0);result=ar.sync(self.root)
        self.assertEqual(result['accepted_archives'],0);self.assertIn('exactly five',result['pending'][0]['error'])

    def test_missing_native_session_blocks_publication(self):
        session=next(self.state.glob('evaluation-queue/*/runs/*/*/agent/sessions/*.jsonl'));session.unlink()
        result=ar.sync(self.root);self.assertEqual(result['accepted_archives'],0)
        self.assertTrue(result['pending']);self.assertFalse(list((self.root/'tasks').glob('*')) if (self.root/'tasks').exists() else [])

    def test_verifier_disagreement_blocks_publication(self):
        p=next(self.state.glob('evaluation-queue/*/runs/*/*/verifier/reward.txt'));p.write_text('0' if p.read_text()=='1' else '1')
        self.assertEqual(ar.sync(self.root)['accepted_archives'],0)

    def test_tampered_archive_detected(self):
        ar.sync(self.root);bundle=self.bundle();(bundle/'task/instruction.md').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'checksum'):ar.verify_bundle(bundle)

    def test_task_symlink_refused(self):
        registry=ar.load(self.universe/'.config/science-evaluation/registry.json');snap=Path(registry[self.eid]['snapshot']);(snap/'linked').symlink_to(snap/'instruction.md')
        self.assertTrue(ar.sync(self.root)['pending'])

    def test_independent_universes_do_not_overwrite(self):
        import shutil
        other=self.base/'universe-two';shutil.copytree(self.universe,other)
        registry=ar.load(other/'.config/science-evaluation/registry.json');registry[self.eid]['snapshot']=str(other/Path(registry[self.eid]['snapshot']).relative_to(self.universe));ar.atomic(other/'.config/science-evaluation/registry.json',registry)
        ar.register(self.root,other,'universe-two');result=ar.sync(self.root)
        self.assertFalse(result['pending']);self.assertEqual(result['accepted_archives'],2)
        entries=ar.load(self.root/'index.json')['entries'];self.assertNotEqual(entries[0]['path'],entries[1]['path'])


class LauncherArchiveHookTests(unittest.TestCase):
    def test_fresh_provisioned_universe_registers_before_first_ledger(self):
        with tempfile.TemporaryDirectory(prefix='archive-fresh-',dir=ar.WORKSPACE/'.tmp') as temp:
            base=Path(temp);universe=base/'fresh';root=base/'accepted'
            ar.atomic(universe/'.state/gewu-multiverse/deployment.json',dict(status='service_config_prepared',universe_root=str(universe)))
            identifier=ar.register(root,universe)
            self.assertTrue(identifier.startswith('fresh-'))
            result=ar.sync(root)
            self.assertEqual(result['accepted_archives'],0)
            self.assertEqual(len(result['pending']),1)

    def test_launcher_registers_then_starts_global_watcher(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory(prefix='archive-launcher-',dir=ar.WORKSPACE/'.tmp') as temp:
            base=Path(temp);state=base/'state';state.mkdir();fake=base/'fake-python'
            fake.write_text('#!'+sys.executable+'\nimport os,json,sys\nfrom pathlib import Path\np=Path(os.environ["CALLS"])\nwith p.open("a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n')
            fake.chmod(0o700)
            source=(Path(__file__).parent/'start-universe').read_text()
            block=source.split('# Accepted outputs outlive individual universes.',1)[1].split('# Refuse a controller start',1)[0]
            block='# Accepted outputs outlive individual universes.'+block
            env=dict(os.environ,W=str(base),U=str(base/'universe'),PY=str(fake),SERVICE_STATE=str(state),CALLS=str(base/'calls.jsonl'),PYTHONDONTWRITEBYTECODE='1')
            result=subprocess.run(['bash','-c',block],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            calls=[json.loads(line) for line in (base/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[0][2:],['--root',str(base/'accepted-tasks'),'register','--universe',str(base/'universe')])
            self.assertEqual(calls[1][2:],['--root',str(base/'accepted-tasks'),'ensure-watch'])


if __name__=='__main__':unittest.main()
