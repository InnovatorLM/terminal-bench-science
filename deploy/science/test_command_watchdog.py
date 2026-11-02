import contextlib,io,json,os,signal,subprocess,sys,tempfile,time,unittest
from pathlib import Path
import command_watchdog as watchdog
from command_watchdog import overdue,process_info,stop_command_group,owner_alive
from unittest.mock import patch
class DeadlineTests(unittest.TestCase):
    def test_budget_inspection_failure_is_visible_without_stopping_other_scans(self):
        with tempfile.TemporaryDirectory(dir=watchdog.W/'.tmp') as raw:
            root=Path(raw);config=root/'config.json';config.write_text('{"roles":{}}')
            outcome=[dict(role='author-01',status='inspection_required',error_type='JSONDecodeError')]
            output=io.StringIO()
            with patch.object(watchdog,'CONFIG',config),patch.object(watchdog,'STATE',root/'monitor'), \
                 patch.object(sys,'argv',['command-watchdog','--once','--config',str(config),
                                        '--state',str(root/'monitor'),'--session-state',str(root/'sessions')]), \
                 patch.object(watchdog,'scan'),patch('iteration_lifecycle.scan_clocks'), \
                 patch('author_budget_watchdog.scan',return_value=outcome), \
                 patch('dead_end_watchdog.scan') as dead_ends,contextlib.redirect_stdout(output):
                watchdog.main()
            records=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertIn(dict(event='author_budget_watchdog',results=outcome),records)
            dead_ends.assert_called_once()

    def test_hot_restarted_service_stops_if_launcher_exits_or_pid_is_reused(self):
        owner=dict(pid=123,uid=0,start_ticks=456,state='S')
        with patch('command_watchdog.process_info',return_value=owner):
            self.assertTrue(owner_alive(owner))
        for changed in (dict(state='Z'),dict(start_ticks=457),dict(uid=1000)):
            with patch('command_watchdog.process_info',return_value=dict(owner,**changed)):
                self.assertFalse(owner_alive(owner))
        with patch('command_watchdog.process_info',side_effect=FileNotFoundError):
            self.assertFalse(owner_alive(owner))
        self.assertTrue(owner_alive(None))

    def test_term_ignoring_command_is_killed_after_grace(self):
        process=subprocess.Popen([sys.executable,'-u','-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(30)'],start_new_session=True,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'ready')
            result=stop_command_group(process_info(process.pid),[],grace=.1)
            self.assertEqual(process.wait(timeout=2),-signal.SIGKILL)
            self.assertEqual(result['kill_signals'],1)
        finally:
            if process.poll() is None:process.kill();process.wait()
            process.stdout.close()

    def test_boundary_and_process_scope(self):
        info=dict(pid=12,pgid=12,state='S',comm='bash',start_ticks=10000)
        self.assertFalse(overdue(info,3699,100,3600))
        self.assertTrue(overdue(info,3700,100,3600))
        for change in [dict(state='Z'),dict(comm='python'),dict(pgid=11)]:
            self.assertFalse(overdue(dict(info,**change),4000,100,3600))
