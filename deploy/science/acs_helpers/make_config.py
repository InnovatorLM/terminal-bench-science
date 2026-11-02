#!/usr/bin/env python3
"""Emit secret-free Harbor configuration for one ACS task."""
import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

ACS_RUN_PREFIX = 'tbs-universe-linfeng-'


def build(args):
    task = Path(args.task).resolve()
    if not (task / 'task.toml').is_file() or not (task / 'instruction.md').is_file():
        raise ValueError('task must be one Harbor task directory')
    kubeconfig = Path(args.kubeconfig).resolve()
    if not kubeconfig.is_file():
        raise ValueError('kubeconfig not found')
    image = args.image
    if not image and not args.registry:
        raise ValueError('Provide --image or --registry for Dockerfile builds')
    if image:
        tail = image.rsplit('/', 1)[-1]
        if ':' not in tail and '@sha256:' not in tail:
            raise ValueError('Use an explicit task image tag or digest')
    run_id = args.run_id or ('harbor-acs-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:6])
    if not run_id.startswith(ACS_RUN_PREFIX):
        run_id = ACS_RUN_PREFIX + run_id
    if len(run_id) > 63 or not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', run_id):
        raise ValueError('run-id must be a lowercase DNS label, <=63 characters')
    profile = getattr(args, 'resource_profile', '4c8g')
    cpus, memory = {'4c8g': (4, 8192), '8c16g': (8, 16384)}[profile]
    agent_timeout = int(getattr(args, 'agent_timeout_sec', 14400))
    config = {
        'job_name': run_id, 'jobs_dir': str(Path(args.jobs_dir).resolve()),
        'n_attempts': 1, 'n_concurrent_trials': 1, 'retry': {'max_retries': 0},
        'environment': {
            'type': 'ack', 'delete': True, 'override_cpus': cpus, 'override_memory_mb': memory,
            'override_storage_mb': 30720,
            'kwargs': {
                'namespace': args.namespace, 'kubeconfig': str(kubeconfig),
                'registry': args.registry,
                'dind_image': args.dind_image,
                'use_sandbox_claim': False, 'image_pull_secret': args.pull_secret,
                'skip_image_check': not bool(args.registry),
                'pod_overrides': {
                    'metadata': {'labels': {'alibabacloud.com/acs': 'true', 'evaluation-run': run_id}},
                    'spec': {'nodeName': 'virtual-kubelet-cn-beijing-i', 'activeDeadlineSeconds': 14400, 'automountServiceAccountToken': False,
                             'containers': [{'name': 'main', 'resources': {
                                 'requests': {'cpu': str(cpus), 'memory': str(memory // 1024) + 'Gi', 'ephemeral-storage': '30Gi'},
                                 'limits': {'cpu': str(cpus), 'memory': str(memory // 1024) + 'Gi', 'ephemeral-storage': '30Gi'}}}]}}}},
        'verifier': {'override_timeout_sec': 14400},
        'datasets': [{'path': str(task.parent), 'task_names': [task.name]}],
    }
    if image:
        config['environment']['kwargs']['pod_overrides']['spec']['containers'][0]['image'] = image
    if args.mode == 'oracle':
        config['agents'] = [{'name': 'oracle', 'override_timeout_sec': 600}]
    else:
        if not args.probe:
            raise ValueError('LLM mode requires a successful --probe report')
        probe = json.loads(Path(args.probe).read_text())
        protocol = probe.get('selected_protocol')
        if not probe.get('live_api_test') or protocol not in ('chat', 'responses'):
            raise ValueError('Provider protocol has not been confirmed')
        if args.agent == 'codex-preinstall' and protocol != 'responses':
            raise ValueError('Codex requires a confirmed Responses protocol probe')
        config['agents'] = [{
            'name': args.agent, 'model_name': 'openai/' + probe['model'],
            'override_timeout_sec': agent_timeout, 'override_setup_timeout_sec': 300,
            'kwargs': {'cost_limit': '0', 'max_tokens': 2048,
                       'config': {
                           # No step limit: the four-hour wall-clock budget is
                           # the sole bounded trial budget.
                           'agent': {},
                           'model': {'model_class': 'litellm' if protocol == 'chat' else 'litellm_response',
                                     'model_kwargs': {'api_base': probe['base_url'],
                                                      # DawnFM can take longer on later tool turns;
                                                      # force a tool response and allow the request to finish.
                                                      'timeout': 300, 'num_retries': 0,
                                                      'tool_choice': 'required',
                                                      'max_tokens': 2048,
                                                      'drop_params': True}}}},
            'env': {'OPENAI_API_KEY': '${OPENAI_API_KEY}',
                    'OPENAI_BASE_URL': probe['base_url'], 'OPENAI_API_BASE': probe['base_url'],
                    'LLM_PROVIDER': probe['provider'],
                    'CODEX_MODEL_PROVIDER': 'dawnfm' if args.agent=='codex-preinstall' else '',
                    'CODEX_WIRE_API': 'responses' if args.agent=='codex-preinstall' else '',
                    'CODEX_API_KEY_ENV': 'OPENAI_API_KEY' if args.agent=='codex-preinstall' else '',
                    'LITELLM_LOCAL_MODEL_COST_MAP': 'True'},
        }]
        if args.agent == 'codex-preinstall':
            config['agents'][0]['kwargs'] = {'cost_limit': '0'}
        # The label is provider identity; openai/ is only LiteLLM's wire adapter.
        if protocol == 'responses' and args.agent != 'codex-preinstall':
            config['agents'][0]['kwargs'].pop('max_tokens')
            config['agents'][0]['kwargs']['config']['model']['model_kwargs']['max_output_tokens'] = 4096
    try:
        from .execution_budget import apply
    except ImportError:
        from execution_budget import apply
    apply(config)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resource-profile', choices=['4c8g', '8c16g'], default='4c8g')
    parser.add_argument('--task', required=True)
    parser.add_argument('--image')
    parser.add_argument('--registry')
    parser.add_argument('--dind-image', default='docker:dind')
    parser.add_argument('--agent-timeout-sec', type=int, default=14400)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--namespace', default='default')
    parser.add_argument('--pull-secret', default='acr-registry')
    parser.add_argument('--mode', choices=['llm', 'oracle'], default='llm')
    parser.add_argument('--agent', choices=['codex-preinstall', 'mini-swe-agent', 'mini-swe-agent-preinstall'], default='codex-preinstall')
    parser.add_argument('--probe')
    parser.add_argument('--run-id')
    parser.add_argument('--jobs-dir', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    config = build(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=2))
    print(json.dumps({'config': str(out.resolve()), 'run_id': config['job_name'], 'mode': args.mode, 'cpu': config['environment']['override_cpus'], 'memory_mib': config['environment']['override_memory_mb']}))


if __name__ == '__main__':
    main()
