import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import io
from unittest.mock import patch
from urllib.error import HTTPError
from universe_hourly_report import review_counts, render, redact, candidate_task, publish, scheduler, runtime_status, llm_summaries, llm_summaries_once, W


class ReportEvidenceTests(unittest.TestCase):
    def test_summary_accepts_direct_array_but_still_requires_all_roles(self):
        row={'role':'author-01','title':'verified','summary':'work','next_step':'check','blocker':''}
        payload={'choices':[{'message':{'content':json.dumps([row])}}]}
        with patch.dict('os.environ',{'LLM_PROVIDER':'dawnfm','OPENAI_MODEL':'deepseek-v4.1-flash-1017','OPENAI_BASE_URL':'https://dawnfm.gravarc.cn/v1','OPENAI_API_KEY':'fixture-only'},clear=True), patch('universe_hourly_report.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(llm_summaries_once([{'role':'author-01'}])['author-01']['title'],'verified')
            with self.assertRaisesRegex(ValueError,'Incomplete'):
                llm_summaries_once([{'role':'author-01'},{'role':'author-02'}])
    def test_summary_recovers_after_502_and_preserves_evidence(self):
        evidence=[{'role':'author-01'}];answer={'author-01':{'title':'restored'}}
        with patch('universe_hourly_report.llm_summaries_once',side_effect=[HTTPError('http://fixture',502,'fixture',{},None),answer]) as call, patch('universe_hourly_report.time.sleep') as wait:
            self.assertEqual(llm_summaries(evidence),answer)
            self.assertEqual(call.call_count,2)
            self.assertEqual(call.call_args.args,(evidence,))
            wait.assert_called_once_with(2)

    def test_summary_permanent_errors_fail_directly_and_outages_are_bounded(self):
        with patch('universe_hourly_report.llm_summaries_once',side_effect=HTTPError('http://fixture',401,'fixture',{},None)) as call, patch('universe_hourly_report.time.sleep') as wait:
            with self.assertRaises(HTTPError):llm_summaries([])
            self.assertEqual(call.call_count,1);wait.assert_not_called()
        with patch('universe_hourly_report.llm_summaries_once',side_effect=HTTPError('http://fixture',502,'fixture',{},None)) as call, patch('universe_hourly_report.time.sleep'):
            with self.assertRaises(HTTPError):llm_summaries([])
            self.assertEqual(call.call_count,3)
    def test_blocked_and_dead_sessions_never_look_like_normal_waiting(self):
        self.assertEqual(runtime_status({'state':'blocked','reason':'configured role broker is unavailable'},False)[0],'执行环境故障')
        self.assertEqual(runtime_status({'state':'running'},False)[0],'会话已结束')
        self.assertEqual(runtime_status({'state':'restarting'},False)[0],'等待调度')
        self.assertEqual(runtime_status({'state':'running'},True,901)[0],'等待命令返回')
        self.assertEqual(runtime_status({'state':'running'},True,0,{'state':'retrying','attempt':2})[0],'模型重试中')
    def test_scheduler_publishes_once_at_hour_boundary(self):
        status={'last_slot':0}
        calls=[]
        def fake_publish(config):
            calls.append(config);status['last_slot']=1
        with patch('universe_hourly_report.read_json',side_effect=lambda *args:dict(status)), \
             patch('universe_hourly_report.time.time',side_effect=[3599,3600,3601]), \
             patch('universe_hourly_report.time.sleep',side_effect=[None,None,StopIteration]), \
             patch('universe_hourly_report.publish',side_effect=fake_publish):
            with self.assertRaises(StopIteration):scheduler({'state':str(W/'.tmp')})
        self.assertEqual(len(calls),1)

    def test_failed_generation_keeps_previous_report(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as temp:
            root=Path(temp);(root/'public').mkdir();(root/'public/index.html').write_text('previous valid report')
            with patch('universe_hourly_report.collect',side_effect=RuntimeError('fixture')):
                with self.assertRaises(RuntimeError):publish({'state':temp})
            self.assertEqual((root/'public/index.html').read_text(),'previous valid report')

    def test_reading_an_upstream_task_is_not_author_progress(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as temp:
            root=Path(temp);relative='tasks/domain/subject/example'
            task=root/'workspace/terminal-bench-science'/relative;task.mkdir(parents=True)
            raw=b'An upstream example\n';(task/'instruction.md').write_bytes(raw)
            reference={relative+'/instruction.md':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}
            calls=[{'command':'cat /workspace/terminal-bench-science/'+relative+'/instruction.md','ts':'2026-11-03T00:00:00Z'}]
            self.assertEqual(candidate_task(root,calls,0,reference),(None,None))
            (task/'proposal.md').write_text('A new author proposal\n')
            self.assertEqual(candidate_task(root,calls,0,reference)[1],relative)

    def test_assignment_does_not_inflate_started_and_versions_do_not_inflate_tasks(self):
        ledger={'a':{'task':'one','state':'reviewing'},
                'b':{'task':'two','state':'reviewing','last_activity_at':1},
                'c':{'task':'two','state':'completed','formal_decision':'accepted','scientific_acceptance':True},
                'd':{'task':'three','state':'completed','review_recommendation':'accept','review_completion':{'key':'d'}}}
        result=review_counts(ledger)
        self.assertEqual(result['assigned_versions'],4)
        self.assertEqual(result['started_versions'],3)
        self.assertEqual(result['started_tasks'],2)
        self.assertEqual(result['accepted_tasks'],1)
        self.assertEqual(result['accepted_versions'],1)

    def test_report_text_cannot_close_json_script(self):
        output=render({}, {'title':'</script><script>alert(1)</script>'}, [])
        self.assertNotIn('</script><script>alert(1)',output)
        marker='<script type="application/json" id="report-data">'
        data=output.split(marker)[1].split('</script>')[0]
        self.assertEqual(json.loads(data)['report']['title'],'</script><script>alert(1)</script>')

    def test_credentials_redacted_before_summary(self):
        output=redact('Authorization: Bearer abcdefg password=letmein https://user:private@example.org/')
        self.assertNotIn('letmein',output)
        self.assertNotIn('private@',output)
        self.assertNotIn('abcdefg',output)


if __name__=='__main__':unittest.main()
