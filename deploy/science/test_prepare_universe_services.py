import json,os,tempfile,unittest
from pathlib import Path
from prepare_universe_services import apply_plan,plan
from deployment_paths import WORKSPACE,broker_socket_name,require_isolated_service_config
from evaluation_queue import parallel_limit


class PrepareUniverseServicesTests(unittest.TestCase):
    def fixture(self):
        raw=Path(tempfile.mkdtemp(dir=WORKSPACE/'.tmp'))
        self.addCleanup(lambda:__import__('shutil').rmtree(raw,ignore_errors=True))
        root=raw/'universes/world';role=root/'.config/gewu-multiverse/agents/evaluator-01'
        inbox=role/'evaluation/inbox';results=role/'evaluation/results'
        inbox.mkdir(parents=True);results.mkdir(parents=True)
        os.chown(inbox,22001,22001);inbox.chmod(0o700)
        registry=raw/'source-registry.json';template=raw/'template.json';snapshot=raw/'snapshot'
        snapshot.mkdir();(snapshot/'task.toml').write_text('version = "1.0"\n')
        template.write_text('{}');template.chmod(0o600)
        registry.write_text(json.dumps({'calibration':{'template':str(template),'snapshot':str(snapshot)}}));registry.chmod(0o600)
        broker=raw/'source-broker.json'
        author=root/'.config/gewu-multiverse/agents/author-01';author.mkdir(parents=True)
        broker.write_text(json.dumps({'controller_uid':65534,'socket':'@/old/socket','roles':{
            'evaluator-01':{'root':str(role),'uid':22001,'gid':22001},
            'author-01':{'root':str(author),'uid':22002,'gid':22002}}}));broker.chmod(0o600)
        queue=raw/'source-queue.json'
        queue.write_text(json.dumps({'registry':str(registry),'state':str(raw/'old-state'),'max_jobs_per_day':0,
            'role_queues':[{'role':'evaluator-01','evaluator_uid':22001,'inbox':str(inbox),'results':str(results)}]}));queue.chmod(0o600)
        return raw,root,broker,queue

    def test_dry_run_does_not_create_or_modify_target(self):
        raw,root,broker,queue=self.fixture()
        value=plan(root,broker,queue)
        self.assertEqual(value['root'],root)
        self.assertFalse((root/'.state/gewu-multiverse').exists())
        self.assertFalse((root/'.config/science-evaluation').exists())

    def test_apply_writes_ready_marker_last_and_passes_start_gate(self):
        raw,root,broker,queue=self.fixture()
        result=apply_plan(plan(root,broker,queue))
        self.assertEqual(result['status'],'service_config_prepared')
        self.assertEqual(require_isolated_service_config(root),root/'.state/gewu-multiverse')
        config=json.loads((root/'.state/gewu-multiverse/role-broker/config.json').read_text())
        self.assertEqual(config['socket'],broker_socket_name(root))
        queue_config=json.loads((root/'.config/science-evaluation/queue.json').read_text())
        self.assertEqual(queue_config['broker_config'],str(root/'.state/gewu-multiverse/role-broker/config.json'))
        self.assertEqual(parallel_limit(queue_config),5)
        self.assertEqual(json.loads((root/'.config/science-evaluation/registry.json').read_text())['calibration']['snapshot'],str(raw/'snapshot'))

    def test_existing_service_files_are_never_overwritten(self):
        raw,root,broker,queue=self.fixture()
        apply_plan(plan(root,broker,queue))
        ready=root/'.state/gewu-multiverse/deployment.json'
        before=ready.read_bytes()
        with self.assertRaisesRegex(ValueError,'already exists'):
            plan(root,broker,queue)
        self.assertEqual(ready.read_bytes(),before)

    def test_role_roots_outside_target_are_refused(self):
        raw,root,broker,queue=self.fixture()
        value=json.loads(broker.read_text());value['roles']['evaluator-01']['root']=str(raw/'elsewhere')
        broker.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'role roots'):
            plan(root,broker,queue)


if __name__=='__main__':unittest.main()
