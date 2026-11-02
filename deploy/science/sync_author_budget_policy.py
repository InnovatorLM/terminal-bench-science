"""Synchronize the active 48-hour rule into existing author instructions."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile


REPLACEMENTS = (
    (r'五小时', '48 小时'),
    (r'(?<!\d)5\s*小时', '48 小时'),
    (r'five-hour', '48-hour'),
    (r'(?<!\d)5-hour', '48-hour'),
)
SKILLS = ('acs-task-troubleshooting', 'harbor-acs-evaluation')


def update(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing or linked author policy: '+str(path))
    original = path.read_text()
    current = original
    for pattern, replacement in REPLACEMENTS:
        current = re.sub(pattern, replacement, current, flags=re.IGNORECASE)
    if current == original:
        return False
    info = path.stat()
    fd, name = tempfile.mkstemp(prefix='.budget-policy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(output.fileno(), info.st_uid, info.st_gid)
            output.write(current)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def sync(config, universe):
    changed = []
    paths = set()
    for role, entry in config['roles'].items():
        if not re.fullmatch(r'author-[0-9]+', role):
            continue
        for root in (Path(universe)/'.config/gewu-multiverse/agents'/role,
                     Path(entry['root'])):
            paths.add(root/'instruction.md')
            for skill in SKILLS:
                paths.add(root/'skills'/skill/'SKILL.md')
    for path in sorted(paths):
        if update(path):
            changed.append(str(path))
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--universe', type=Path, required=True)
    args = parser.parse_args()
    changed = sync(json.loads(args.config.read_text()), args.universe)
    print(json.dumps(dict(updated=len(changed), files=changed)))


if __name__ == '__main__':
    main()
