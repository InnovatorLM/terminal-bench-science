"""Reject provably missing local COPY/ADD inputs without executing Dockerfiles.

This is an existence check, not a Docker parser or a successful build receipt.
Variable expansion, ignore rules and frontend extensions remain builder checks.
"""
import json
from pathlib import Path, PurePosixPath
import re
import shlex


class DockerContextError(ValueError):
    def __init__(self, report):
        self.report = report
        details = [f"{row['dockerfile']}:{row['line']} {row['source']}"
                   for row in report['missing_sources']]
        details.extend(f"{row['path']} ({row['reason']})" for row in report.get('missing_files', []))
        super().__init__('Invalid task build/verifier inputs: ' + '; '.join(details))


def inspect_context(directory, label):
    directory = Path(directory)
    dockerfile = directory / 'Dockerfile'
    result = dict(dockerfile=label + '/Dockerfile', checked_sources=[],
                  missing_sources=[], deferred=[], docker_executed=False,
                  dockerfile_present=dockerfile.is_file())
    if not dockerfile.is_file():
        return result
    source = dockerfile.read_text()
    # Do not mistake heredoc contents for Docker instructions or interpret a
    # frontend escape convention using shell rules.
    if '<<' in source or re.search(r'^\s*#\s*escape\s*=', source, re.I | re.M):
        result['deferred'].append(dict(reason='frontend_escape_or_heredoc'))
        return result
    if (directory / '.dockerignore').exists():
        result['deferred'].append(dict(reason='dockerignore_requires_builder'))
    pending = ''
    start = 0
    for number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith('#'):
            continue
        if not pending:
            start = number
        pending += line
        if pending.rstrip().endswith('\\'):
            pending = pending.rstrip()[:-1] + ' '
            continue
        instruction, pending = pending, ''
        match = re.match(r'^\s*(COPY|ADD)\s+(.*)$', instruction, re.I)
        if not match:
            continue
        body = match[2].strip()
        flags = []
        while body.startswith('--'):
            flag = re.match(r'(--[^\s]+)\s+(.*)$', body)
            if not flag:
                break
            flags.append(flag[1]); body = flag[2].lstrip()
        if any(flag.startswith('--from=') for flag in flags):
            continue  # Inputs belong to another stage/image, not this context.
        if any(not re.fullmatch(r'--(?:chown|chmod|exclude|checksum)=\S+|--(?:link|parents|keep-git-dir)(?:=(?:true|false))?', flag) for flag in flags):
            result['deferred'].append(dict(line=start, reason='unsupported_copy_flag'))
            continue
        try:
            args = json.loads(body) if body.startswith('[') else shlex.split(body, comments=False)
        except (ValueError, TypeError):
            result['deferred'].append(dict(line=start, reason='builder_syntax_check'))
            continue
        if not isinstance(args, list) or len(args) < 2 or not all(isinstance(x, str) for x in args):
            result['deferred'].append(dict(line=start, reason='builder_syntax_check'))
            continue
        for entry in args[:-1]:
            row = dict(dockerfile=result['dockerfile'], line=start, source=entry)
            if match[1].upper() == 'ADD' and re.match(r'(?:[a-zA-Z][\w+.-]*://|git@)', entry):
                result['deferred'].append(dict(row, reason='remote_add'))
                continue
            # Go filepath.Match and Docker variable expansion are not Python
            # glob/shlex. Leave ambiguous inputs to the actual builder.
            if not entry or any(c in entry for c in '$*?[]\\') or '..' in PurePosixPath(entry).parts:
                result['deferred'].append(dict(row, reason='dynamic_or_pattern_source'))
                continue
            target = directory / entry.lstrip('/')
            result['checked_sources'].append(row)
            if not target.exists():
                result['missing_sources'].append(row)
    return result


def verifier_build_plan(task, config):
    """Preserve Harbor's inheritance, but never discard a declared verifier image.

    This builder accepts Dockerfile-authored tasks. When no dedicated verifier
    environment was declared, its historical FROM-official fallback is valid
    for both shared mode and a fresh separate copy of the agent environment.
    A declared environment must instead supply its own image or Dockerfile.
    When both are present the explicit image wins, matching Harbor's prebuilt
    image behavior; the unused Dockerfile is recorded, not silently substituted.
    """
    from harbor.models.task.verifier_mode import resolve_task_verifier_mode
    task = Path(task)
    environment = config.verifier.environment
    image = environment.docker_image if environment is not None else None
    dockerfile = task / 'tests/Dockerfile'
    plan = dict(mode=resolve_task_verifier_mode(config).value,
                declared_environment=environment is not None,
                tests_materialization_verified=False, runtime_verified=False)
    if image:
        return dict(plan, source='declared_image', image=image,
                    dockerfile_present=dockerfile.is_file(),
                    dockerfile_used=False)
    if dockerfile.is_file():
        return dict(plan, source='dockerfile', dockerfile='tests/Dockerfile', dockerfile_used=True)
    if environment is None:
        return dict(plan, source='inherited_agent_image', dockerfile_used=False)
    return dict(plan, source='missing', dockerfile_used=False)


def require_contexts(task):
    from harbor.models.task.config import TaskConfig
    task = Path(task)
    # This is also called directly by the image builder, without inspect_task.
    # A missing verifier entrypoint must fail before pull/build/publication.
    missing_files = []
    for name in ('task.toml', 'environment/Dockerfile', 'tests/test.sh'):
        path = task / name
        if not path.is_file() or not path.read_bytes().strip():
            missing_files.append(dict(path=name, reason='missing_or_empty_required_file'))
    config = None
    plan = None
    if not any(row['path'] == 'task.toml' for row in missing_files):
        config = TaskConfig.model_validate_toml((task / 'task.toml').read_text())
        plan = verifier_build_plan(task, config)
        if plan['source'] == 'missing':
            missing_files.append(dict(path='tests/Dockerfile',
                reason='declared_verifier_environment_requires_dockerfile_or_image'))
        elif plan['source'] == 'dockerfile' and not (task / 'tests/Dockerfile').read_bytes().strip():
            missing_files.append(dict(path='tests/Dockerfile', reason='empty_verifier_dockerfile'))
    labels = ['environment']
    # Do not reject unused COPY inputs when a declared image is the verifier
    # definition. The required entrypoint is checked separately; its actual
    # upload/baked materialization remains an execution-template decision.
    if plan is None or plan['source'] == 'dockerfile':
        labels.append('tests')
    contexts = [inspect_context(task / label, label) for label in labels]
    result = dict(contexts=contexts, missing_sources=[row for context in contexts
                  for row in context['missing_sources']], missing_files=missing_files,
                  verifier_build=plan, docker_executed=False, runtime_verified=False,
                  failure_owner='task' if missing_files or any(context['missing_sources'] for context in contexts) else None)
    if result['missing_sources'] or missing_files:
        raise DockerContextError(result)
    return result
