"""Exercise the actual launcher readiness block without services or model calls."""
import errno
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
import subprocess
import tempfile
from unittest.mock import patch


SOURCE = (Path(__file__).parent / 'start-universe').read_text().split("<<'PY_QUEUE_CHECK'\n", 1)[1].split('\nPY_QUEUE_CHECK', 1)[0]
LAUNCHER = (Path(__file__).parent / 'start-universe').read_text()


class ReadyFileTests(unittest.TestCase):
    def backup_script(self):
        launcher=Path(__file__).with_name('start-universe-local-runtime').read_text()
        block=launcher.split('backup() {',1)[1].split('\n}\nbackup_pid=',1)[0]
        return 'umask 022\nRUNTIME="$1"\nBACKUP_DIR="$2"\nW="$3"\nPY="$4"\nbackup() {'+block+'\n}\nbackup\n'

    def test_incremental_backup_is_private_and_excludes_credentials(self):
        import json
        from evaluation_queue import W
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);runtime=root/'runtime';runtime.mkdir()
            (runtime/'work.txt').write_text('recoverable work')
            (runtime/'credentials-fixture').write_text('fixture excluded from backup')
            directory=root/'backups';directory.mkdir(mode=0o755)
            subprocess.run(['bash','-ec',self.backup_script(),'test',str(runtime),str(directory),str(W),sys.executable],check=True)
            recovery=directory/'recovery'
            manifest=json.loads((recovery/'manifest.json').read_text())
            self.assertIn('work.txt',manifest['files'])
            self.assertNotIn('credentials-fixture',manifest['files'])
            self.assertEqual(stat.S_IMODE((recovery/'data/work.txt').stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(recovery.stat().st_mode),0o700)
            receipt=json.loads((runtime/'.state/gewu-multiverse/runtime-backup.json').read_text())
            self.assertEqual(receipt['status'],'ok')
            self.assertIsNotNone(receipt['last_success_at'])
            self.assertEqual(list(directory.glob('*.tar.gz')),[])

    def test_backup_lock_prevents_overlapping_inventory_and_copy(self):
        import fcntl
        from evaluation_queue import W
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);runtime=root/'runtime';runtime.mkdir()
            (runtime/'work.txt').write_text('recoverable work')
            directory=root/'backups';recovery=directory/'recovery';recovery.mkdir(parents=True)
            with (recovery/'backup.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                subprocess.run(['bash','-ec',self.backup_script(),'test',str(runtime),str(directory),str(W),sys.executable],check=True)
                self.assertFalse((recovery/'manifest.json').exists())
                self.assertFalse((recovery/'data/work.txt').exists())
            subprocess.run(['bash','-ec',self.backup_script(),'test',str(runtime),str(directory),str(W),sys.executable],check=True)
            self.assertEqual((recovery/'data/work.txt').read_text(),'recoverable work')

    def test_local_workspace_is_not_renamed_or_replaced_by_self_link(self):
        import json
        from evaluation_queue import W
        launcher=Path(__file__).with_name('start-universe-local-runtime').read_text()
        block=launcher.split('# Keep the controller\'s local layout connected',1)[1].split("<<'PY'\n",1)[1].split('\nPY',1)[0]
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);agents=root/'agents';work=agents/'author-01/workspace'
            work.mkdir(parents=True);(work/'current-task.txt').write_text('unsaved work')
            config=root/'broker.json';config.write_text(json.dumps({'roles':{'author-01':{'root':str(work.parent)}}}))
            for _ in range(2):
                subprocess.run([sys.executable,'-B','-c',block,str(agents),str(config)],check=True)
                self.assertFalse(work.is_symlink())
                self.assertEqual((work/'current-task.txt').read_text(),'unsaved work')
            self.assertEqual(list(work.parent.glob('workspace.preserved-*')),[])

    def test_start_refreshes_both_skill_views_from_tracked_source(self):
        import json
        from evaluation_queue import W
        from install_role_skills import SOURCE as skills_source, selected
        block=LAUNCHER.split("<<'PY_TASK_POLICY'\n",1)[1].split('\nPY_TASK_POLICY',1)[0]
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);roles={}
            for role in ['author-01','evaluator-01','curator']:
                isolated=root/'isolated'/role
                controller=root/'universe/.config/gewu-multiverse/agents'/role
                for view in [isolated,controller]:
                    view.mkdir(parents=True)
                    (view/'instruction.md').write_text('Detailed existing '+role)
                    for name in selected(role):
                        skill=view/'skills'/name;skill.mkdir(parents=True)
                        (skill/'SKILL.md').write_text('STALE DONOR SKILL')
                roles[role]={'root':str(isolated)}
            config=root/'config.json';config.write_text(json.dumps({'roles':roles}))
            subprocess.run([sys.executable,'-B','-c',block,str(config),str(root/'universe')],check=True,cwd=Path(__file__).parent)
            for role,entry in roles.items():
                for view in [Path(entry['root']),root/'universe/.config/gewu-multiverse/agents'/role]:
                    self.assertIn('Detailed existing '+role,(view/'instruction.md').read_text())
                    for name in selected(role):
                        self.assertEqual((view/'skills'/name/'SKILL.md').read_bytes(),(skills_source/name/'SKILL.md').read_bytes())

    def test_restart_keeps_controller_plans_readable_and_operator_evidence_private(self):
        import os
        if os.geteuid() != 0:
            self.skipTest('Actual ownership regression requires root')
        from evaluation_queue import W
        launcher=Path(__file__).with_name('start-universe-local-runtime').read_text()
        block=launcher.split("# The runtime's mutable top-level state is owned by the controller UID.",1)[1].split('\n',1)[1].split('# Keep directories searchable',1)[0]
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);root.chmod(0o755)
            for folder in ('.config/gewu-multiverse','.config/science-evaluation','workspaces','.local','.cache','.tmp'):
                (root/folder).mkdir(parents=True,exist_ok=True)
            for name in ('queue.json','registry.json'):
                (root/'.config/science-evaluation'/name).write_text('{}')
            state=root/'.state/gewu-multiverse'
            for service in ('role-broker','review-dispatcher','author-selftest','command-watchdog'):
                d=state/service;d.mkdir(parents=True)
                (d/'config.json').write_text('{}')
            private=state/'author-selftest/receipt.json';private.write_text('{}');private.chmod(0o600)
            plan=state/'author-01/run-example/plan.json';plan.parent.mkdir(parents=True)
            plan.write_text('{}');plan.chmod(0o600)
            for directory in [p for p in root.rglob('*') if p.is_dir()]:directory.chmod(0o755)
            subprocess.run(['bash','-ec','RUNTIME="$1"\n'+block,'ownership',str(root)],check=True)
            self.assertEqual(plan.stat().st_uid,65534)
            self.assertEqual(private.stat().st_uid,0)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode),0o600)
            from controller_readability import check
            subprocess.run(['setpriv','--reuid=65534','--regid=65534','--clear-groups',sys.executable,'-B',str(Path(__file__).with_name('controller_readability.py')),str(state)],check=True)

    def test_fresh_controller_uses_cached_binary_without_overriding_operator(self):
        from evaluation_queue import W
        block=LAUNCHER.split('# Reuse the preinstalled workspace binary',1)[1].split('\nif [[ $(id -u)',1)[0]
        block=block[block.index('\nif [['):]
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);cached=root/'.local/share/gewu-multiverse/forgejo/forgejo-17.0.0-linux-amd64'
            cached.parent.mkdir(parents=True);cached.write_bytes(b'fixture: product verifies digest')
            script='W="$1"\n'+block+'\nprintf "%s" "${GEWU_FORGEJO_BINARY:-}"'
            import os
            env=dict(os.environ);env.pop('GEWU_FORGEJO_BINARY',None)
            result=subprocess.check_output(['bash','-c',script,'check',str(root)],env=env,text=True)
            self.assertEqual(result,str(cached))
            env['GEWU_FORGEJO_BINARY']=str(root/'explicit')
            self.assertEqual(subprocess.check_output(['bash','-c',script,'check',str(root)],env=env,text=True),env['GEWU_FORGEJO_BINARY'])

    def test_runtime_keeps_full_instruction_and_skill_manifest_for_every_role(self):
        from evaluation_queue import W
        from install_role_skills import SOURCE as skill_source
        launcher=Path(__file__).with_name('start-universe-local-runtime').read_text()
        seed=launcher.split('# Seed all configured role instructions,',1)[1].split("<<'PY'\n",1)[1].split('\nPY',1)[0]
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);runtime=root/'runtime';donor=root/'donor'
            broker=runtime/'.state/gewu-multiverse/role-broker/config.json'
            broker.parent.mkdir(parents=True)
            roles=['author-01','author-10','evaluator-03','curator']
            import json
            broker.write_text(json.dumps({'roles':dict.fromkeys(roles,{})}))
            for role in roles:
                source=donor/'.config/gewu-multiverse/agents'/role/'instruction.md'
                source.parent.mkdir(parents=True)
                source.write_text('Full rules '+role+'\n/skills/harbor-acs-evaluation/SKILL.md\n')
                from install_role_skills import selected
                for name in selected(role):
                    skill=source.parent/'skills'/name
                    skill.mkdir(parents=True)
                    (skill/'SKILL.md').write_text('Complete '+name+'\n')
            subprocess.run([sys.executable,'-c',seed,str(root/'source'),str(runtime),str(donor)],check=True,cwd=Path(__file__).parent)
            for role in roles:
                path=Path('.config/gewu-multiverse/agents')/role/'instruction.md'
                self.assertEqual((runtime/path).read_bytes(),(donor/path).read_bytes())
                for name in selected(role):
                    path=path.parent/'skills'/name/'SKILL.md'
                    self.assertEqual((runtime/path).read_bytes(),(skill_source/name/'SKILL.md').read_bytes())
                    path=Path('.config/gewu-multiverse/agents')/role/'instruction.md'
            latest=runtime/'.config/gewu-multiverse/agents/author-01/instruction.md'
            latest.write_text('Current reviewed instructions')
            subprocess.run([sys.executable,'-c',seed,str(root/'source'),str(runtime),str(donor)],check=True,cwd=Path(__file__).parent)
            self.assertEqual(latest.read_text(),'Current reviewed instructions')
        self.assertNotIn('default-instructions/',launcher)

    def test_runtime_restart_preserves_controller_role_choices(self):
        from evaluation_queue import W
        launcher=Path(__file__).with_name('start-universe-local-runtime').read_text()
        loop=launcher.split('for f in ',1)[1].split('\ndone',1)[0]
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw)
            for name in ('source','donor','runtime'):
                for directory in ('.config/gewu-multiverse','.config/science-evaluation'):
                    (root/name/directory).mkdir(parents=True)
            source=root/'source/.config/gewu-multiverse/multiverse.toml'
            target=root/'runtime/.config/gewu-multiverse/multiverse.toml'
            source.write_text('desired = "stopped"\n')
            credentials=root/'runtime/.config/gewu-multiverse/credentials.toml'
            credentials.write_text('latest database identity fixture')
            (root/'donor/.config/gewu-multiverse/credentials.toml').write_text('stale donor fixture')
            script='set -eu\nSOURCE="$1/source"\nDONOR="$1/donor"\nRUNTIME="$1/runtime"\ncopy_control() { cp "$1" "$2"; }\nfor f in '+loop+'\ndone\n'
            subprocess.run(['bash','-c',script,'check',str(root)],check=True)
            self.assertEqual(target.read_text(),source.read_text())
            target.write_text('desired = "started"\n')
            subprocess.run(['bash','-c',script,'check',str(root)],check=True)
            self.assertEqual(target.read_text(),'desired = "started"\n')
            self.assertEqual(credentials.read_text(),'latest database identity fixture')

    def test_watchdog_is_owned_and_stopped_with_launcher(self):
        self.assertIn('command_watchdog.py',LAUNCHER)
        self.assertIn('watchdog_pid=$!',LAUNCHER)
        self.assertIn('kill -TERM "$watchdog_pid"',LAUNCHER)
        self.assertIn('wait "$watchdog_pid"',LAUNCHER)
        self.assertLess(LAUNCHER.index('watchdog_pid=$!'),LAUNCHER.index('export USER=nobody'))

    def run_check(self, reads, times):
        with patch('pathlib.Path') as constructor, patch('os.kill'), patch('time.sleep'), patch('time.monotonic', side_effect=times), patch.object(sys, 'argv', ['check', '42', '/ready']):
            path = constructor.return_value
            path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)
            path.read_text.side_effect = reads
            exec(compile(SOURCE, 'launcher-ready-check', 'exec'), {})
            return path.read_text.call_count

    def test_stale_handle_is_reopened_then_current_worker_required(self):
        self.assertEqual(self.run_check([OSError(errno.ESTALE, 'stale'), '{"pid":41}', '{"pid":42}'], [0, 1, 2]), 3)

    def test_repeated_stale_handles_still_hit_deadline(self):
        with self.assertRaisesRegex(SystemExit, 'worker failed startup'):
            self.run_check([OSError(errno.ESTALE, 'stale')], [0, 16])

    def test_permission_error_is_not_retried(self):
        with self.assertRaises(PermissionError):
            self.run_check([PermissionError(errno.EACCES, 'denied')], [0])


if __name__ == '__main__':
    unittest.main()
