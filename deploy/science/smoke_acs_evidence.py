#!/usr/bin/env python3
"""Live ACS transport/cancellation smoke with a registered image, no model call."""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import uuid

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from universe_ack import UniverseACK
from acs_helpers.run_guard import Cluster, release, remember
from acs_helpers.pod_evidence import execution_identity, merge_captured
from evaluation_queue import W, atomic_json, root_file


async def smoke(template, output):
    output=Path(output)
    if os.geteuid()!=0 or output.exists() or not output.resolve().is_relative_to(W):
        raise ValueError('Require fresh private workspace smoke directory')
    output.mkdir(mode=0o700,parents=True)
    c=json.loads(root_file(template).read_text())
    run='tbs-universe-linfeng-evidence-'+uuid.uuid4().hex[:12]
    c['job_name']=run
    kw=deepcopy(c['environment']['kwargs'])
    kw['pod_overrides']['metadata']['labels']['evaluation-run']=run
    kw['pod_overrides']['spec']['activeDeadlineSeconds']=600
    c['environment']['kwargs']=kw
    journal=output/'pod-evidence';os.environ['GEWU_POD_EVIDENCE_DIR']=str(journal)
    paths=TrialPaths(output/'trial');paths.mkdir()
    environment_dir=output/'environment';environment_dir.mkdir()
    env=UniverseACK(environment_dir=environment_dir,environment_name=run,session_id=run,
        trial_paths=paths,task_env_config=EnvironmentConfig(cpus=4,memory_mb=8192,storage_mb=30720),**kw)
    cluster=Cluster(kw['kubeconfig'],kw['namespace']);known={}
    report={'run_id':run,'mode':'live-acs-evidence-transport-smoke','model_calls':0,
            'synthetic_log_fixture':True,'scientific_acceptance':False,
            'execution_identity':execution_identity(c),'cleanup':{'confirmed':False}}
    try:
        await asyncio.wait_for(env.start(),timeout=300)
        snapshot=await asyncio.to_thread(cluster.snapshot,run);remember(snapshot,known)
        script='''from pathlib import Path
import json
p=Path('/tmp/codex-home/sessions/smoke/run.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(json.dumps({'type':'smoke_only','text':'partial execution evidence'})+'\\n')
Path('/logs/agent/codex.txt').write_text('partial ACS transport smoke\\n')
Path('/tmp/transport-smoke.bin').write_bytes(bytes(range(256))*8192)
'''
        created=await env.exec(command='python3 -I -c '+shlex.quote(script),timeout_sec=30)
        if created.return_code!=0:raise RuntimeError('Unable to write smoke fixture')
        await env._retain_incomplete_agent_evidence(checkpoint=True)
        session=paths.agent_dir/'sessions/smoke/run.jsonl'
        if not session.is_file() or 'partial execution evidence' not in session.read_text():
            raise RuntimeError('Native session checkpoint missing')
        report['checkpoint_retained']=True
        target=output/'binary-evidence.bin'
        await env.download_file('/tmp/transport-smoke.bin',target)
        expected=hashlib.sha256(bytes(range(256))*8192).hexdigest()
        if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:raise RuntimeError('Binary transfer differs')
        report['binary_transfer_sha256']=expected
        result=await env.exec(command='sleep 3',timeout_sec=1)
        if result.return_code!=124:raise RuntimeError('Expected bounded command timeout')
        report['command_timeout_observed']=True
        await env.stop(delete=True)
        record=json.loads((paths.agent_dir/'acs-collection.json').read_text())
        if record['status']!='collected' or not session.is_file():raise RuntimeError('Final collection missing')
        report['final_collection_retained']=True
    except Exception as error:
        report['error_type']=type(error).__name__
    finally:
        # Cancel the checkpoint loop even on failure. Cleanup is still scoped
        # to this smoke's label and observed UIDs.
        try:await asyncio.wait_for(env.stop(delete=True),timeout=180)
        except Exception as error:report['adapter_stop_error_type']=type(error).__name__
        try:
            merge_captured(report,journal,known)
            report['cleanup']=await asyncio.to_thread(release,cluster,run,known,report)
        except Exception as error:report['cleanup_error_type']=type(error).__name__
        atomic_json(output/'report.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    result=asyncio.run(smoke(args.template,args.output))
    print(json.dumps({key:result.get(key) for key in ('run_id','model_calls','checkpoint_retained',
        'command_timeout_observed','final_collection_retained','error_type','cleanup')}))
    raise SystemExit(0 if result.get('final_collection_retained') and result['cleanup']['confirmed'] else 1)
