"""Offline real file/client/consumer tests; never invoke ACS, builds or a model."""
import contextlib
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from evaluation_queue import W
import role_acs_selftest as client
import selftest_review_supplement as supplement
import test_selftest_handoff as fixtures
from selftest_handoff import qualify


def concurrent_capture(role, entry, source, state, barrier, results):
    try:
        barrier.wait(timeout=10)
        results.put(('ok',str(supplement.capture(role,entry,source,state))))
    except BaseException as error:
        results.put(('error',type(error).__name__))


def concurrent_reconcile(work,state,entry,universe,barrier,counter,results):
    from selftest_handoff import reconcile
    def run(*_):
        with counter.get_lock():
            counter[0]+=1;counter[1]=max(counter[0],counter[1])
        time.sleep(0.1)
        with counter.get_lock():counter[0]-=1
        return dict(state='waiting_platform',stage='immutable_execution_registration',scientific_acceptance=False)
    try:
        barrier.wait(timeout=10)
        with patch('selftest_handoff.handoff',side_effect=run):reconcile(work,state,entry,universe)
        results.put('ok')
    except BaseException as error:results.put(type(error).__name__)


class SupplementTests(unittest.TestCase):
    def fixture(self,root):
        state,work,obs,template,status=fixtures.HandoffTests().fixture(root)
        original={key:status[key] for key in ('job_id','idea_id','source_repository','source_task_path','source_commit')}
        original['archive_sha256']='d'*64
        (work/'request.json').write_text(json.dumps(original))
        role=root/'role';base=role/'selftest'
        (base/'inbox').mkdir(parents=True);(base/'results').mkdir()
        (base/'results'/(work.name+'.json')).write_text(json.dumps(status))
        entry=dict(root=str(role),uid=os.getuid(),gid=os.getgid())
        return state,work,obs,template,status,entry,base

    def queue(self,work,base):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            client.main(['request-review',work.name,'--previous-job','a'*32,
                         '--iteration-note','Fixed the verifier using the prior execution diagnostic.'],base=base)
        result=json.loads(output.getvalue())
        return base/'inbox'/('review-'+result['request_id']+'.json')

    def test_real_client_capture_qualification_retains_execution_and_immutable_evidence(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status,entry,base=self.fixture(Path(raw))
            files=[work/'request.json',work/'status.json',work/'build/receipt.json']
            before={str(path):path.read_bytes() for path in files}
            source=self.queue(work,base)
            self.assertEqual(self.queue(work,base),source)
            with patch('subprocess.run',side_effect=AssertionError('No cloud/build/process calls allowed')):
                self.assertEqual(supplement.process_request('author-01',entry,source,state),work)
                receipt=qualify(work,state,obs,template)
                self.assertTrue(receipt['iteration_applied'])
                self.assertEqual(receipt['budget_status'],'verified_within_budget')
                self.assertEqual(receipt['active_iteration_seconds'],60)
                self.assertEqual(qualify(work,state,obs,template),receipt)
            self.assertEqual({str(path):path.read_bytes() for path in files},before)
            evidence=work/'review-supplement.json'
            self.assertEqual(evidence.stat().st_mode&0o777,0o600)
            self.assertIn(str(evidence),receipt['check_evidence']['post_iteration_rerun'])
            self.assertIn(str(evidence),{item['path'] for item in receipt['evidence_files']})
            self.assertFalse(any(path.suffix=='.tar' for path in (base/'inbox').iterdir()))
            source.write_text('{}')
            self.assertEqual(qualify(work,state,obs,template),receipt) # accepted root snapshot survives author edits
            changed=dict(status,completed_at=101);(work/'status.json').write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError,'immutable execution'):
                qualify(work,state,obs,template)

    def test_later_abandonment_revokes_cached_qualification(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status,entry,base=self.fixture(Path(raw))
            supplement.capture('author-01',entry,self.queue(work,base),state)
            qualify(work,state,obs,template)
            observed=json.loads(obs.read_text());observed['ideas']['idea']['status']='abandoned'
            obs.write_text(json.dumps(observed))
            from selftest_handoff import AuthorEvidenceRequired
            with self.assertRaisesRegex(AuthorEvidenceRequired,'Idea abandoned'):
                qualify(work,state,obs,template)

    def test_concurrent_capture_is_single_append_and_conflicting_intent_cannot_replace_it(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status,entry,base=self.fixture(Path(raw))
            source=self.queue(work,base)
            ctx=multiprocessing.get_context('fork');barrier=ctx.Barrier(2);results=ctx.Queue()
            processes=[ctx.Process(target=concurrent_capture,args=('author-01',entry,source,state,barrier,results)) for _ in range(2)]
            for process in processes:process.start()
            for process in processes:process.join(timeout=15);self.assertEqual(process.exitcode,0)
            self.assertEqual([results.get(timeout=1)[0] for _ in processes],['ok','ok'])
            captured=(work/'review-supplement.json').read_bytes()
            request=json.loads(source.read_text());request['iteration']['note']='A conflicting explanation cannot overwrite the original operator captured request.'
            other=source.parent/('review-'+supplement.request_id(request)+'.json');other.write_bytes(supplement.encoded(request))
            with self.assertRaisesRegex(ValueError,'another immutable'):
                supplement.capture('author-01',entry,other,state)
            self.assertEqual((work/'review-supplement.json').read_bytes(),captured)

    def test_periodic_consumer_and_worker_handoff_are_serialized(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status,entry,base=self.fixture(root)
            supplement.capture('author-01',entry,self.queue(work,base),state)
            ctx=multiprocessing.get_context('fork');barrier=ctx.Barrier(2);counter=ctx.Array('i',[0,0]);results=ctx.Queue()
            processes=[ctx.Process(target=concurrent_reconcile,args=(work,state,entry,root/'universe',barrier,counter,results)) for _ in range(2)]
            for process in processes:process.start()
            for process in processes:process.join(timeout=15);self.assertEqual(process.exitcode,0)
            self.assertEqual([results.get(timeout=1) for _ in processes],['ok','ok'])
            self.assertEqual(counter[1],1)

    def test_truncated_input_is_retryable_without_cloud_execution(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state,work,obs,template,status,entry,base=self.fixture(Path(raw))
            source=self.queue(work,base);complete=source.read_bytes();source.write_bytes(complete[:30])
            self.assertIsNone(supplement.process_request('author-01',entry,source,state))
            result=json.loads((base/'results'/source.name).read_text())
            self.assertEqual(result['state'],'rejected');self.assertFalse(result['cloud_execution_started'])
            self.assertFalse((work/'review-supplement.json').exists())
            source.write_bytes(complete)
            self.assertEqual(supplement.process_request('author-01',entry,source,state),work)

    def test_wrong_owner_other_author_stale_source_missing_original_idea_are_rejected(self):
        for kind in ('owner','author','source','original_idea'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
                state,work,obs,template,status,entry,base=self.fixture(Path(raw))
                source=self.queue(work,base);role='author-01'
                if kind=='owner':entry['uid']=12345
                elif kind=='author':role='author-02'
                elif kind=='source':
                    request=json.loads(source.read_text());request['source_commit']='f'*40
                    source=source.parent/('review-'+supplement.request_id(request)+'.json');source.write_bytes(supplement.encoded(request))
                else:
                    original=json.loads((work/'request.json').read_text());original.pop('idea_id');(work/'request.json').write_text(json.dumps(original))
                with self.assertRaises(ValueError):supplement.capture(role,entry,source,state)
                self.assertFalse((work/'review-supplement.json').exists())

    def test_periodic_consumer_retries_platform_handoff_with_backoff_and_changed_dependencies(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status,entry,base=self.fixture(root)
            self.queue(work,base);config={'roles':{'author-01':entry}}
            broken=state/('0'*32);broken.mkdir();(broken/'status.json').write_text('{')
            with patch('selftest_handoff.handoff',side_effect=ValueError('Missing platform evidence')) as handoff:
                first=supplement.consume_once(config,state,root/'universe',now=100)
                self.assertEqual(first['attempted'],1);self.assertEqual(handoff.call_count,1)
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=120)['attempted'],0)
                (work/'legacy-budget-admission.json').write_text('{}')
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=125)['attempted'],1)
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=130)['attempted'],0)
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=246)['attempted'],1)
                self.assertEqual(handoff.call_count,3)
            # A genuine successful qualified handoff is never cloud retried.
            (work/'handoff.json').write_text(json.dumps({'state':'ready_for_independent_review'}))
            with patch('selftest_handoff.handoff') as handoff:
                supplement.consume_once(config,state,root/'universe',now=10000);handoff.assert_not_called()
            self.assertEqual(json.loads((work/'status.json').read_text()),status)

    def test_observer_heartbeat_and_other_ideas_do_not_bypass_retry_backoff(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status,entry,base=self.fixture(root)
            self.queue(work,base);config={'roles':{'author-01':entry}}
            observation=state.parent/'command-watchdog/iteration-observations/author-01.json'
            observation.parent.mkdir(parents=True);observed=json.loads(obs.read_text())
            observation.write_text(json.dumps(observed))
            with patch('selftest_handoff.handoff',side_effect=ValueError('Missing platform evidence')) as handoff:
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=100)['attempted'],1)
                before=supplement.retry_signature(work,state,'author-01',root/'universe')
                observed.update(observed_at=130,runner_live=True,previous_observation_sha256='f'*64)
                observed['ideas']['unrelated']=dict(events=[dict(action='start',at=129)],status='active',first_observed_at=129)
                observed['recovery_evidence']={'unrelated':{'sources':[]}}
                observation.write_text(json.dumps(observed))
                self.assertEqual(supplement.retry_signature(work,state,'author-01',root/'universe'),before)
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=130)['attempted'],0)
                self.assertEqual(handoff.call_count,1)
                # A real transition for this exact idea permits an early retry.
                observed['ideas']['idea']['status']='session_paused'
                observed['ideas']['idea']['events'].append(dict(action='session-pause',at=130))
                observation.write_text(json.dumps(observed))
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=131)['attempted'],1)
                # Authentic recovery metadata is also a meaningful dependency.
                observed['recovery_evidence']['idea']={'origin':'matching_operator_observation_prefix'}
                observation.write_text(json.dumps(observed))
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=140)['attempted'],1)
                self.assertEqual(handoff.call_count,3)

    def test_explicit_legacy_review_requests_also_reconcile_and_author_wait_requires_change(self):
        from selftest_handoff import AuthorEvidenceRequired
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            root=Path(raw);state,work,obs,template,status,entry,base=self.fixture(root)
            request=json.loads((work/'request.json').read_text());request.update(request_review=True,iteration=dict(previous_job='a'*32,note='Fixed the verifier using the prior execution diagnostic.'))
            (work/'request.json').write_text(json.dumps(request));config={'roles':{'author-01':entry}}
            with patch('selftest_handoff.handoff',side_effect=AuthorEvidenceRequired('Needs author evidence')) as handoff:
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=100)['attempted'],1)
                self.assertEqual(supplement.consume_once(config,state,root/'universe',now=1000000)['attempted'],0)
                self.assertEqual(handoff.call_count,1)
            self.assertFalse((work/'review-supplement.json').exists())


if __name__=='__main__':unittest.main()
