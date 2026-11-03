"""Public upload adapter to the maintained immutable evaluation queue."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
import secrets as token_secrets

from evaluation_queue import W, atomic_json, digest_tree, validated_template
from check_harbor_task import inspect_task
from author_selftest_worker import build_command
from build_task_images import validate_build_receipt, build_failure_details
from build_command_logs import run_logged
from harbor_public_artifacts import build_diagnostics, publish_artifacts
from codex_evaluation_template import prepare
from publish_evidence import redact
from selftest_logs import credential_values, sanitize
from harbor_public_policy import validate_task

PYTHON = W / 'acs-harbor-test/.venv/bin/python'
HERE = Path(__file__).resolve().parent


def execute(root, settings):
    root = Path(root).resolve()
    request = json.loads((root / 'request.json').read_text())
    public = dict(run_id=request['run_id'], status='validating', agent='codex',
                  model='deepseek-v4.1-flash-1017', provider='dawnfm',
                  requested_trials=request['attempts'], trials=[],
                  uploaded_task_sha256=request['uploaded_task_sha256'],
                  reference_placeholder=request['reference_placeholder'],
                  task_sha256=request['task_sha256'], created_at=request['created_at'])
    secrets = credential_values(W)

    def save(**changes):
        public.update(changes, updated_at=time.time())
        atomic_json(root / 'public.json', redact(public, secrets))

    try:
        save()
        repo = root / 'source'
        task = repo / request['task_name']
        validate_task(task)
        base = json.loads(Path(settings['base_template']).read_text())
        mechanical = inspect_task(task, acs_environment=base['environment'])
        atomic_json(root / 'mechanical-check.json', mechanical)
        if digest_tree(task) != request['task_sha256']:
            raise ValueError('Uploaded source changed before build')
        for args in (['init', '-q'], ['config', 'user.email', 'harbor-service@localhost'],
                     ['config', 'user.name', 'Harbor Service'], ['add', '.'],
                     ['commit', '-qm', 'Immutable public Harbor upload']):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        save(status='building')
        build_env = dict(os.environ)
        build_env.setdefault('GEWU_DOCKER_LIBRARY_MIRROR', 'public.ecr.aws/docker/library')
        built = run_logged(build_command(repo, commit, task.name, root / 'build'), root/'build',
            env=build_env, timeout=7200, details={'official_library_mirror':build_env['GEWU_DOCKER_LIBRARY_MIRROR']})
        built.check_returncode()
        receipt = json.loads((root / 'build/receipt.json').read_text())
        template = prepare(base, receipt)
        lease = dict(token=token_secrets.token_urlsafe(48), expires_at=time.time()+15000,
                     base_url=settings['public_url'].rstrip('/')+'/model/v1',
                     model='deepseek-v4.1-flash-1017', max_requests=1000*request['attempts'], revoked=False)
        atomic_json(root/'model-lease.json',lease)
        secrets.append(lease['token'])
        template['agents'][0]['import_path']='public_codex:PublicCodex'
        template['agents'][0]['kwargs']['public_model_lease']=str(root/'model-lease.json')
        template['datasets'] = [{'path': str(repo), 'task_names': [task.name]}]
        # Keep task verifier timing rather than the old smoke-test override.
        template['verifier'].pop('override_timeout_sec', None)
        validate_build_receipt(receipt, task, commit, task.name, template)
        template_path = root / 'template.json'
        atomic_json(template_path, template)
        entry = dict(snapshot=str(task), snapshot_sha256=digest_tree(task),
                     template=str(template_path), template_sha256=hashlib.sha256(template_path.read_bytes()).hexdigest(),
                     source_commit=commit, source_task_path=task.name,
                     build_receipt_sha256=hashlib.sha256((root / 'build/receipt.json').read_bytes()).hexdigest())
        validated_template(entry, {'snapshot_sha256': entry['snapshot_sha256']})
        atomic_json(root / 'provenance.json', entry)
        atomic_json(root / 'registry.json', {request['run_id']: entry})
        for name in ('inbox', 'results', 'queue-state'):
            (root / name).mkdir(mode=0o700)
        uid = 21990
        os.chown(root / 'inbox', uid, uid)
        queue = dict(inbox=str(root / 'inbox'), results=str(root / 'results'),
                     state=str(root / 'queue-state'), registry=str(root / 'registry.json'),
                     evaluator_uid=uid, max_jobs_per_day=request['attempts'])
        atomic_json(root / 'queue.json', queue)
        ids = [uuid.uuid4().hex for _ in range(request['attempts'])]
        for jid in ids:
            path = root / 'inbox' / (jid + '.json')
            atomic_json(path, dict(job_id=jid, evaluation_id=request['run_id'], snapshot_sha256=entry['snapshot_sha256']))
            os.chown(path, uid, uid)
        atomic_json(root / 'trial-ids.json', ids)
        save(status='evaluating', source_commit=commit,
             images={name: receipt['images'][name]['published_digest'] for name in ('runtime', 'verifier')},
             harness_version=receipt['harness_versions']['codex'])

        def one(jid):
            with (root / (jid + '.worker.log')).open('w') as log:
                command = [str(PYTHON), str(HERE / 'evaluation_queue.py'), '--config', str(root / 'queue.json'),
                           '--single-request', str(root / 'inbox' / (jid + '.json')), '--queue-index', '0']
                return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                    env=dict(os.environ,PUBLIC_MODEL_ACCESS_TOKEN=lease['token'])).returncode

        with concurrent.futures.ThreadPoolExecutor(max_workers=request['attempts']) as pool:
            futures = {pool.submit(one, jid): jid for jid in ids}
            for future in concurrent.futures.as_completed(futures):
                jid = futures[future]
                rc = future.result()
                result = root / 'results' / (jid + '.json')
                row = json.loads(result.read_text()) if result.exists() else {'status': 'worker_failed'}
                public['trials'].append(dict(trial_id=jid, worker_exit_code=rc,
                    **{k: row.get(k) for k in ('status', 'reward', 'rewards', 'cleanup_confirmed', 'diagnostics',
                                             'input_tokens', 'output_tokens', 'sanitized_trajectory_exported')}))
                save()
        unchanged = digest_tree(task) == entry['snapshot_sha256']
        scored = [t for t in public['trials'] if t['reward'] is not None]
        valid = [t for t in public['trials'] if t.get('rewards') is not None]
        clean = all(t['cleanup_confirmed'] is True for t in public['trials'])
        complete = (len(public['trials']) == request['attempts'] and clean and unchanged
                    and all(t['status'] == 'solver_complete' for t in public['trials']))
        public.update(status='completed' if complete else 'failed', cleanup_confirmed=clean, task_unchanged=unchanged,
             summary=dict(returned_trials=len(public['trials']), scored_trials=len(valid),
                          passed_trials=sum(t['reward'] == 1 for t in scored),
                          mean_reward=sum(t['reward'] for t in scored)/len(scored) if scored else None),
             scientific_acceptance=False)
        save(**publish_artifacts(root, public, secrets))
    except Exception as error:
        public.update(status='failed', error_type=type(error).__name__,
             error=sanitize(str(error), secrets).replace(str(W), '[workspace]')[:1500])
        public.update(build_failure_details(root/'build'))
        public['build_diagnostics'] = build_diagnostics(root, secrets)
        try:
            public.update(publish_artifacts(root, public, secrets))
        except Exception as publication_error:
            public['artifact_error'] = sanitize(str(publication_error), secrets)[:500]
        save()
        raise
    finally:
        path=root/'model-lease.json'
        if path.exists():
            lease=json.loads(path.read_text());lease['revoked']=True
            atomic_json(path,lease)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--settings', required=True, type=Path)
    args = parser.parse_args()
    execute(args.run, json.loads(args.settings.read_text()))
