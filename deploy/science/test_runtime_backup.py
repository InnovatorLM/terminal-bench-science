import json
from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import threading
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
    def test_recovery_payload_excludes_secrets_rootfs_caches_and_links(self):
        role='.config/gewu-multiverse/agents/author-01/'
        excluded=['credentials.toml','git-credential.json','dawnfm.env','service.token','app.ini','kubeconfig','private.key','custom-secret-dir/key','secrets/key','.cache/data','.tmp/cache','worker.pid','state.lock',role+'usr/lib/lib.so',role+'tmp/pip/file',role+'workspace.preserved-1/old',role+'workspace/repo/.git/config',role+'workspace/repo/.git/objects/tmp_123']
        retained=['trajectory.json','review.json','session.stdout.log','.state/gewu-multiverse/author-selftest/job/source/task/requirements.lock',role+'runs/run-1/trajectory.json',role+'workspace/repo/.git/HEAD',role+'workspace/repo/.git/objects/ab/cdef',role+'workspace/repo/task/solution/solve.sh',role+'workspace/repo/task/requirements.lock',role+'workspace/repo/task/Cargo.lock']
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

if __name__=='__main__':unittest.main()
