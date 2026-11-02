import copy
import json
from pathlib import Path
import tempfile
import unittest
from evaluation_queue import W, digest_tree
from selftest_iteration import validate


class IterationTests(unittest.TestCase):
    def test_prior_execution_and_changed_task_required(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            state=Path(raw);job='a'*32;task=state/job/'source/proof'
            task.mkdir(parents=True);(task/'data').write_text('before')
            prior=dict(schema_version=2,role='author-01',job_id=job,status='selftest_failed',
                       source_git_tree_verified=True,source_repository='repo',source_task_path='tasks/proof',
                       completed_at=10,execution_task_path='proof',source_commit='b'*40,snapshot_sha256=digest_tree(task),
                       trials=[{'cleanup_confirmed':True},{'cleanup_confirmed':True}])
            current=dict(source_repository='repo',source_task_path='tasks/proof',received_at=11,
                         execution_task_path='proof-stage',source_commit='c'*40,snapshot_sha256='d'*64)
            link=dict(previous_job=job,note='Fixed verifier after observing the prior oracle failure.')
            def save(p): (state/job/'status.json').write_text(json.dumps(p))
            save(prior);result=validate(link,current,state,'author-01')
            self.assertTrue(result['linked_execution_verified'])
            self.assertFalse(result['author_note_independently_reviewed'])
            for field,value in [('role','author-02'),('status','building'),('completed_at',12),('source_repository','other-repo'),('source_task_path','tasks/other'),('source_git_tree_verified',False),('trials',[{'cleanup_confirmed':False}])]:
                bad=copy.deepcopy(prior);bad[field]=value;save(bad)
                with self.subTest(field=field),self.assertRaises(ValueError):validate(link,current,state,'author-01')
            save(prior)
            for field in ('source_commit','snapshot_sha256'):
                bad=dict(current);bad[field]=prior[field]
                with self.subTest(field=field),self.assertRaises(ValueError):validate(link,bad,state,'author-01')
            with self.assertRaisesRegex(ValueError,'idea ID'):
                validate(link,dict(current,idea_id='new-unbound-idea'),state,'author-01')
            for path in (None, '', '.', '..', '../proof', '/proof', 'a/b', 'a\\b'):
                save(dict(prior,execution_task_path=path))
                with self.subTest(side='prior',path=path),self.assertRaisesRegex(ValueError,'execution task path'):
                    validate(link,current,state,'author-01')
                save(prior)
                with self.subTest(side='current',path=path),self.assertRaisesRegex(ValueError,'execution task path'):
                    validate(link,dict(current,execution_task_path=path),state,'author-01')
            save(prior)
            (task/'data').write_text('tampered')
            with self.assertRaises(ValueError):validate(link,current,state,'author-01')


if __name__=='__main__':unittest.main()
