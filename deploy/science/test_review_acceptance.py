import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review_acceptance as a
import review_dispatcher as d
from formal_review_decision import apply, name_for
from role_evaluation_batch import requests
from role_review_status import report as role_report
from execution_evidence import inspect, public_status
from publish_evidence import publish
import test_execution_evidence as fixtures_module

W = Path('/mnt/data/zhanglinfeng/workspace')


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=W / '.tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.jobs = self.root / 'jobs'
        self.role = self.root / 'evaluator-01'
        self.results = self.role / 'evaluation/results'
        self.results.mkdir(parents=True)
        (self.role / 'home/role').mkdir(parents=True)
        (self.role / 'workspace').mkdir()
        (self.root / 'author-01').mkdir()
        self.row = dict(key='a'*40+':tasks/science/example', commit='a'*40, task='tasks/science/example',
                        snapshot_sha256='b'*64, evaluation_id='sample', role='evaluator-01',
                        author_role='author-01', state='completed', generation=2, batch_generation=0,
                        review_recommendation='accept', reason='Scientific review complete; three of five solved.')
        self.row['review_completion'] = dict(key=self.row['key'], generation=2, role='evaluator-01',
            review_recommendation='accept', reason_sha256=a.sha(self.row['reason'].encode()))
        self.entry = dict(root=str(self.role), uid=0, gid=0)
        self.config = {'roles': {'evaluator-01': self.entry,
                                'author-01': dict(root=str(self.root/'author-01'), uid=0,gid=0)}}
        self.execution = {k:self.row[k] for k in ('evaluation_id', 'snapshot_sha256')}
        self.ledger = {self.row['key']: self.row}
        self.batch = requests(self.row)
        self.trials = []
        fixtures = fixtures_module.EvidenceTests()
        for index, request in enumerate(self.batch):
            job = self.jobs / request['job_id']
            config = fixtures.config();config['job_name'] += '-' + request['job_id']
            result_path, trial = fixtures.fixture(job/'runs', config)
            native = trial/'agent/sessions/run.jsonl';native.parent.mkdir()
            native.write_text('{"type":"response_item","payload":{"type":"message","content":"working"}}\n')
            reward = int(index < 3)
            trial_result = json.loads((trial/'result.json').read_text())
            trial_result['verifier_result']['rewards']['reward'] = reward
            self.write(trial/'result.json', trial_result)
            (trial/'verifier/reward.txt').write_text(str(reward))
            report = fixtures.report(config)
            evidence = inspect(report, result_path, config, self.row['snapshot_sha256'], True)
            self.assertTrue(evidence['complete'])
            self.write(job/'config.json', config);self.write(job/'report.json', report)
            self.write(job/'evidence-manifest.json', evidence)
            result = dict(request, **public_status(evidence), status='solver_complete',
                          provider='dawnfm', model='deepseek-v4.1-flash-1017',
                          cleanup_confirmed=True, n_errors=0, reward=reward)
            packet = self.role/'evaluation/evidence'/request['job_id']
            publish(trial, packet, ['SECRET-CANARY'], 0)
            result['published_evidence_manifest_sha256'] = a.digest(packet/'manifest.json')
            self.write(packet/'execution-binding.json', dict(run_id=config['job_name'],
                snapshot_sha256=self.row['snapshot_sha256'], evidence_manifest_sha256=result['evidence_manifest_sha256'],
                execution_identity=report['execution_identity'], pod_evidence=report['pod_observations'], cleanup_confirmed=True))
            self.write(self.results/(request['job_id']+'.json'), result)
            self.write(job/'result.json', result)
            self.trials.append((job, trial, packet))

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def finalize(self):
        self.write(self.root/'ledger.json', self.ledger)
        return a.finalize(self.root, self.ledger, self.config, self.jobs, lambda *_:self.execution)

    def test_acceptance_records_all_five_evidence_identities_and_survives_crash(self):
        before = copy.deepcopy(self.ledger)
        self.assertEqual(self.finalize(), {})
        self.assertTrue(self.row['scientific_acceptance'])
        self.assertGreater(self.row['formal_decided_at'], 0)
        receipt = self.root/'formal-decisions'/name_for(self.row['key'])
        original = receipt.read_bytes()
        proof = json.loads(original)['verified_evidence']
        self.assertEqual(proof['pass_rate'], .6)
        self.assertEqual(len({r['run_id'] for r in proof['trials']}), 5)
        apply(self.root, before)  # Receipt exists but accepted ledger was never saved.
        self.assertTrue(before[self.row['key']]['scientific_acceptance'])
        self.assertEqual(before[self.row['key']]['formal_decided_at'], self.row['formal_decided_at'])
        self.finalize();self.assertEqual(receipt.read_bytes(), original)
        d.publish_author_feedback({}, self.row, self.config)
        feedback = json.loads((self.root/'author-01/review-feedback.json').read_text())
        self.assertTrue(feedback['post_acceptance_required'])
        self.assertFalse(feedback['author_must_pause'])

    def test_private_original_tampering_blocks_even_when_summary_flags_are_true(self):
        (self.trials[0][1]/'agent/sessions/run.jsonl').write_text('changed')
        blocked = self.finalize()
        self.assertTrue(blocked[self.row['key']]['blockers'][0].startswith('original_file_hash_mismatch:job='))
        d.publish_author_feedback({}, self.row, self.config)
        feedback = json.loads((self.root/'author-01/review-feedback.json').read_text())
        self.assertEqual(feedback['state'], 'waiting_platform')
        self.assertTrue(feedback['author_must_pause'])
        self.assertEqual(feedback['formal_decision'], 'pending_evidence')

    def test_published_tampering_or_missing_resources_never_accept(self):
        path = self.trials[0][2]/'agent/trajectory.json';original=path.read_bytes()
        path.write_text('{}')
        self.assertTrue(self.finalize()[self.row['key']]['blockers'][0].startswith('published_file_hash_mismatch:job='))
        path.write_bytes(original)
        binding=self.trials[0][2]/'execution-binding.json'
        value=json.loads(binding.read_text());value['pod_evidence']={};self.write(binding,value)
        self.assertTrue(self.finalize()[self.row['key']]['blockers'][0].startswith('published_execution_binding_mismatch:job='))

    def test_no_acceptance_without_exact_reviewer_and_version(self):
        self.row['review_completion']['generation']=1
        self.assertIn('matching_evaluator_completion_missing',self.finalize()[self.row['key']]['blockers'])
        self.row['review_completion']['generation']=2
        self.execution['snapshot_sha256']='c'*64
        self.assertIn('registered_version_mismatch',self.finalize()[self.row['key']]['blockers'])

    def test_eighty_percent_and_invalid_timeout_cannot_pass(self):
        path=self.results/(self.batch[3]['job_id']+'.json');value=json.loads(path.read_text())
        value['reward']=1;self.write(path,value)
        self.assertIn('pass_rate_exceeds_sixty_percent',self.finalize()[self.row['key']]['blockers'])
        value.update(status='agent_timeout_requires_cause_review',reward=None);self.write(path,value)
        self.assertIn('five_valid_rollouts_required',self.finalize()[self.row['key']]['blockers'])

    def test_reject_is_not_overridden_and_missing_result_can_resume(self):
        self.row['review_recommendation']='reject'
        self.assertFalse(self.finalize());self.assertNotIn('formal_decision',self.row)
        self.row['review_recommendation']='accept'
        path=self.results/(self.batch[4]['job_id']+'.json');raw=path.read_bytes();path.unlink()
        self.assertTrue(self.finalize());self.assertFalse(self.row['scientific_acceptance'])
        path.write_bytes(raw)
        self.assertFalse(self.finalize());self.assertTrue(self.row['scientific_acceptance'])

    def test_role_cli_rejects_acceptance_before_recording_terminal_receipt(self):
        home=self.role/'home/role';workspace=self.role/'workspace'
        self.write(home/'review-assignment.json',self.row)
        path=self.results/(self.batch[4]['job_id']+'.json');path.unlink()
        with self.assertRaisesRegex(ValueError,'five valid'):
            role_report(home,workspace,'completed','final','accept',recommendation='accept')
        self.assertFalse((home/'review-terminal-status.json').exists())
        result=role_report(home,workspace,'waiting_author','scientific_defect','Repair contract')
        self.assertEqual(result['state'],'waiting_author')

    def test_dispatcher_finalizes_then_publishes_original_thread_retrospective(self):
        queue=self.root/'queue.json';self.write(queue,{'state':str(self.jobs)})
        self.row['proposal_thread']=42
        with patch.object(d,'STATE',self.root),patch('evaluation_queue.root_file',return_value=queue), \
                patch.object(d,'registered_execution',return_value=self.execution):
            d.finalize_acceptances(self.config,self.ledger)
        class Api:
            def pages(self,path):return []
        d.sync_post_acceptance(Api(),self.config,self.ledger,[])
        followup=json.loads((self.root/'author-01/post-acceptance.json').read_text())
        self.assertEqual(followup['pending'][0]['proposal_thread'],42)
        self.assertEqual(followup['pending'][0]['formal_decision'],'accepted')
        self.assertEqual(json.loads((self.root/'pending-acceptances.json').read_text()),{})


if __name__ == '__main__':
    unittest.main()
