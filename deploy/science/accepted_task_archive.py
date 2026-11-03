#!/usr/bin/env python3
"""Private, cross-universe archives of formally accepted Harbor task versions.

Only reads production data. Publishes complete, verified directories atomically;
never changes reviews, reruns ACS, copies credentials, or follows source links.
All operational files live below the workspace. Standard-library only.
"""
import argparse
import collections
import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

WORKSPACE = Path('/mnt/data/zhanglinfeng/workspace')
DEFAULT_ROOT = WORKSPACE / 'accepted-tasks'
SECRET_KEY = re.compile(r'(?i)(?:api[_-]?key|password|authorization|access[_-]?token|private[_-]?key|client[_-]?secret|credential)')
TOKEN_PATTERN = re.compile(r'(?i)(Bearer\s+)[^\s,\"\']+')
PEM_PATTERN = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S)
ASSIGN_PATTERN = re.compile(r'''(?i)((?:OPENAI_API_KEY|ANTHROPIC_API_KEY|api_key|access_token|password)\s*["']?\s*[:=]\s*["']?)([^\s,"'}]+)''')
REQUIRED = ('instruction.md', 'task.toml', 'environment/Dockerfile', 'tests/test.sh', 'solution/solve.sh')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def local(path):
    path = Path(path).absolute()
    require(path.is_relative_to(WORKSPACE) and path != WORKSPACE, 'Path must remain below workspace')
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'Symlink path refused')
    return path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    path = local(path)
    require(stat.S_ISREG(path.stat().st_mode), 'Regular evidence file required')
    return path.read_bytes()


def load(path):
    return json.loads(read(path))


