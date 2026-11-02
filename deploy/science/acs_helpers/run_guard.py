#!/usr/bin/env python3
"""Run a tagged Harbor ACS job; fail closed unless cleanup is confirmed."""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
try:
    from .pod_evidence import execution_identity, observe, merge_captured, validate as validate_pod_evidence
    from .execution_budget import budgets
except ImportError:
    from pod_evidence import execution_identity, observe, merge_captured, validate as validate_pod_evidence
    from execution_budget import budgets


KINDS = ('sandboxsets', 'sandboxclaims', 'sandboxes', 'pods', 'jobs', 'configmaps')
RUN_PREFIX = 'tbs-universe-linfeng-'

def watchdog_snapshot(kubeconfig, namespace, out, kill=False):
    script = Path(__file__).with_name('acs_prefix_watchdog.py')
    cmd = [sys.executable, str(script), '--kubeconfig', kubeconfig,
           '--namespace', namespace, '--out', str(out)]
    if kill:
        cmd.append('--kill')
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=90)


def check_config(c):
    env = c['environment']
    kw = env['kwargs']
    run_id = c['job_name']
    if not run_id.startswith(RUN_PREFIX):
        raise ValueError('ACS run_id must use the tbs-universe-linfeng- prefix')
    if env.get('type') != 'ack' or kw.get('use_sandbox_claim') or not env.get('delete'):
        raise ValueError('Guard requires ACK direct Pod mode and delete=true')
    if kw['pod_overrides']['metadata']['labels'].get('evaluation-run') != run_id:
        raise ValueError('Run label must equal job_name')
    podspec = kw['pod_overrides']['spec']
    forbidden = podspec.get('nodeName', '').endswith('-f') or podspec.get('nodeSelector', {}).get('topology.kubernetes.io/zone') == 'cn-beijing-f'
    if forbidden:
        raise ValueError('北京 ACS cn-beijing-f is forbidden for Universe runs')
    # Current successful path is pinned to cn-beijing-i; a future change must be explicit.
    if podspec.get('nodeName') != 'virtual-kubelet-cn-beijing-i':
        raise ValueError('Universe runs must pin virtual-kubelet-cn-beijing-i')
    resources = podspec['containers'][0]['resources']
    profile = (env.get('override_cpus'), env.get('override_memory_mb'))
    if profile not in ((4, 8192), (8, 16384)):
        raise ValueError('Supported resource profiles are 4c8g and 8c16g')
    cpu, memory = str(profile[0]), str(profile[1] // 1024) + 'Gi'
    if any(resources[k].get('cpu') != cpu or resources[k].get('memory') != memory for k in ('requests', 'limits')):
        raise ValueError('Pod requests/limits must match selected resource profile')
    deadline = podspec.get('activeDeadlineSeconds', 0)
    # Accept legacy registered templates at validation time. Launchers replace
    # their infrastructure TTL before execution without changing task bytes.
    if type(deadline) is not int or not 0 < deadline <= budgets(c)['pod_seconds']:
        raise ValueError('Pod lifetime exceeds the bounded execution budget')
    return run_id, kw


class Cluster:
    def __init__(self, kubeconfig, namespace):
        config.load_kube_config(config_file=kubeconfig)
        self.api = client.ApiClient()
        self.namespace = namespace

    def path(self, kind):
        if kind == 'pods' or kind == 'configmaps':
            prefix = '/api/v1'
        elif kind == 'jobs':
            prefix = '/apis/batch/v1'
        else:
            prefix = '/apis/agents.kruise.io/v1alpha1'
        return prefix + '/namespaces/' + self.namespace + '/' + kind

    def call(self, path, method='GET', query=None, body=None):
        method, url, headers, body, post = self.api.param_serialize(
            method, path, query_params=query or [], body=body,
            auth_settings=['BearerToken'])
        response = self.api.call_api(method, url, header_params=headers, body=body,
                                     post_params=post, _request_timeout=(10, 25))
        response.read()
        if response.status < 200 or response.status >= 300:
            raise ApiException(status=response.status, reason=response.data)
        if not response.data:
            return {}
        return json.loads(response.data.decode() if isinstance(response.data, bytes) else response.data)

    def list(self, kind, run_id):
        result = []
        token = ''
        while True:
            query = [('labelSelector', 'evaluation-run=' + run_id), ('limit', 200)]
            if token:
                query.append(('continue', token))
            try:
                data = self.call(self.path(kind), query=query)
            except ApiException as exc:
                # 404 means this CRD API is absent, not an authorization success.
                if kind != 'pods' and exc.status == 404:
                    return []
                raise
            result.extend(data.get('items', []))
            token = data.get('metadata', {}).get('continue', '')
            if not token:
                return result

    def snapshot(self, run_id):
        return {kind: self.list(kind, run_id) for kind in KINDS}

    def deleted(self, kind, name):
        try:
            self.call(self.path(kind) + '/' + name)
            return False
        except ApiException as exc:
            if exc.status == 404:
                return True
            raise


def remember(snapshot, known):
    for kind, items in snapshot.items():
        for obj in items:
            meta = obj['metadata']
            known[(kind, meta['name'])] = meta['uid']


def release(cluster, run_id, known, report=None):
    snapshot = cluster.snapshot(run_id)
    remember(snapshot, known)
    if report is not None: observe(snapshot, report)
    deleted = []
    # Delete controlling resources first; never remove finalizers or touch other jobs.
    for kind in KINDS:
        for (item_kind, name), uid in list(known.items()):
            if kind != item_kind:
                continue
            try:
                cluster.call(cluster.path(kind) + '/' + name, 'DELETE', body={
                    'apiVersion': 'v1', 'kind': 'DeleteOptions',
                    'preconditions': {'uid': uid}, 'gracePeriodSeconds': 0,
                    'propagationPolicy': 'Foreground'})
                deleted.append({'kind': kind, 'name': name})
            except ApiException as exc:
                if exc.status != 404:
                    raise
    deadline = time.monotonic() + 120
    clean_passes = 0
    while time.monotonic() < deadline:
        remaining = cluster.snapshot(run_id)
        missing = {kind + '/' + name: cluster.deleted(kind, name) for kind, name in known}
        clear = not any(remaining.values()) and all(missing.values())
        clean_passes = clean_passes + 1 if clear else 0
        if clean_passes >= 2:
            return {'confirmed': True, 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
                    'deleted_by_guard': deleted, 'known_resource_404': missing,
                    'remaining': {k: [] for k in KINDS}}
        time.sleep(3)
    raise RuntimeError('Resource deletion unconfirmed after 120s; inspect labeled resources')


def execution_mode(config):
    name = config["agents"][0].get("name")
    return name if name in ("oracle", "nop") else "llm"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--harbor', help='Exact Harbor executable (required unless --cleanup-only)')
    parser.add_argument('--out', required=True)
    parser.add_argument('--cleanup-only', action='store_true')
    parser.add_argument('--expected-region', default='cn-beijing')
    args = parser.parse_args()
    path = Path(args.config).resolve()
    c = json.loads(path.read_text())
    run_id, kw = check_config(c)
    # Callers set TMPDIR inside the workspace before this script starts.
    if not os.environ.get('TMPDIR') or not Path(os.environ['TMPDIR']).is_dir():
        raise ValueError('Set TMPDIR to a pre-created workspace-local directory')
    tempfile.tempdir = os.environ['TMPDIR']
    cluster = Cluster(kw['kubeconfig'], kw['namespace'])
    nodes = cluster.call('/api/v1/nodes')
    regions = {n['metadata'].get('labels', {}).get('topology.kubernetes.io/region') for n in nodes['items']}
    if args.expected_region not in regions or any(r != args.expected_region for r in regions):
        raise ValueError('Cluster region does not match --expected-region')
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {'run_id': run_id, 'mode': execution_mode(c),
              'cleanup': {'confirmed': False}, 'harbor_exit_code': None,
              'execution_identity': execution_identity(c), 'pod_observations': {}}
    journal=out.with_name(out.stem+'.pod-evidence')
    journal.mkdir(mode=0o700,exist_ok=True)
    known = {}
    # Recover names seen before a previous interruption; never infer arbitrary names.
    if out.exists():
        previous = json.loads(out.read_text())
        if previous.get('run_id') != run_id:
            raise ValueError('Output report belongs to another run')
        known = {(x['kind'], x['name']): x['uid'] for x in previous.get('known_resources', [])}
        if previous.get('execution_identity') == report['execution_identity']:
            report['pod_observations'] = previous.get('pod_observations', {})

    if not args.cleanup_only:
        # Reject collisions before entering a cleanup scope. A concurrent run
        # with the same label must never be killed as a side effect of refusal.
        if (Path(c['jobs_dir']) / run_id).exists() or any(cluster.snapshot(run_id).values()):
            raise ValueError('Use a fresh unique run-id; existing run left untouched')

    def save():
        try:
            merge_captured(report,journal,known)
        except (OSError,ValueError,TypeError,KeyError) as error:
            report['pod_capture_error_type']=type(error).__name__
        report['known_resources'] = [{'kind': k, 'name': n, 'uid': u} for (k, n), u in known.items()]
        report['pod_evidence'] = validate_pod_evidence(report, c)
        temporary = out.with_suffix(out.suffix + '.writing')
        with temporary.open('w') as stream:
            temporary.chmod(0o600)
            json.dump(report, stream, indent=2)
        temporary.replace(out)

    proc = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        # Observe stale peers, but never delete another run as a side effect of
        # starting this one. release() below handles this run's exact identities.
        watchdog_snapshot(kw['kubeconfig'], kw['namespace'], out.with_name(out.stem + '.watchdog-start.json'), kill=False)
        if not args.cleanup_only:
            if not args.harbor:
                raise ValueError('--harbor required')
            job_dir = Path(c['jobs_dir']) / run_id
            if report['mode'] == 'llm':
                from provider_probe import settings
                settings_value = settings()
                agent = c['agents'][0]
                model = agent.get('model_name', '').removeprefix('openai/')
                if model != settings_value['OPENAI_MODEL'] or agent['env']['OPENAI_BASE_URL'].rstrip('/') != settings_value['OPENAI_BASE_URL'].rstrip('/') or agent['env']['LLM_PROVIDER'] != settings_value['LLM_PROVIDER']:
                    raise ValueError('Loaded provider/model settings do not match job configuration')
            budget = budgets(c)
            if kw['pod_overrides']['spec']['activeDeadlineSeconds'] < budget['pod_seconds']:
                raise ValueError('Pod lifetime must include setup, verifier and evidence collection')
            report['execution_budget'] = budget
            with out.with_suffix('.harbor.log').open('w') as log:
                proc = subprocess.Popen([str(Path(args.harbor).resolve()), 'run', '--config', str(path), '--yes'],
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                        env=dict(os.environ,GEWU_POD_EVIDENCE_DIR=str(journal)))
                deadline = time.monotonic() + budget['controller_seconds']
                while proc.poll() is None:
                    snapshot = cluster.snapshot(run_id)
                    remember(snapshot, known)
                    observe(snapshot, report)
                    save()
                    if time.monotonic() > deadline:
                        raise TimeoutError('Harbor controller exceeded its execution and collection budget')
                    time.sleep(1)
                report['harbor_exit_code'] = proc.returncode
            result_path = job_dir / 'result.json'
            if result_path.exists():
                result = json.loads(result_path.read_text())
                report['result_path'] = str(result_path)
                report['finished_at'] = result.get('finished_at')
                report['stats'] = result.get('stats', {})
                report['result_available'] = bool(result.get('finished_at'))
    except (Exception, KeyboardInterrupt) as exc:
        # Avoid raw SDK exception bodies that may echo credentials.
        report['error_type'] = type(exc).__name__
        if isinstance(exc, (ValueError, TimeoutError)):
            report['error'] = str(exc)
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        try:
            save()
            report['cleanup'] = release(cluster, run_id, known, report)
        except Exception as exc:
            report['cleanup'] = {'confirmed': False, 'error_type': type(exc).__name__}
        watchdog_snapshot(kw['kubeconfig'], kw['namespace'], out.with_name(out.stem + '.watchdog-end.json'), kill=False)
        save()
    print(json.dumps(report, indent=2))
    successful = (args.cleanup_only or (report.get('result_available') and report['harbor_exit_code'] == 0))
    return 0 if successful and report['cleanup']['confirmed'] and 'error_type' not in report else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Preflight can fail before a report is created. Never dump SDK bodies.
        print(json.dumps({'cleanup': {'confirmed': False},
                          'error_type': type(exc).__name__,
                          'stage': 'preflight_or_report_write'}), file=sys.stderr)
        raise SystemExit(1)
