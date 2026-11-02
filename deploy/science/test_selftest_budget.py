import json
import os
from pathlib import Path
import tempfile
import unittest
from evaluation_queue import W
from selftest_budget import verify,merge,BudgetEvidenceRequired


class BudgetTests(unittest.TestCase):
    def fixture(self,root):
        state=root/'selftest';job=state/('a'*32);job.mkdir(parents=True)
        (job/'status.json').write_text(json.dumps(dict(role='author-01',idea_id='idea',job_id='a'*32,
            received_at=20,trials=[dict(cleanup_confirmed=True,acs_started_at=30,acs_finished_at=60)])))
        obs=root/'clock.json';obs.write_text(json.dumps(dict(role='author-01',observed_at=95,
            ideas={'idea':dict(status='active',first_observed_at=10,events=[dict(action='start',at=0)])})))
        return state,obs

    def maintenance(self,root,**changes):
        interval=dict(origin='operator_confirmed_maintenance',iteration_paused=True,
                      roles=['author-01'],started_at=40,finished_at=80)
        interval.update(changes)
        record=dict(schema_version=1,state_root=str(root),observed_at=100,intervals=[interval])
        path=root/'maintenance/verified-pauses.json';path.parent.mkdir();path.write_text(json.dumps(record));path.chmod(0o600)
        return path

    def test_verified_maintenance_and_acs_overlap_are_refunded_once(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,obs=self.fixture(root)
            self.assertEqual(verify(state,'author-01','idea',obs,100)['active_iteration_seconds_upper_bound'],70)
            self.maintenance(root)
            result=verify(state,'author-01','idea',obs,100)
            self.assertEqual(result['active_iteration_seconds_upper_bound'],50)
            self.assertEqual(result['acs_excluded_seconds'],30)
            self.assertEqual(result['maintenance_excluded_seconds'],40)
            self.assertEqual(result['total_excluded_seconds'],50)
            self.assertFalse(result['unverified_pauses_refunded'])

    def test_untrusted_or_unconfirmed_maintenance_is_refused(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,obs=self.fixture(root);path=self.maintenance(root)
            path.chmod(0o666)
            with self.assertRaises(ValueError):verify(state,'author-01','idea',obs,100)
            path.chmod(0o600);os.chown(path,21001,21001)
            with self.assertRaises(ValueError):verify(state,'author-01','idea',obs,100)
            os.chown(path,0,0);record=json.loads(path.read_text());record['intervals'][0]['iteration_paused']=False;path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError,'Unconfirmed'):verify(state,'author-01','idea',obs,100)

    def test_other_role_interval_not_refunded_and_abandoned_stays_closed(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,obs=self.fixture(root);self.maintenance(root,roles=['author-02'])
            self.assertEqual(verify(state,'author-01','idea',obs,100)['maintenance_excluded_seconds'],0)
            value=json.loads(obs.read_text());value['ideas']['idea']['status']='abandoned';obs.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'abandoned'):verify(state,'author-01','idea',obs,100)

    def test_actual_acs_intervals_refunded_once_and_unverified_pauses_not_refunded(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state=root/'state';job=state/('a'*32);job.mkdir(parents=True)
            status=dict(role='author-01',idea_id='idea',job_id='a'*32,received_at=20,
                        trials=[dict(cleanup_confirmed=True,acs_started_at=30,acs_finished_at=50),
                                dict(cleanup_confirmed=True,acs_started_at=40,acs_finished_at=60)])
            (job/'status.json').write_text(json.dumps(status))
            obs=root/'clock.json';row=dict(status='active',first_observed_at=10,events=[dict(action='start',at=0),dict(action='platform-wait',at=5),dict(action='resume',at=90)])
            value=dict(role='author-01',observed_at=95,ideas={'idea':row});obs.write_text(json.dumps(value))
            result=verify(state,'author-01','idea',obs,100)
            self.assertEqual(result['acs_excluded_seconds'],30)
            self.assertEqual(result['active_iteration_seconds_upper_bound'],70)
            self.assertTrue(result['within_budget']);self.assertFalse(result['event_origin_attested'])
            result=verify(state,'author-01','idea',obs,200000)
            self.assertFalse(result['within_budget'])
            row['first_observed_at']=21;obs.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'before its first selftest'):verify(state,'author-01','idea',obs,100)
            row['first_observed_at']=10;obs.write_text(json.dumps(value));status['trials'][0]['cleanup_confirmed']=False;(job/'status.json').write_text(json.dumps(status))
            with self.assertRaisesRegex(ValueError,'cleanup'):verify(state,'author-01','idea',obs,100)

    def test_interval_boundaries_and_nonfinite_values(self):
        self.assertEqual(merge([(-1,20),(15,30),(40,200)],0,100),[[0,30],[40,100]])
        with self.assertRaises(ValueError):merge([(0,float('nan'))],0,100)

    def test_observer_migration_gap_names_the_earliest_job_without_resetting_time(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,obs=self.fixture(root)
            value=json.loads(obs.read_text());value['ideas']['idea']['first_observed_at']=80;obs.write_text(json.dumps(value))
            with self.assertRaises(BudgetEvidenceRequired) as caught:verify(state,'author-01','idea',obs,100)
            result=caught.exception.handoff()
            self.assertEqual(result['error_class'],'iteration_observation_history_gap')
            self.assertEqual(result['evidence_gap']['earliest_selftest_received_at'],20)
            self.assertEqual(result['evidence_gap']['recorded_idea_started_at'],0)
            self.assertFalse(result['retry_requires_task_change']);self.assertFalse(result['selftest_rerun_required'])
            value['ideas']['idea']['events'][0]['at']=70;obs.write_text(json.dumps(value))
            with self.assertRaises(BudgetEvidenceRequired) as caught:verify(state,'author-01','idea',obs,100)
            self.assertEqual(caught.exception.code,'iteration_original_start_conflict')
            obs.unlink()
            with self.assertRaises(BudgetEvidenceRequired) as caught:verify(state,'author-01','idea',obs,100)
            self.assertEqual(caught.exception.code,'iteration_observation_missing')


if __name__=='__main__':unittest.main()