def mkdir(path):
    path = local(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic(path, data):
    path = local(path); mkdir(path.parent)
    raw = data if isinstance(data, bytes) else (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    fd, tmp = tempfile.mkstemp(prefix='.archive-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600); f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(root):
    mkdir(root)
    with (root / '.archive.lock').open('a') as f:
        os.fchmod(f.fileno(), 0o600); fcntl.flock(f, fcntl.LOCK_EX)
        yield


def files(root):
    root = local(root)
    require(root.is_dir(), 'Missing source directory')
    for p in sorted(root.rglob('*')):
        info = p.lstat()
        require(not p.is_symlink() and (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)), 'Special evidence entry refused')
        if p.is_file():
            yield p


def tree_hash(root):
    h = hashlib.sha256()
    for p in files(root):
        data = read(p); name = p.relative_to(root).as_posix().encode()
        h.update(len(name).to_bytes(8, 'big') + name)
        h.update(len(data).to_bytes(8, 'big')); h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def accepted(row):
    return row.get('state') == 'completed' and row.get('formal_decision') == 'accepted' and row.get('scientific_acceptance') is True


def discover_secrets(value):
    result = set()
    if isinstance(value, dict):
        for key, val in value.items():
            if (SECRET_KEY.search(key) and isinstance(val, str) and len(val) >= 12
                    and not val.startswith(('[REDACTED', '$', '/'))
                    and not key.endswith('_ENV') and val not in ('OPENAI_API_KEY','ANTHROPIC_API_KEY')):
                result.add(val)
            else:
                result.update(discover_secrets(val))
    elif isinstance(value, list):
        for val in value: result.update(discover_secrets(val))
    return result


def scrub_text(text, secrets):
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = TOKEN_PATTERN.sub(lambda m: m[1] + '[REDACTED]', text)
    text = PEM_PATTERN.sub('[REDACTED PRIVATE KEY]', text)
    return ASSIGN_PATTERN.sub(lambda m: m[1] + '[REDACTED]', text)


def scrub(value, secrets):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if SECRET_KEY.search(k) and v else scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list): return [scrub(v, secrets) for v in value]
    if isinstance(value, str): return scrub_text(value, secrets)
    return value


def sanitize_bytes(data, suffix, secrets):
    try: text = data.decode('utf-8')
    except UnicodeError:
        require(not any(s.encode() in data for s in secrets), 'Credential in binary evidence; archive refused')
        return data
    if suffix == '.json':
        try:
            obj = json.loads(text); cleaned = scrub(obj, secrets)
            return data if obj == cleaned else (json.dumps(cleaned, ensure_ascii=False, indent=2) + '\n').encode()
        except json.JSONDecodeError: pass
    if suffix == '.jsonl':
        out = []
        for line in text.splitlines(keepends=True):
            try:
                obj = json.loads(line); cleaned = scrub(obj, secrets)
                out.append(line if obj == cleaned else json.dumps(cleaned, ensure_ascii=False) + '\n')
            except json.JSONDecodeError: out.append(scrub_text(line, secrets))
        return ''.join(out).encode()
    return scrub_text(text, secrets).encode()


class Bundle:
    def __init__(self, root, secrets):
        self.root = root; self.secrets = secrets; self.entries = {}

    def put(self, relative, data, source=None, original=None, executable=False):
        target = local(self.root / relative)
        require(target.is_relative_to(self.root), 'Archive path escaped bundle')
        mkdir(target.parent)
        with target.open('xb') as f:
            os.fchmod(f.fileno(), 0o700 if executable else 0o600); f.write(data); f.flush(); os.fsync(f.fileno())
        self.entries[str(Path(relative))] = dict(bytes=len(data), sha256=sha(data), executable=executable,
            source=str(source) if source else None, source_sha256=sha(original) if original is not None else None,
            credential_redacted=original is not None and original != data)

    def document(self, relative, value):
        self.put(relative, (json.dumps(scrub(value, self.secrets), ensure_ascii=False, indent=2) + '\n').encode())

    def copy(self, src, relative, exact=False):
        raw = read(src); data = sanitize_bytes(raw, Path(src).suffix, self.secrets)
        if exact:
            # Task bytes and acceptance receipts must not be silently changed.
            require(not any(x.encode() in raw for x in self.secrets), 'Credential in immutable input; archive refused')
            data = raw
        self.put(relative, data, src, raw, bool(Path(src).stat().st_mode & 0o111))

    def copy_tree(self, src, relative, exact=False):
        for p in files(src): self.copy(p, Path(relative) / p.relative_to(src), exact)


def validate_decision(row, receipt, raw):
    require(accepted(row), 'Only formally accepted versions can be archived')
    require(receipt.get('decision') == 'accepted', 'Missing formal acceptance')
    for k in ('key', 'commit', 'task', 'snapshot_sha256', 'evaluation_id', 'role', 'generation'):
        require(receipt.get(k) == row.get(k), 'Acceptance identity mismatch: ' + k)
    require(receipt.get('review_reason_sha256') == sha(row.get('reason', '').encode()), 'Review reason hash mismatch')
    require(row.get('formal_decision_sha256') == sha(raw), 'Decision file hash mismatch')


def select_jobs(state, row, receipt):
    matches = []
    for p in sorted((state / 'evaluation-queue').glob('*/result.json')):
        result = load(p)
        if result.get('evaluation_id') == row['evaluation_id'] and result.get('snapshot_sha256') == row['snapshot_sha256']:
            require(result.get('job_id') == p.parent.name, 'Job identity mismatch')
            matches.append((p.parent, result))
    explicit = [x.get('job_id') for x in receipt.get('verified_evidence', {}).get('trials', [])]
    if explicit:
        require(len(explicit) == 5 and len(set(explicit)) == 5 and all(explicit), 'Acceptance must select five unique jobs')
        selected = [x for x in matches if x[1]['job_id'] in explicit]
        basis = 'formal_acceptance_verified_evidence'
    else:
        selected = [x for x in matches if x[1].get('status') == 'solver_complete' and x[1].get('reward') in (0, 1) and x[1].get('n_errors') == 0]
        basis = 'legacy_operator_acceptance_exactly_five_scored_jobs_for_same_evaluation_and_snapshot'
    require(len(selected) == 5, 'Cannot identify exactly five accepted-version scored runs; no cherry-picking')
    return selected, [x for x in matches if x not in selected], basis


def collect_comments(universe, row):
    database = universe / '.local/share/gewu-multiverse/forgejo/instance/data/forgejo.db'
    if not database.exists(): return None, [], 'forgejo_database_unavailable'
    con = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True); con.row_factory = sqlite3.Row
    try:
        issue = con.execute('SELECT i.id,i.[index],i.name,i.content,i.is_closed,i.is_pull,r.owner_name,r.name AS repository FROM issue i JOIN repository r ON r.id=i.repo_id WHERE r.owner_name=? AND r.name=? AND i.[index]=?',
                            ('team', 'terminal-bench-science', row.get('proposal_thread'))).fetchone()
        if issue is None: return None, [], 'original_proposal_not_found'
        comments = [dict(x) for x in con.execute('SELECT c.id,c.type,c.content,c.created_unix,c.updated_unix,u.name AS author FROM comment c LEFT JOIN user u ON u.id=c.poster_id WHERE c.issue_id=? ORDER BY c.id', (issue['id'],))]
        return dict(issue), comments, None
    finally: con.close()


