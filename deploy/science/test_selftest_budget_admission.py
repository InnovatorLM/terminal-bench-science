"""Completed-version budget tests using local receipts; no cloud or model calls."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation_queue import W
from selftest_budget import BudgetEvidenceRequired, verify
from selftest_budget_admission import assess_budget, read_audit, validate_admission, IDENTITY
from selftest_handoff import qualify
from author_submission_gate import validate_receipt
import test_selftest_handoff as fixtures


def write(path, value):
    path.write_text(json.dumps(value));path.chmod(0o600)


class CompletedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=W/'.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.state,self.work,self.obs,self.template,self.status=fixtures.HandoffTests().fixture(self.root)

    def history_gap(self):
        value=json.loads(self.obs.read_text())
        value['ideas']['idea']['first_observed_at']=80
        write(self.obs,value)

    def audit(self):
        history=self.work/'legacy-clock-evidence.json';history.write_bytes(self.obs.read_bytes())
        audit=dict(schema_version=1,kind='legacy_completed_budget_unknown',operator_reviewed=True,
            scope='completed_frozen_version_only',budget_error_code='iteration_observation_history_gap',
            reason='The original observation history is unavailable; the measured duration remains unknown.',
            completed_at=self.status['completed_at'],audited_at=100,
            status_file=str(self.work/'status.json'),
            status_sha256=hashlib.sha256((self.work/'status.json').read_bytes()).hexdigest(),
            evidence_files=[dict(path=str(history),sha256=hashlib.sha256(history.read_bytes()).hexdigest())],
            **{key:self.status[key] for key in IDENTITY})
        path=self.work/'legacy-budget-admission.json';write(path,audit)
        return path,audit

    def test_completed_version_does_not_accrue_handoff_wait(self):
        result=assess_budget(self.work,self.state,self.status,self.obs,200000)
        self.assertEqual(result['active_iteration_seconds_upper_bound'],60)
        self.assertEqual(result['iteration_cutoff_at'],100)
        self.assertTrue(result['within_budget'])
        self.assertFalse(verify(self.state,'author-01','idea',self.obs,200000)['within_budget'])
        with patch('selftest_handoff.time.time',return_value=200000):
            receipt=qualify(self.work,self.state,self.obs,self.template)
        self.assertEqual(receipt['schema_version'],3)
        self.assertEqual(receipt['active_iteration_seconds'],60)

    def test_later_running_jobs_do_not_reopen_completed_version_cleanup(self):
        later=self.state/('f'*32);later.mkdir()
        write(later/'status.json',dict(role='author-01',idea_id='idea',job_id=later.name,
            received_at=200,trials=[dict(cleanup_confirmed=False,acs_started_at=210)]))
        result=assess_budget(self.work,self.state,self.status,self.obs,300)
        self.assertEqual(result['selftest_jobs'],['a'*32,'b'*32])
        self.assertEqual(result['active_iteration_seconds_upper_bound'],60)

    def test_history_gap_without_operator_admission_stays_blocked(self):
        self.history_gap()
        with self.assertRaises(BudgetEvidenceRequired):
            assess_budget(self.work,self.state,self.status,self.obs,30000)
        self.assertFalse((self.work/'handoff-qualified.json').exists())

    def test_explicit_admission_preserves_unknown_through_actual_qualification(self):
        self.history_gap();self.audit()
        with patch('selftest_handoff.time.time',return_value=30000):
            receipt=qualify(self.work,self.state,self.obs,self.template)
        self.assertEqual(receipt['schema_version'],4)
        self.assertIsNone(receipt['active_iteration_seconds'])
        proof=json.loads((self.work/'handoff-evidence.json').read_text())
        self.assertIsNone(proof['budget']['within_budget'])
        self.assertEqual(proof['budget']['iteration_cutoff_at'],100)
        validate_receipt(receipt,self.status['source_commit'],self.status['source_task_path'])

    def test_admission_cannot_be_reused_by_another_job_role_idea_or_snapshot(self):
        self.history_gap();self.audit()
        budget=assess_budget(self.work,self.state,self.status,self.obs,100)
        for key in IDENTITY:
            with self.subTest(key=key):
                other=dict(self.status);other[key]='another-identity'
                with self.assertRaisesRegex(ValueError,'completed version'):
                    validate_admission(budget,self.status['source_commit'],self.status['source_task_path'],expected_status=other)

    def test_tampered_audit_status_evidence_and_nonoperator_files_are_refused(self):
        self.history_gap();path,audit=self.audit()
        read_audit(path,self.status)
        evidence=self.work/'legacy-clock-evidence.json';raw=evidence.read_bytes()
        evidence.write_text('{}')
        with self.assertRaisesRegex(ValueError,'historical evidence changed'):read_audit(path,self.status)
        evidence.write_bytes(raw)
        write(path,dict(audit,job_id='a'*32))
        with self.assertRaisesRegex(ValueError,'exact-version'):read_audit(path,self.status)
        write(path,audit)
        os.chown(path,21001,21001)
        try:
            with self.assertRaisesRegex(ValueError,'ownership'):read_audit(path,self.status)
        finally:os.chown(path,0,0)
        status_path=self.work/'status.json';write(status_path,dict(self.status,added='changed'))
        with self.assertRaisesRegex(ValueError,'source status changed'):read_audit(path,self.status)

    def test_admission_does_not_bypass_required_iteration_or_controls(self):
        self.history_gap();self.audit()
        request_path=self.work/'request.json';request=json.loads(request_path.read_text())
        write(request_path,dict(request,iteration=None))
        with self.assertRaisesRegex(ValueError,'Prior selftest'):qualify(self.work,self.state,self.obs,self.template)
        write(request_path,request)
        self.status['trials'][1]['results'][0]['rewards']['reward']=1
        write(self.work/'status.json',self.status)
        self.audit()
        with self.assertRaises(ValueError):qualify(self.work,self.state,self.obs,self.template)
        self.assertFalse((self.work/'handoff-qualified.json').exists())

    def test_cached_gate_rejects_live_abandonment_but_allows_finished(self):
        with patch('selftest_handoff.time.time',return_value=100):
            receipt=qualify(self.work,self.state,self.obs,self.template)
        current=self.state.parent/'command-watchdog/iteration-observations/author-01.json'
        current.parent.mkdir(parents=True)
        value=json.loads(self.obs.read_text());value['ideas']['idea']['status']='finished';write(current,value)
        validate_receipt(receipt,self.status['source_commit'],self.status['source_task_path'])
        value['ideas']['idea']['status']='abandoned';write(current,value)
        with self.assertRaisesRegex(ValueError,'abandoned'):
            validate_receipt(receipt,self.status['source_commit'],self.status['source_task_path'])

    def test_legacy_admission_cannot_revive_abandoned_idea(self):
        self.history_gap();self.audit()
        value=json.loads(self.obs.read_text());value['ideas']['idea']['status']='abandoned';write(self.obs,value)
        with self.assertRaisesRegex(ValueError,'abandoned'):
            assess_budget(self.work,self.state,self.status,self.obs,100)

    def test_completion_in_future_or_before_receipt_is_rejected(self):
        for instant in (50,301):
            with self.subTest(completed_at=instant):
                with self.assertRaisesRegex(ValueError,'chronology'):
                    assess_budget(self.work,self.state,dict(self.status,completed_at=instant),self.obs,300)


if __name__=='__main__':unittest.main()
