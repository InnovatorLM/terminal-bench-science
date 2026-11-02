import json,os,subprocess,tempfile,unittest
from pathlib import Path
from role_capabilities import W,inspect,MODULES,TOOLS,enabled_roles
from install_role_skills import selected,install

class CapabilityTests(unittest.TestCase):
    def test_stopped_configured_roles_still_require_registered_boundaries(self):
        settings={'agents':[dict(name='author-01',desired='stopped'),dict(name='evaluator-02',desired='started')]}
        config={'roles':{'evaluator-02':{}}}
        with self.assertRaisesRegex(ValueError,'including stopped'):
            enabled_roles(config,settings)
        config['roles']['author-01']={}
        self.assertEqual(enabled_roles(config,settings),{'evaluator-02'})

    def fixture(self,root):
        (root/'instruction.md').write_text('policy')
        install(root,'author-01',owner=None)
        for name in ['home/role/.local/env/bin/python','usr/local/bin/role-acs-selftest']:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
        (root/'usr/local/bin/role-acs-selftest').write_bytes(Path(__file__).with_name('role_acs_selftest.py').read_bytes())
        return dict(root=str(root),uid=21001,gid=21001)

    def test_checks_identity_and_reports_missing_dependencies(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            entry=self.fixture(Path(raw))
            payload=dict(uid=21001,modules={k:{'ok':True} for k in MODULES},tools={k:'/usr/bin/'+k for k in TOOLS},commands={'role-acs-selftest':{'executable':'/usr/local/bin/role-acs-selftest','help_ok':True}})
            def run(args,**kwargs):
                self.assertIn('--userspec=21001:21001',args)
                return subprocess.CompletedProcess(args,0,json.dumps(payload),'')
            receipt=inspect('author-01',entry,run);self.assertTrue(receipt['ready'])
            payload['commands']['role-acs-selftest']['help_ok']=False
            self.assertFalse(inspect('author-01',entry,run)['ready'])
            payload['commands']['role-acs-selftest']['help_ok']=True
            reference=Path(raw)/'skills/acs-task-pipeline/references/receipt.md'
            before=reference.read_bytes();reference.write_text('stale reference')
            stale=inspect('author-01',entry,run);self.assertFalse(stale['ready'])
            self.assertFalse(stale['skills_current']['acs-task-pipeline'])
            reference.write_bytes(before)
            payload['modules']['numpy']['ok']=False
            self.assertFalse(inspect('author-01',entry,run)['ready'])
            payload['modules'].pop('numpy')
            with self.assertRaisesRegex(ValueError,'Incomplete'):inspect('author-01',entry,run)
            payload['uid']=0
            with self.assertRaises(ValueError):inspect('author-01',entry,run)

    def test_no_raw_probe_error_is_exposed(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            entry=self.fixture(Path(raw))
            with self.assertRaisesRegex(ValueError,'no raw output'):
                inspect('author-01',entry,lambda *a,**k:subprocess.CompletedProcess([],1,'secret','secret'))

    def test_evaluator_requires_current_executable_batch_client(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);entry=self.fixture(root)
            install(root,'evaluator-01',owner=None)
            payload=dict(uid=21001,modules={k:{'ok':True} for k in MODULES},tools={k:'/usr/bin/'+k for k in TOOLS},commands={'role-acs-selftest':{'executable':'/usr/local/bin/role-acs-selftest','help_ok':True}})
            payload['commands']={'role-evaluation-batch':{'executable':'/usr/local/bin/role-evaluation-batch','help_ok':True}}
            runner=lambda *a,**k:subprocess.CompletedProcess([],0,json.dumps(payload),'')
            self.assertFalse(inspect('evaluator-01',entry,runner)['ready'])
            client=root/'usr/local/bin/role-evaluation-batch'
            client.write_bytes(Path(__file__).with_name('role_evaluation_batch.py').read_bytes())
            client.chmod(0o644)
            self.assertFalse(inspect('evaluator-01',entry,runner)['ready'])
            client.chmod(0o755)
            self.assertTrue(inspect('evaluator-01',entry,runner)['ready'])
            client.write_text('stale client')
            self.assertFalse(inspect('evaluator-01',entry,runner)['ready'])

    def test_stale_client_blocks_startup_even_when_entrypoint_exists(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);entry=self.fixture(root)
            (root/'usr/local/bin/role-acs-selftest').write_text('old client')
            payload=dict(uid=21001,modules={k:{'ok':True} for k in MODULES},tools={k:'/usr/bin/'+k for k in TOOLS},commands={'role-acs-selftest':{'executable':'/usr/local/bin/role-acs-selftest','help_ok':True}})
            result=inspect('author-01',entry,lambda *a,**k:subprocess.CompletedProcess([],0,json.dumps(payload),''))
            self.assertFalse(result['ready'])
            self.assertFalse(result['entrypoints']['selftest_client_current'])

if __name__=='__main__':unittest.main()


class ProbeLockTests(unittest.TestCase):
    def test_probe_uses_broker_lock_and_defers_without_running_when_busy(self):
        import fcntl
        from role_capabilities import probe_lock
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);cfg={'_lock_dir':str(root),'socket':'@fixture'}
            with (root/'author-01.lock').open('a') as broker:
                fcntl.flock(broker,fcntl.LOCK_EX)
                with self.assertRaises(TimeoutError):
                    with probe_lock(cfg,'author-01',wait_seconds=0):self.fail('Probe entered a busy role')
            with probe_lock(cfg,'author-01'):
                with (root/'author-01.lock').open('a') as contender:
                    with self.assertRaises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with (root/'author-01.lock').open('a') as contender:
                fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