def validate_trial(work, result, row):
    report = load(work / 'report.json'); cfg = load(work / 'config.json')
    run_id = report.get('run_id')
    require(isinstance(run_id, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', run_id), 'Invalid run ID')
    trials = list((work / 'runs' / run_id).glob('*/result.json'))
    require(len(trials) == 1, 'Expected one independent trial per job')
    trial_file = trials[0]; trial = load(trial_file); trial_dir = trial_file.parent
    trial_cfg = load(trial_dir / 'config.json')
    require(trial_cfg.get('task', {}).get('source') == row['evaluation_id'], 'Trial source identity mismatch')
    require(trial.get('exception_info') is None and result.get('n_errors') == 0, 'Scored run contains infrastructure exception')
    reward = trial.get('verifier_result', {}).get('rewards', {}).get('reward')
    require(type(reward) in (int, float) and reward in (0, 1) and reward == result.get('reward'), 'Native trial reward mismatch')
    verifier = trial_dir / 'verifier' / 'reward.txt'
    if verifier.exists(): require(float(read(verifier)) == reward, 'Verifier reward file mismatch')
    require(result.get('cleanup_confirmed') is True and report.get('cleanup', {}).get('confirmed') is True, 'Cleanup not confirmed')
    require(cfg.get('n_attempts') == 1 and cfg.get('n_concurrent_trials') == 1, 'Expected independent single-attempt job')
    trajectories = [p for p in (trial_dir / 'agent').glob('*trajectory*.json') if p.is_file()]
    sessions = list((trial_dir / 'agent' / 'sessions').rglob('*.jsonl'))
    require(trajectories and sessions, 'Missing trajectory or native agent session; archive pending')
    require(all(p.stat().st_size > 0 for p in trajectories + sessions), 'Empty trajectory/session')
    for p in trajectories:
        t = load(p); require(t.get('steps') or t.get('messages'), 'Trajectory has no steps')
    env = trial_cfg.get('environment', {}); kwargs = env.get('kwargs', {})
    missing = list(result.get('missing_evidence', []))
    return dict(job_id=result['job_id'], run_id=run_id, trial_id=trial.get('id'), trial_name=trial.get('trial_name'),
        reward=reward, passed=reward == 1, provider=result.get('provider'), model=result.get('model'),
        started_at=trial.get('started_at'), finished_at=trial.get('finished_at'),
        input_tokens=result.get('input_tokens'), output_tokens=result.get('output_tokens'),
        cleanup_confirmed=True, n_errors=0, evidence_complete=result.get('evidence_complete') is True,
        missing_evidence=missing, resource_request={'cpus': env.get('override_cpus'), 'memory_mb': env.get('override_memory_mb'), 'storage_mb': env.get('override_storage_mb')},
        resource_observation_note='Requested configuration is not actual Pod observation.',
        runtime_image=kwargs.get('runtime_image'), verifier_image=kwargs.get('verifier_image'),
        trajectory_paths=[str(Path('acs') / result['job_id'] / 'native' / p.relative_to(work)) for p in trajectories],
        session_paths=[str(Path('acs') / result['job_id'] / 'native' / p.relative_to(work)) for p in sessions],
        result_path=str(Path('acs') / result['job_id'] / 'native' / trial_file.relative_to(work))), cfg


def archive_one(root, universe, source_id, row):
    state = universe / '.state/gewu-multiverse'
    receipt_path = state / 'review-dispatcher/formal-decisions' / (sha(row['key'].encode()) + '.json')
    raw_receipt = read(receipt_path); receipt = json.loads(raw_receipt); validate_decision(row, receipt, raw_receipt)
    require(re.fullmatch('[a-f0-9]{40}', row['commit']) and re.fullmatch('[a-f0-9]{64}', row['snapshot_sha256']), 'Invalid immutable version')
    slug = Path(row['task']).name
    require(re.fullmatch('[A-Za-z0-9_.-]+', slug) and slug not in ('.', '..'), 'Invalid task name')
    archive_id = sha((source_id + ':' + row['key'] + ':' + sha(raw_receipt)).encode())
    rel = Path('tasks') / slug / row['snapshot_sha256'] / archive_id
    destination = root / rel
    if destination.exists():
        info = verify_bundle(destination)
        require(info['archive_id'] == archive_id, 'Existing archive identity differs')
        return dict(path=str(rel), metadata=info, already_present=True)
    registry = load(universe / '.config/science-evaluation/registry.json')
    entry = registry[row['evaluation_id']]; snapshot = local(entry['snapshot'])
    require(entry['source_commit'] == row['commit'] and entry['source_task_path'] == row['task'], 'Registered source differs')
    require(tree_hash(snapshot) == row['snapshot_sha256'], 'Frozen task snapshot hash mismatch')
    require(all((snapshot / p).is_file() for p in REQUIRED), 'Harbor task files missing')
    jobs, excluded, selection = select_jobs(state, row, receipt)
    trials = []; secrets = set()
    for work, result in jobs:
        trial, cfg = validate_trial(work, result, row); trials.append(trial); secrets.update(discover_secrets(cfg))
    require(len({t['run_id'] for t in trials}) == 5 and len({t['trial_id'] for t in trials}) == 5, 'Independent run identity duplicated')
    require(len({(t['provider'], t['model']) for t in trials}) == 1, 'Mixed model batch')
    stage = mkdir(root / '.staging' / (archive_id + '-' + str(os.getpid())))
    require(not any(stage.iterdir()), 'Staging directory already populated')
    bundle = Bundle(stage, secrets)
    try:
        bundle.copy_tree(snapshot, 'task', exact=True)
        bundle.copy(receipt_path, 'acceptance/decision.json', exact=True)
        bundle.document('acceptance/ledger-entry.json', row)
        bundle.document('registration/registry-entry.json', entry)
        for name in ('provenance.json', 'mechanical-check.json', 'build-receipt.json', 'selftest-binding.json', 'template.json'):
            p = snapshot.parent / name
            if p.exists(): bundle.copy(p, 'registration/' + name)
        for work, result in jobs:
            bundle.copy_tree(work, Path('acs') / result['job_id'] / 'native')
            packet = universe / '.config/gewu-multiverse/agents' / row['role'] / 'evaluation/evidence' / result['job_id']
            if packet.is_dir(): bundle.copy_tree(packet, Path('acs') / result['job_id'] / 'evaluator-packet')
        excluded_records = [dict(job_id=x['job_id'], status=x.get('status'), reward=x.get('reward'), counted=False) for _, x in excluded]
        bundle.document('acs/excluded-attempts.json', excluded_records)
        for work, result in excluded:
            bundle.copy(work / 'result.json', Path('acs/excluded-results') / (result['job_id'] + '.json'))
        selftest = state / 'author-selftest' / str(row.get('job_id'))
        for name in ('status.json', 'request.json', 'oracle-report.json', 'nop-report.json', 'mechanical.json', 'handoff-qualified.json', 'handoff-evidence.json', 'handoff-clock.json'):
            if (selftest / name).is_file(): bundle.copy(selftest / name, 'selftest/' + name)
        issue, comments, discussion_gap = collect_comments(universe, row)
        bundle.document('review/proposal.json', issue)
        bundle.document('review/discussion.json', comments)
        lessons = []
        for comment in comments:
            if comment.get('author') == 'gewu-' + row['author_role'] and 'gewu-post-acceptance-lessons-v1' in comment.get('content', '') and row['commit'] in comment['content']:
                name = 'lessons/comment-' + str(comment['id']) + '.md'
                content = scrub_text(comment['content'], secrets).encode(); bundle.put(name, content)
                lessons.append(dict(id=comment['id'], path=name, created_unix=comment['created_unix'], sha256=sha(content)))
        bundle.document('lessons/index.json', dict(recorded_comment=row.get('lessons_comment'), captured_comments=lessons, note='Includes historical repetitions and supplements; original timestamps retained.'))
        passed = sum(t['passed'] for t in trials); failed = 5 - passed
        gaps = sorted({g for t in trials for g in t['missing_evidence']})
        if discussion_gap: gaps.append(discussion_gap)
        if not lessons: gaps.append('author_lessons_unavailable_at_archive_time')
        metadata = dict(schema_version=1, archive_id=archive_id, archived_at=now(),
            source_universe=dict(id=source_id, root=str(universe)),
            task=dict(name=slug, path=row['task'], commit=row['commit'], snapshot_sha256=row['snapshot_sha256'], harbor_directory='task'),
            author=row['author_role'], evaluator=row['role'], evaluation_id=row['evaluation_id'],
            acceptance=dict(status='accepted', scope=row.get('acceptance_scope'), decision_path='acceptance/decision.json',
                operator=receipt.get('operator'), rationale=receipt.get('rationale'), historical_override=not bool(receipt.get('verified_evidence'))),
            statistics=dict(denominator='Five scored solver-complete jobs bound to the accepted evaluation ID and task snapshot; cancelled attempts excluded. Historical acceptance retains missing Pod evidence.',
                selected_trials=5, passed=passed, failed=failed, pass_rate=passed/5, pass_rate_percent=passed*20,
                model=trials[0]['model'], provider=trials[0]['provider'], input_tokens=sum(t['input_tokens'] or 0 for t in trials),
                output_tokens=sum(t['output_tokens'] or 0 for t in trials), strict_evidence_complete_trials=sum(t['evidence_complete'] for t in trials),
                strict_evidence_complete_pass_rate=None if not all(t['evidence_complete'] for t in trials) else passed/5,
                excluded_attempts=len(excluded), selection_basis=selection),
            trials=trials, evidence=dict(trajectory_count=5, native_sessions_count=sum(len(t['session_paths']) for t in trials),
                complete=not gaps and all(t['evidence_complete'] for t in trials), missing=gaps,
                retention='Full original-content logs and native sessions, with credential fields/values redacted when present. Source and archived hashes recorded per file.',
                known_limitations='The original task and discussion retain scientific-contract limitations. Formal acceptance is not a claim that all defects or historical evidence gaps were resolved.'),
            portability=dict(no_symlinks=True, all_required_payload_files_local=True, original_absolute_paths_are_provenance_only=True,
                container_images='Immutable image references and build receipts retained; OCI image layers are not embedded.',
                external_data='The exact committed Harbor task is retained; upstream datasets absent from the task are not downloaded.'),
            lessons=lessons, storage=dict(private=True, separate_from_universe_runtime=True, offsite_backup=False))
        bundle.document('metadata.json', metadata)
        buffer = io.StringIO(); writer = csv.writer(buffer); writer.writerow(['job_id','run_id','reward','passed','model','cleanup_confirmed','evidence_complete','missing_evidence'])
        for t in trials: writer.writerow([t['job_id'],t['run_id'],t['reward'],t['passed'],t['model'],t['cleanup_confirmed'],t['evidence_complete'],';'.join(t['missing_evidence'])])
        bundle.put('statistics.csv', buffer.getvalue().encode())
        readme = '\n'.join(['# ' + slug, '', '正式接受的冻结版本归档；不修改原题。', '',
            'Commit: ' + row['commit'], 'Snapshot SHA256: ' + row['snapshot_sha256'],
            '五次 ACS 求解：%s/5 通过，正确率 %s%%。' % (passed, passed*20), '',
            'task/：标准 Harbor 题包。acs/<job_id>/native/：完整轨迹、原生会话、result、verifier、运行配置与清理证据。',
            'acceptance/：接受回执。registration/：版本、镜像和构建绑定。review/：原PR及讨论。lessons/：作者经验帖。',
            'metadata.json、statistics.csv、manifest.json：元数据、正确率和逐文件校验。', '',
            '历史缺口：' + (', '.join(gaps) or '详见 metadata.json'),
            '该历史版本的实际Pod资源观测不能由请求配置替代。接受不意味着已知科学契约问题消失。',
            '日志按凭据字段和值脱敏；没有省略求解步骤。原始来源路径只用于溯源，读取归档不依赖原宇宙。', ''])
        bundle.put('README.md', readme.encode())
        for p in files(stage):
            require(not any(secret.encode() in read(p) for secret in secrets), 'Credential remains after archive redaction')
        atomic(stage / 'manifest.json', dict(schema_version=1, task_snapshot_sha256=row['snapshot_sha256'], files=bundle.entries))
        verify_bundle(stage)
        require(tree_hash(snapshot) == row['snapshot_sha256'], 'Source snapshot changed during archival')
        mkdir(destination.parent); os.replace(stage, destination)
        fd = os.open(destination.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return dict(path=str(rel), metadata=metadata, already_present=False)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_bundle(directory):
    directory = local(directory); manifest = load(directory / 'manifest.json'); metadata = load(directory / 'metadata.json')
    actual = {str(p.relative_to(directory)) for p in files(directory)}
    require(actual == set(manifest['files']) | {'manifest.json'}, 'Archive has missing or extra files')
    for rel, record in manifest['files'].items():
        p = local(directory / rel); require(p.is_relative_to(directory), 'Manifest path escaped archive')
        data = read(p); require(len(data) == record['bytes'] and sha(data) == record['sha256'], 'Archive checksum mismatch: ' + rel)
        require(bool(p.stat().st_mode & 0o111) == record['executable'], 'Archive executable mode mismatch')
    require(tree_hash(directory / 'task') == metadata['task']['snapshot_sha256'] == manifest['task_snapshot_sha256'], 'Archived Harbor task differs')
    require(all((directory/'task'/p).is_file() for p in REQUIRED), 'Missing Harbor task file')
    require(len(metadata['trials']) == 5, 'Archive must have five trials')
    for trial in metadata['trials']:
        require(trial['trajectory_paths'] and trial['session_paths'], 'Missing archived trajectory')
        for rel in trial['trajectory_paths'] + trial['session_paths']:
            require(rel in manifest['files'] and (directory/rel).stat().st_size > 0, 'Missing trajectory payload')
    require(sum(t['passed'] for t in metadata['trials']) == metadata['statistics']['passed'], 'Statistics mismatch')
    return metadata


def register(root, universe, source_id=None):
    root = local(root); universe = local(universe)
    require(not root.is_relative_to(universe) and not universe.is_relative_to(root), 'Archive must be independent of universe')
    ledger=universe/'.state/gewu-multiverse/review-dispatcher/ledger.json'
    if not ledger.is_file():
        # A fresh universe can register before its first dispatcher poll. The
        # provisioned identity is enough to subscribe; sync still requires a
        # real ledger and formal acceptance evidence before publishing tasks.
        marker=universe/'.state/gewu-multiverse/deployment.json'
        require(marker.is_file(), 'Universe review ledger missing')
        deployment=load(marker)
        require(deployment.get('status')=='service_config_prepared'
                and Path(deployment.get('universe_root','')).resolve()==universe,
                'Universe deployment identity missing or mismatched')
    source_id = source_id or universe.name + '-' + sha(str(universe).encode())[:12]
    require(re.fullmatch('[A-Za-z0-9_.-]+', source_id) and source_id not in ('.','..'), 'Invalid source ID')
    with locked(root):
        p=root/'sources.json'; config=load(p) if p.exists() else dict(schema_version=1,sources=[])
        same=[x for x in config['sources'] if x['id']==source_id or x['universe_root']==str(universe)]
        if same: require(len(same)==1 and same[0]['id']==source_id and same[0]['universe_root']==str(universe), 'Source identity conflict')
        else: config['sources'].append(dict(id=source_id,universe_root=str(universe),registered_at=now()))
        atomic(p,config)
    return source_id


def sync(root):
    root=local(root); completed=[]; errors=[]
    with locked(root):
        sources=load(root/'sources.json')['sources']; index_path=root/'index.json'
        index=load(index_path) if index_path.exists() else dict(schema_version=1,entries=[])
        known={e['archive_id']:e for e in index['entries']}
        for source in sources:
            universe=local(source['universe_root'])
            try: ledger=load(universe/'.state/gewu-multiverse/review-dispatcher/ledger.json')
            except (OSError,ValueError):
                errors.append(dict(universe_id=source['id'],error='Source ledger unavailable')); continue
            for key,row in ledger.items():
                if not accepted(row):continue
                row=dict(row,key=key)
                try:
                    exported=archive_one(root,universe,source['id'],row);meta=exported['metadata']
                    known[meta['archive_id']]=dict(archive_id=meta['archive_id'],path=exported['path'],task=meta['task'],
                        author=meta['author'],universe_id=source['id'],accepted=True,archived_at=meta['archived_at'],
                        statistics=meta['statistics'],evidence_complete=meta['evidence']['complete'],missing_evidence=meta['evidence']['missing'])
                    completed.append(dict(archive_id=meta['archive_id'],already_present=exported['already_present']))
                except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
                    # No source contents or exception detail that could include credentials.
                    errors.append(dict(universe_id=source['id'],task=row['task'],commit=row['commit'],error_type=type(error).__name__,
                        error=str(error) if isinstance(error,ValueError) else 'Archive pending; inspect source integrity or availability'))
        index.update(updated_at=now(),entries=sorted(known.values(),key=lambda e:(e['task']['name'],e['archive_id'])))
        atomic(index_path,index)
        report=dict(observed_at=now(),accepted_archives=len(known),processed=completed,pending=errors)
        atomic(root/'sync-status.json',report)
        return report


def watch(root, interval):
    root=mkdir(root)
    with (root/'.watch.lock').open('a') as lock:
        os.fchmod(lock.fileno(),0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Archive watcher already running')
        atomic(root/'watcher.json',dict(pid=os.getpid(),started_at=now(),poll_seconds=interval,
            program=str(Path(__file__).resolve()),program_sha256=sha(read(Path(__file__).resolve()))))
        while True:
            try: print(json.dumps(sync(root),ensure_ascii=False),flush=True)
            except (OSError,ValueError): print(json.dumps(dict(at=now(),error='Archive sync unavailable; retrying')),flush=True)
            time.sleep(interval)


def ensure_watch(root):
    """One detached, private archive worker serves all registered universes."""
    root=mkdir(root)
    with (root/'.watch-start.lock').open('a') as startup:
        os.fchmod(startup.fileno(),0o600);fcntl.flock(startup,fcntl.LOCK_EX)
        with (root/'.watch.lock').open('a') as ownership:
            os.fchmod(ownership.fileno(),0o600)
            try:fcntl.flock(ownership,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                info=load(root/'watcher.json');os.kill(info['pid'],0)
                return dict(running=True,already_running=True,pid=info['pid'])
            fcntl.flock(ownership,fcntl.LOCK_UN)
        tools=mkdir(root/'tools');source=Path(__file__).resolve();installed=tools/'accepted_task_archive.py'
        if source != installed:atomic(installed,read(source))
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(WORKSPACE/'.tmp'))
        with (root/'watcher.log').open('ab') as log:
            os.fchmod(log.fileno(),0o600)
            proc=subprocess.Popen([sys.executable,'-B',str(installed),'--root',str(root),'watch'],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,cwd=root,env=env,start_new_session=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if proc.poll() is not None:raise ValueError('Archive watcher failed startup')
            try:
                info=load(root/'watcher.json')
                if info.get('pid')==proc.pid:return dict(running=True,already_running=False,pid=proc.pid)
            except (OSError,ValueError):pass
            time.sleep(0.1)
        raise ValueError('Archive watcher readiness timeout')


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('register');a.add_argument('--universe',type=Path,required=True);a.add_argument('--id')
    sub.add_parser('sync');v=sub.add_parser('verify');v.add_argument('--bundle',type=Path)
    a=sub.add_parser('watch');a.add_argument('--interval',type=int,default=60)
    sub.add_parser('ensure-watch')
    args=p.parse_args();root=local(args.root)
    if args.command=='register':print(json.dumps(dict(source_id=register(root,args.universe,args.id))))
    elif args.command=='sync':print(json.dumps(sync(root),ensure_ascii=False))
    elif args.command=='watch':watch(root,max(10,args.interval))
    elif args.command=='ensure-watch':print(json.dumps(ensure_watch(root)))
    else:
        entries=[args.bundle] if args.bundle else [root/e['path'] for e in load(root/'index.json')['entries']]
        for entry in entries:
            m=verify_bundle(entry);print(json.dumps(dict(verified=True,archive_id=m['archive_id'],task=m['task']['name'],trials=5,pass_rate=m['statistics']['pass_rate'])))


if __name__=='__main__':main()
