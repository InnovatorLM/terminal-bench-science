"""Explicit current Codex/DeepSeek solver profile for source-bound task images."""
from copy import deepcopy
from acs_helpers.run_guard import check_config


def prepare(base, receipt):
    if receipt.get('harness')!='codex' or not receipt.get('published'):
        raise ValueError('Require published Codex runtime receipt')
    c=deepcopy(base)
    c.update(job_name='tbs-universe-linfeng-template',n_attempts=1,n_concurrent_trials=1)
    c['retry']={'max_retries':0}
    c.setdefault('verifier',{}).update(include_logs=[],exclude_logs=[])
    c['agents']=[dict(name=None,import_path='universe_codex:UniverseCodex',model_name='openai/deepseek-v4.1-flash-1017',
        override_timeout_sec=14400,kwargs={'version':receipt['harness_versions']['codex']},
        env=dict(OPENAI_API_KEY='${OPENAI_API_KEY}',OPENAI_BASE_URL='https://dawnfm.gravarc.cn/v1',
                 OPENAI_API_BASE='https://dawnfm.gravarc.cn/v1',CODEX_MODEL_PROVIDER='dawnfm',
                 CODEX_WIRE_API='responses',CODEX_API_KEY_ENV='OPENAI_API_KEY',
                 CODEX_SUPPORTS_WEBSOCKETS='false',CODEX_API_RETRY_ENABLED='false',LLM_PROVIDER='dawnfm'))]
    env=c['environment'];env.update(type='ack',import_path='universe_ack:UniverseACK',delete=True,
                                   override_cpus=4,override_memory_mb=8192,override_storage_mb=30720)
    kw=env['kwargs']
    for key in ['image_manifest','verifier_image_manifest','override_cpus','override_memory_mb','override_storage_mb']:
        kw.pop(key,None)
    kw.update(use_sandbox_claim=False,runtime_image=receipt['images']['runtime']['published_digest'],
              verifier_image=receipt['images']['verifier']['published_digest'])
    overrides=kw.setdefault('pod_overrides',{})
    overrides.setdefault('metadata',{}).setdefault('labels',{}).update({'alibabacloud.com/acs':'true','evaluation-run':c['job_name']})
    resources={'cpu':'4','memory':'8Gi','ephemeral-storage':'30Gi'}
    overrides['spec']={'nodeName':'virtual-kubelet-cn-beijing-i','activeDeadlineSeconds':14400,
                      'automountServiceAccountToken':False,'containers':[{'name':'main','image':kw['runtime_image'],
                      'resources':{'requests':resources.copy(),'limits':resources.copy()}}]}
    for key in list(c):
        if key.startswith('export'):c.pop(key)
    from acs_helpers.execution_budget import apply
    apply(c)
    check_config(c)
    return c


def main():
    import argparse,json
    from pathlib import Path
    from evaluation_queue import atomic_json,root_file,W
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',required=True,type=Path)
    parser.add_argument('--build-receipt',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if args.output.exists() or not args.output.resolve().is_relative_to(W):
        raise ValueError('Require fresh workspace output')
    atomic_json(args.output,prepare(json.loads(root_file(args.base).read_text()),
                                   json.loads(root_file(args.build_receipt).read_text())))


if __name__=='__main__':main()
