import json
from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import runtime_backup as b

W=Path('/mnt/data/zhanglinfeng/workspace')

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=W/'.tmp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.src=self.root/'source';self.src.mkdir();self.dst=self.root/'backup'
    def put(self,name,data='fixture'):
        p=self.src/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data);return p
    def test_launcher_targets_resume_live_and_isolate_other_universes(self):
        live=self.root/'.tmp/gewu-terminal-bench-science-runtime-live'
        self.assertEqual(b.backup_target(live,workspace=self.root),self.root/'.state/backups/live/recovery')
        one=b.backup_target(self.root/'one/runtime',workspace=self.root)
        two=b.backup_target(self.root/'two/runtime',workspace=self.root)
        self.assertNotEqual(one,two)
        self.assertTrue(one.is_relative_to(self.root/'.state/backups'))
        self.assertFalse(one.exists())

    def test_launcher_target_rejects_recursive_outside_or_foreign_backup(self):
        for bad in (self.src,self.src/'nested',self.root,self.root.parent/'outside','relative'):
            with self.subTest(target=bad),self.assertRaises(ValueError):
                b.backup_target(self.src,bad,workspace=self.root)
        self.put('work.txt');b.cycle(self.src,self.dst)
        self.assertEqual(b.backup_target(self.src,self.dst,workspace=self.root),self.dst)
        with self.assertRaisesRegex(ValueError,'another source'):
            b.backup_target(self.root/'another',self.dst,workspace=self.root)
        alias=self.root/'alias';alias.symlink_to(self.dst,target_is_directory=True)
        with self.assertRaises(ValueError):b.backup_target(self.src,alias,workspace=self.root)
        with self.assertRaises(ValueError):b.backup_target('/tmp/historical-runtime',workspace=self.root)
    def test_recovery_payload_excludes_secrets_rootfs_caches_and_links(self):
        role='.config/gewu-multiverse/agents/author-01/'
        excluded=['credentials.toml','git-credential.json','dawnfm.env','service.token','app.ini','kubeconfig','private.key','custom-secret-dir/key','secrets/key','.cache/data','.tmp/cache','worker.pid','state.lock',role+'usr/lib/lib.so',role+'tmp/pip/file',role+'workspace.preserved-1/old',role+'workspace/repo/.git/config',role+'workspace/repo/.git/objects/tmp_123']
        retained=['trajectory.json','review.json','session.stdout.log','.state/gewu-multiverse/author-selftest/job/source/task/requirements.lock',role+'runs/run-1/trajectory.json',role+'workspace/repo/.git/HEAD',role+'workspace/repo/.git/objects/ab/cdef',role+'workspace/repo/task/solution/solve.sh',role+'workspace/repo/task/requirements.lock',role+'workspace/repo/task/Cargo.lock']
        excluded.append(role+'workspace/repo/tests/.pytest_cache/v/cache/nodeids')
        for name in excluded+retained:self.put(name)
        external=self.root/'external';external.write_text('outside');(self.src/'external-link').symlink_to(external)
        row=b.cycle(self.src,self.dst)
        self.assertEqual(row['status'],'ok')
        files={p.relative_to(self.dst/'data').as_posix() for p in (self.dst/'data').rglob('*') if p.is_file()}
        self.assertEqual(files,set(retained));self.assertEqual(self.dst.stat().st_mode&0o777,0o700)
        manifest=json.loads((self.dst/'manifest.json').read_text())
        restored=b.restore(self.dst,self.root/'restored');self.assertEqual(set(restored),set(retained))
        self.assertEqual((self.root/'restored/trajectory.json').read_text(),'fixture')
        self.assertTrue(manifest['last_success_at'])
    def test_partial_cycle_resumes_without_recopying_verified_prefix(self):
        for name in ['a','b','c']:self.put(name)
        original=b.copy_file;calls=[]
        def bounded(*args):
            calls.append(args[0].name)
            if len(calls)==2:raise b.BudgetExpired()
            return original(*args)
        with patch.object(b,'copy_file',side_effect=bounded):row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['status'],'partial');self.assertEqual(row['remaining_files'],2)
        self.assertIsNone(row['last_success_at'])
        with patch.object(b,'copy_file',wraps=original) as copier:row=b.cycle(self.src,self.dst)
        self.assertEqual(row['status'],'ok');self.assertEqual(copier.call_count,2)
        self.assertEqual(set(b.restore(self.dst,self.root/'restore')),{'a','b','c'})
        with patch.object(b,'copy_file',wraps=original) as copier:row=b.cycle(self.src,self.dst)
        self.assertEqual(row['unchanged'],3);copier.assert_not_called()
    def test_disappearing_ordinary_source_is_visible_not_a_false_success(self):
        self.put('trajectory.json')
        with patch.object(b,'copy_file',side_effect=FileNotFoundError()):row=b.cycle(self.src,self.dst)
        self.assertEqual(row['status'],'partial');self.assertEqual(row['warnings'][0]['kind'],'FileNotFoundError')
        self.assertIsNone(row['last_success_at'])
    def test_changed_source_and_corrupt_backup_are_repaired(self):
        source=self.put('task.py','old');b.cycle(self.src,self.dst)
        source.write_text('new');row=b.cycle(self.src,self.dst);self.assertEqual(row['copied'],1)
        (self.dst/'data/task.py').write_text('bad')
        with self.assertRaisesRegex(ValueError,'verification'):b.restore(self.dst,self.root/'bad-restore')
        row=b.cycle(self.src,self.dst);self.assertEqual(row['copied'],1)
        self.assertEqual((self.dst/'data/task.py').read_text(),'new')
    def test_sqlite_committed_wal_is_included_without_copying_live_wal(self):
        database=self.src/'forgejo.db';connection=sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute('pragma journal_mode=wal');connection.execute('create table data(value)');connection.execute('insert into data values (42)');connection.commit()
        row=b.cycle(self.src,self.dst);self.assertEqual(row['status'],'ok')
        with closing(sqlite3.connect(self.dst/'data/forgejo.db')) as copy:self.assertEqual(copy.execute('select value from data').fetchone()[0],42)
        self.assertFalse((self.dst/'data/forgejo.db-wal').exists())
        connection.execute('insert into data values (43)');connection.commit()
        row=b.cycle(self.src,self.dst);self.assertEqual(row['copied'],1)
        with closing(sqlite3.connect(self.dst/'data/forgejo.db')) as copy:self.assertEqual(copy.execute('select count(*) from data').fetchone()[0],2)
    def test_live_transcript_append_keeps_verified_prefix_and_updates_next_cycle(self):
        path=self.put('transcript.jsonl','first\n');original=b.inspect_source;calls=[]
        def inspect(file):
            if file==path:
                calls.append(file)
                if len(calls)==4:
                    with path.open('a') as stream:stream.write('second\n')
            return original(file)
        with patch.object(b,'inspect_source',side_effect=inspect):row=b.cycle(self.src,self.dst)
        self.assertEqual(row['status'],'ok')
        self.assertEqual((self.dst/'data/transcript.jsonl').read_text(),'first\n')
        row=b.cycle(self.src,self.dst)
        self.assertEqual(row['status'],'ok')
        self.assertEqual((self.dst/'data/transcript.jsonl').read_text(),'first\nsecond\n')
    def test_scoped_backup_records_coverage_and_rejects_escaping_scope(self):
        self.put('one/data');self.put('two/data')
        row=b.cycle(self.src,self.dst,includes=['one']);self.assertEqual(row['coverage'],'selected_scope')
        self.assertFalse((self.dst/'data/two').exists())
        with self.assertRaises(ValueError):b.cycle(self.src,self.dst,includes=['../outside'])
        with self.assertRaises(ValueError):b.cycle(self.src,self.dst,includes=['two'])
    def test_parallel_budget_stop_joins_workers_and_resumes_every_file(self):
        for i in range(12):self.put('file-%02d'%i)
        original=b.copy_file;state={'active':0,'peak':0,'calls':0};lock=threading.Lock()
        def bounded(*args):
            with lock:
                state['active']+=1;state['calls']+=1
                state['peak']=max(state['peak'],state['active']);first=state['calls']==1
            try:
                if first:raise b.BudgetExpired()
                return original(*args)
            finally:
                with lock:state['active']-=1
        with patch.object(b,'copy_file',side_effect=bounded):row=b.cycle(self.src,self.dst,workers=4)
        self.assertEqual(row['status'],'partial');self.assertEqual(state['active'],0)
        self.assertLessEqual(state['peak'],4)
        row=b.cycle(self.src,self.dst,workers=4)
        self.assertEqual(row['status'],'ok');self.assertEqual(row['verified_files'],12)
        self.assertEqual(len(b.restore(self.dst,self.root/'restored')),12)
    def test_internal_relative_links_survive_backup_restore(self):
        self.put('repo/AGENTS.md','instructions')
        (self.src/'repo/CLAUDE.md').symlink_to('AGENTS.md')
        row=b.cycle(self.src,self.dst)
        self.assertEqual(row['status'],'ok');self.assertEqual(row['verified_files'],2)
        row=b.cycle(self.src,self.dst);self.assertEqual(row['unchanged'],2)
        b.restore(self.dst,self.root/'restored')
        self.assertTrue((self.root/'restored/repo/CLAUDE.md').is_symlink())
        self.assertEqual((self.root/'restored/repo/CLAUDE.md').read_text(),'instructions')
    def test_recovery_import_preserves_owner_executable_and_directory_access(self):
        script=self.put('nested/work/solve.sh','#!/bin/sh\ntrue\n');script.chmod(0o755)
        (self.src/'nested').chmod(0o750)
        row=b.cycle(self.src,self.dst,recovery=True)
        self.assertEqual(row['status'],'ok');self.assertEqual(row['purpose'],'recovery_import')
        self.assertEqual((self.dst/'data/nested/work/solve.sh').stat().st_mode&0o777,0o755)
        self.assertEqual((self.dst/'data/nested').stat().st_mode&0o777,0o750)
        self.assertEqual((self.dst/'data/nested/work/solve.sh').stat().st_uid,script.stat().st_uid)
        self.assertEqual(self.dst.stat().st_mode&0o777,0o700)
    def test_role_codex_trajectories_are_retained_without_provider_credentials(self):
        home='.config/gewu-multiverse/agents/author-01/home/role/'
        retained=['trajectories/run-1/events.jsonl','trajectories/run-1/stderr.log',
                  'trajectories/run-1/final-message.txt','trajectories/run-1/raw/sessions/2026/session.jsonl',
                  '.codex/sessions/2026/session.jsonl','.codex/archived_sessions/old.jsonl',
                  '.codex/history.jsonl','.codex/session_index.jsonl']
        private=['.codex/auth.json','.codex/config.toml','trajectories/run-1/auth.json',
                 'trajectories/run-1/provider.env','git-credential.json']
        for name in retained+private:self.put(home+name,'{"event":"fixture"}\n')
        row=b.cycle(self.src,self.dst);self.assertEqual(row['status'],'ok')
        self.assertEqual(set(json.loads((self.dst/'manifest.json').read_text())['files']),{home+n for n in retained})
        b.restore(self.dst,self.root/'restored')
        self.assertEqual((self.root/'restored'/home/retained[0]).read_text(),'{"event":"fixture"}\n')
    def test_pending_bulk_refreshes_appended_and_new_sessions_and_author_work(self):
        old='.state/gewu-multiverse/author-01/run-100/transcript.jsonl'
        self.put(old,'first\n')
        for i in range(8):self.put('repo/.git/objects/aa/%02d'%i)
        original=b.copy_file;calls=[]
        def first_cycle(*args):
            calls.append(args[0])
            if len(calls)==2:raise b.BudgetExpired()
            return original(*args)
        with patch.object(b,'copy_file',side_effect=first_cycle):row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['remaining_files'],8)
        self.put(old,'first\nappended\n')
        new='.state/gewu-multiverse/author-02/run-200/transcript.jsonl'
        work='.config/gewu-multiverse/agents/author-02/workspace/repo/task/solution.py'
        self.put(new,'new session\n');self.put(work,'print(42)\n')
        self.put('.state/gewu-multiverse/author-02/run-200/auth.json','private')
        bulk=[]
        def second_cycle(*args):
            if '/.git/' in str(args[0]):
                bulk.append(args[0])
                if len(bulk)==3:raise b.BudgetExpired()
            return original(*args)
        with patch.object(b,'copy_file',side_effect=second_cycle):row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['status'],'partial');self.assertEqual(row['remaining_files'],6)
        self.assertEqual(row['priority_copied'],3)
        for name in [old,new,work]:self.assertEqual((self.dst/'data'/name).read_bytes(),(self.src/name).read_bytes())
        self.assertFalse((self.dst/'data/.state/gewu-multiverse/author-02/run-200/auth.json').exists())
        row=b.cycle(self.src,self.dst,workers=1);self.assertEqual(row['status'],'ok')
        restored=b.restore(self.dst,self.root/'restored')
        self.assertTrue({old,new,work}.issubset(restored))
    def test_priority_copy_budget_still_allows_bulk_to_advance(self):
        for i in range(3):self.put('old-%02d'%i)
        with patch.object(b,'copy_file',side_effect=b.BudgetExpired()):
            b.cycle(self.src,self.dst,workers=1)
        new='.state/gewu-multiverse/author-01/run-100/transcript.jsonl'
        self.put(new,'live\n');original=b.copy_file
        def hot_expired(*args):
            if args[0].name=='transcript.jsonl':raise b.BudgetExpired()
            return original(*args)
        with patch.object(b,'copy_file',side_effect=hot_expired):row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['status'],'partial');self.assertEqual(row['remaining_files'],0)
        self.assertEqual(row['copied'],3);self.assertEqual(row['priority_remaining_files'],1)
        # Bulk is empty here. The persisted priority queue must be resumed;
        # a fresh full inventory may take longer than every cycle's budget.
        with patch.object(b,'advance_inventory',side_effect=AssertionError('inventory restarted')):
            row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['status'],'ok');self.assertEqual((self.dst/'data'/new).read_text(),'live\n')
    def test_priority_walk_without_pending_bulk_resumes(self):
        task='.config/gewu-multiverse/agents/author-01/workspace/repo/tasks/current'
        self.put(task+'/task.toml','new task')
        self.dst.mkdir()
        b.atomic(self.dst/'progress.json',dict(
            identity={'source':str(self.src),'includes':[]},inventory_at=time.time()-1,
            pending=[],warnings=[],inventory_count=0,inventory_paths=[],
            priority_pending=[],priority_walk=[task]))
        with patch.object(b,'advance_inventory',side_effect=AssertionError('inventory restarted')):
            row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['status'],'ok')
        self.assertEqual((self.dst/'data'/task/'task.toml').read_text(),'new task')
    def test_full_inventory_is_resumable_and_copies_each_discovered_prefix(self):
        expected={f'folder-{i}/data.txt' for i in range(5)}
        for name in expected:self.put(name)
        scanner=b.advance_inventory
        cursors=[]
        def bounded(source,includes,progress,deadline):
            cursors.append(progress.get('inventory_walk'))
            # Each cycle has time for two nodes, never the entire inventory.
            with patch.object(b.time,'monotonic',side_effect=[0,0,2]):
                scanner(source,includes,progress,1)
        with patch.object(b,'advance_inventory',side_effect=bounded):
            for _ in range(10):
                row=b.cycle(self.src,self.dst,workers=1)
                if row['status']=='ok':break
            else:self.fail('inventory never completed')
        self.assertIsNone(cursors[0]);self.assertTrue(all(c for c in cursors[1:]))
        self.assertEqual(set(b.restore(self.dst,self.root/'restored')),expected)
        self.assertEqual(row['inventory_remaining_paths'],0)
    def test_detached_backup_stops_when_original_owner_exits(self):
        argv=['runtime-backup','--source',str(self.src),'--target',str(self.dst),'--owner-pid','123']
        with patch('sys.argv',argv), patch('command_watchdog.process_info',return_value={'pid':123}), \
                patch('command_watchdog.owner_alive',side_effect=[True,False,False]), \
                patch.object(b,'cycle',return_value={'status':'ok'}) as cycle, \
                patch.object(b.signal,'signal'), patch.object(b.time,'sleep') as sleep:
            b.main()
        cycle.assert_called_once();sleep.assert_not_called()
    def test_live_task_hint_precedes_resumable_scan_without_expanding_secret_scope(self):
        role='.config/gewu-multiverse/agents/author-01/'
        task=role+'workspace/repo/tasks/current/'
        for name in ['task.toml','tests/test.sh','solution/solve.sh','auth.json']:
            self.put(task+name)
        outside=self.root/'outside';outside.mkdir();(outside/'work').write_text('outside')
        (self.src/(role+'workspace/repo/tasks/escape')).symlink_to(outside)
        self.put('.state/gewu-multiverse/author-01/run-100/transcript.jsonl',
                 'run /workspace/repo/tasks/current/tests/test.sh and /workspace/repo/tasks/escape/work\n')
        progress={'inventory_at':time.time(),'priority_walk':[]}
        items, warnings, complete=b.priority_inventory(self.src,[],progress,float('inf'))
        paths={i['path'] for i in items}
        self.assertTrue({task+'task.toml',task+'tests/test.sh',task+'solution/solve.sh'}.issubset(paths))
        self.assertNotIn(task+'auth.json',paths)
        self.assertFalse(any('/escape/' in p for p in paths));self.assertFalse(warnings)
    def test_unchanged_source_freshness_is_separate_from_old_modification_time(self):
        self.put('control.json','{}')
        b.cycle(self.src,self.dst)
        first=json.loads((self.dst/'manifest.json').read_text())['files']['control.json']
        row=b.cycle(self.src,self.dst)
        second=json.loads((self.dst/'manifest.json').read_text())['files']['control.json']
        self.assertEqual(row['unchanged'],1)
        self.assertEqual(first['source'],second['source'])
        self.assertGreaterEqual(second['source_checked_at'],row['started_at'])
        self.assertGreater(second['source_checked_at'],first['source_checked_at'])

    def test_large_live_scan_cannot_starve_workspace_cursor_and_resumes(self):
        run='.state/gewu-multiverse/author-01/run-100'
        expected={run+f'/nested-{i:02d}/event.jsonl' for i in range(50)}
        for name in expected:self.put(name,'event')
        task='.config/gewu-multiverse/agents/author-01/workspace/tasks/current'
        self.put(task+'/task.toml','task');expected.add(task+'/task.toml')
        progress={'inventory_at':time.time()-1,'priority_walk':[task]}
        tick=[0];scanner=b.os.scandir
        def scan(path):
            tick[0]+=1
            return scanner(path)
        with patch.object(b.os,'scandir',side_effect=scan),patch.object(b.time,'monotonic',side_effect=lambda:tick[0]):
            items,warnings,complete=b.priority_inventory(self.src,[],progress,20)
            got={i['path'] for i in items}
            self.assertIn(task+'/task.toml',got)
            self.assertTrue(progress['priority_fast_walk']);self.assertFalse(complete)
            for _ in range(20):
                items,warnings,complete=b.priority_inventory(self.src,[],progress,tick[0]+20)
                got.update(i['path'] for i in items)
                if complete:break
            self.assertTrue(complete)
            self.assertTrue(expected.issubset(got));self.assertFalse(warnings)

    def test_transient_tool_marker_is_excluded_but_historical_warning_is_audited(self):
        run='.state/gewu-multiverse/author-01/run-100/'
        self.put(run+'transcript.jsonl','retained tool event and output')
        self.put(run+'current-tool','shell')
        self.put('task/current-tool','ordinary source')
        self.dst.mkdir()
        b.atomic(self.dst/'progress.json',dict(
            identity={'source':str(self.src),'includes':[]},inventory_at=time.time()-1,
            pending=[],warnings=[{'path':run+'current-tool','kind':'FileNotFoundError'}],
            inventory_count=0,inventory_paths=[],priority_walk=[run]))
        row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['status'],'ok');self.assertFalse(row['warnings'])
        self.assertEqual(len(row['resolved_transient_warnings']),1)
        self.assertFalse((self.dst/'data'/run/'current-tool').exists())
        self.assertEqual((self.dst/'data'/run/'transcript.jsonl').read_text(),'retained tool event and output')
        self.assertFalse(b.excluded(Path('task/current-tool')))
    def test_carried_git_warning_is_separate_from_new_cycle_errors(self):
        self.put('repo/.git/objects/old');self.put('work.txt')
        original=b.copy_file;calls=[]
        def bounded(*args):
            calls.append(args[0])
            if len(calls)==1:raise FileNotFoundError()
            raise b.BudgetExpired()
        with patch.object(b,'copy_file',side_effect=bounded):first=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(first['warning_summary']['new_bulk']['count'],1)
        row=b.cycle(self.src,self.dst,workers=1)
        self.assertEqual(row['status'],'partial')
        self.assertEqual(row['warning_summary']['carried_from_inventory']['count'],1)
        self.assertEqual(row['warning_summary']['new_bulk']['count'],0)
        kinds=b.warning_counts([
            {'path':'repo/.git/objects/old','kind':'FileNotFoundError'},
            {'path':'repos/repo.git/objects/graph','kind':'FileNotFoundError'},
            {'path':'author/run/current-tool','kind':'FileNotFoundError'},
            {'path':'task.py','kind':'PermissionError'}])
        self.assertEqual(kinds['by_category'],{'git_entry_disappeared':2,
            'transient_tool_marker_disappeared':1,'copy_or_inventory_error':1})

if __name__=='__main__':unittest.main()
