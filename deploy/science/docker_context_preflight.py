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
        missing = report['missing_sources']
        super().__init__('Missing Docker build context inputs: ' + '; '.join(
            f"{row['dockerfile']}:{row['line']} {row['source']}" for row in missing))


def inspect_context(directory, label):
    directory = Path(directory)
    dockerfile = directory / 'Dockerfile'
    result = dict(dockerfile=label + '/Dockerfile', checked_sources=[],
                  missing_sources=[], deferred=[], docker_executed=False)
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


def require_contexts(task):
    contexts = [inspect_context(Path(task) / label, label) for label in ('environment', 'tests')]
    result = dict(contexts=contexts, missing_sources=[row for context in contexts
                  for row in context['missing_sources']], docker_executed=False,
                  runtime_verified=False)
    if result['missing_sources']:
        raise DockerContextError(result)
    return result
