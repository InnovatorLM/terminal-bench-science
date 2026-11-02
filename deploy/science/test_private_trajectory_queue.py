"""Mock solver dispatch assigns private storage; caller cannot pick destinations."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch
from evaluation_queue import W,process,validated_template


@unittest.skipUnless(os.geteuid()==0 and (W/'.config/science-evaluation/registry.json').exists(),'operator fixture required')
class RetentionQueueTests(unittest.TestCase):
    def test_worker_storage_and_aggregate_boundary(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as temporary:
            root=Path(temporary)
            for name in ['state','results','inbox']:(root/name).mkdir(mode=0o700)
            entry=dict(json.loads((W/'.config/science-evaluation/registry.json').read_text())['calibration-v1'])
            template=json.loads(Path(entry['template']).read_text());template['agents'][0].setdefault('kwargs',{})['retain_private_trajectory']=True
            path=root/'template.json';path.write_text(json.dumps(template));path.chmod(0o600)
            entry.update(template=str(path),template_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            registry=root/'registry.json';registry.write_text(json.dumps({'retention-test':entry}));registry.chmod(0o600)
            cfg={'registry':str(registry),'state':str(root/'state'),'results':str(root/'results'),'inbox':str(root/'inbox'),'evaluator_uid':21002,'max_jobs_per_day':3}
            job_id=uuid.uuid4().hex;private=W/'.state/science-private-trajectories'/job_id
            request={'job_id':job_id,'evaluation_id':'retention-test','snapshot_sha256':entry['snapshot_sha256']}
            req=root/'inbox'/(job_id+'.json');req.write_text(json.dumps(request));os.chown(req,21002,21002)
            class Child:
                pid=99999999
                def __init__(self,command,**kwargs):
                    c=json.loads(Path(command[1]).read_text());options=c['agents'][0]['kwargs']
                    assert options['private_trajectory_dir']==str(private)
                    assert 'retain_private_trajectory' not in options
                    assert json.loads((private/'identity.json').read_text())['snapshot_sha256']==entry['snapshot_sha256']
                    assert private.stat().st_mode & 0o777==0o700
                    Path(command[2]).write_text(json.dumps({'stats':{'n_completed_trials':1,'n_errored_trials':0,'evals':{'x':{'n_errors':0,'reward_stats':{'reward':{'1.0':1}}}}},'cleanup':{'confirmed':True},'harbor_exit_code':0}))
                def wait(self,timeout=None):return 0
                def poll(self):return 0
            try:
                with patch('evaluation_queue.subprocess.Popen',Child):answer=process(cfg,req,mock=False)
                self.assertEqual(answer['status'],'incomplete_evidence')
                public=(root/'results'/(job_id+'.json')).read_text()
                self.assertNotIn(str(private),public);self.assertNotIn('identity.json',public)
                binding=json.loads((private/'execution-binding.json').read_text())
                self.assertTrue(binding['snapshot_unchanged_after_run'])
                self.assertTrue(binding['cleanup_confirmed'])
                self.assertFalse(binding['trajectory_retained'])
                self.assertFalse(binding['review_input_eligible'])
                template['agents'][0]['kwargs']['private_trajectory_dir']=str(root/'caller-chosen')
                path.write_text(json.dumps(template));entry['template_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):validated_template(entry,request)
            finally:
                if private.exists():shutil.rmtree(private)


if __name__=='__main__':unittest.main()
