import json
from pathlib import Path
import tempfile
import unittest
from evaluation_queue import W
from role_evaluation_batch import requests, submit, summary, read_assignment, install_clients


class BatchTests(unittest.TestCase):
    def test_install_current_client_for_evaluators_only(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);directory=root/'usr/local/bin';directory.mkdir(parents=True)
            install_clients({'roles':{'evaluator-01':{'root':str(root)},'author-01':{}}})
            installed=directory/'role-evaluation-batch'
            self.assertEqual(installed.read_bytes(),Path(__file__).with_name('role_evaluation_batch.py').read_bytes())
            self.assertEqual(installed.stat().st_mode&0o777,0o755)
            self.assertEqual(list(directory.glob('.role-evaluation-batch-*')),[])

    def assignment(self):
        return dict(key='30:'+'a'*40,generation=0,evaluation_id='example-v1',snapshot_sha256='b'*64,state='reviewing')

    def test_submit_retry_creates_exactly_five_stable_independent_jobs(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);a=self.assignment();first=submit(a,root);again=submit(a,root)
            self.assertEqual(first,again)
            self.assertEqual(len(list(root.glob('*.json'))),5)
            self.assertEqual(len({r['job_id'] for r in first['jobs']}),5)
            self.assertNotEqual(requests(dict(a,generation=1)),first['jobs'])
            file=root/(first['jobs'][0]['job_id']+'.json');file.write_text('{}')
            with self.assertRaises(ValueError):submit(a,root)

    def test_status_distinguishes_unsubmitted_queued_and_finished_jobs(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);results=root/'results';results.mkdir();inbox=root/'inbox';inbox.mkdir()
            a=self.assignment()
            before=summary(a,results)
            self.assertFalse(before['batch_submitted'])
            self.assertTrue(all(r['status']=='not_submitted' for r in before['jobs']))
            jobs=submit(a,inbox)['jobs'];pending=summary(a,results)
            self.assertTrue(pending['batch_submitted'])
            self.assertTrue(all(r['status']=='pending' for r in pending['jobs']))
            request=jobs[0];(inbox/(request['job_id']+'.json')).unlink()
            (results/(request['job_id']+'.json')).write_text(json.dumps(dict(request,status='infrastructure_failed')))
            self.assertTrue(summary(a,results)['batch_submitted'])

    def test_resumed_review_reuses_original_batch(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);original=self.assignment()
            first=submit(original,root)
            resumed=dict(original,generation=1,batch_generation=0)
            self.assertEqual(submit(resumed,root),first)
            self.assertEqual(len(list(root.glob('*.json'))),5)
            assignment=root/'assignment';catalog=root/'catalog'
            assignment.write_text(json.dumps(resumed))
            catalog.write_text(json.dumps({'evaluations':[original]}))
            self.assertEqual(read_assignment(assignment,catalog),resumed)
            assignment.write_text(json.dumps(dict(resumed,batch_generation=2)))
            with self.assertRaises(ValueError):read_assignment(assignment,catalog)

    def test_summary_requires_five_valid_results_and_never_accepts(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);a=self.assignment();jobs=requests(a)
            self.assertIsNone(summary(a,root)['pass_rate'])
            for i,request in enumerate(jobs):
                (root/(request['job_id']+'.json')).write_text(json.dumps(dict(request,status='solver_complete',cleanup_confirmed=True,evidence_complete=True,source_version_bound=True,n_errors=0,reward=int(i<3))))
            result=summary(a,root)
            self.assertTrue(result['complete']);self.assertEqual(result['pass_rate'],.6)
            self.assertTrue(result['difficulty_threshold_met']);self.assertFalse(result['quality_acceptance'])
            self.assertFalse(result['difficulty_strict_reject'])
            fourth=root/(jobs[3]['job_id']+'.json')
            fourth.write_text(json.dumps(dict(jobs[3],status='solver_complete',cleanup_confirmed=True,evidence_complete=True,source_version_bound=True,n_errors=0,reward=1)))
            self.assertTrue(summary(a,root)['difficulty_strict_reject'])
            self.assertFalse(summary(a,root)['difficulty_threshold_met'])
            path=root/(jobs[0]['job_id']+'.json');value=json.loads(path.read_text())
            for bad in (dict(value,status='failed'),dict(value,cleanup_confirmed=False),dict(value,reward=True),dict(value,n_errors=1),dict(value,evidence_complete=False),dict(value,source_version_bound=False)):
                path.write_text(json.dumps(bad));result=summary(a,root)
                self.assertFalse(result['complete']);self.assertIsNone(result['pass_rate'])
            path.write_text(json.dumps(dict(value,snapshot_sha256='c'*64)))
            with self.assertRaises(ValueError):summary(a,root)

    def test_assignment_must_match_catalog(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);a=self.assignment();assignment=root/'assignment';catalog=root/'catalog'
            assignment.write_text(json.dumps(a));catalog.write_text(json.dumps({'evaluations':[a]}))
            self.assertEqual(read_assignment(assignment,catalog),a)
            catalog.write_text('{"evaluations":[]}')
            with self.assertRaises(ValueError):read_assignment(assignment,catalog)


if __name__=='__main__':unittest.main()
