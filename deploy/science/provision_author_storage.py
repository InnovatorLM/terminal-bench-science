#!/usr/bin/env python3
"""Prepare an isolated author instance; never start agents or copy credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil

WORKSPACE = Path('/mnt/data/zhanglinfeng/workspace')
UNIVERSE = WORKSPACE / 'universes/my-terminal-bench-science'


def provision(name, uid):
    if os.geteuid() != 0 or not re.fullmatch(r'author-[0-9]{2,4}', name):
        raise ValueError('Require root and author-NN instance name')
    if not 22000 <= uid < 60000:
        raise ValueError('Instance UID must be in 22000..59999')
    agents = UNIVERSE / '.config/gewu-multiverse/agents'
    root = agents / name
    if root.exists() or root.is_symlink():
        raise ValueError('Instance already exists; never overwrite persistent data')
    broker_path = WORKSPACE / '.state/science-role-broker/config.json'
    broker = json.loads(broker_path.read_text())
    if name in broker['roles'] or any(v['uid'] == uid for v in broker['roles'].values()):
        raise ValueError('Instance name or UID already registered')
    for candidate in agents.iterdir():
        home = candidate / 'home/role'
        if home.exists() and home.stat().st_uid == uid:
            raise ValueError('UID already owns another instance')
    if any(p.stat().st_uid == uid for p in Path('/proc').iterdir() if p.name.isdigit() and p.exists()):
        raise ValueError('UID has existing processes')
    root.mkdir(mode=0o755)
    source = WORKSPACE / '.state/role-chroot-proof'
    # Copies are deliberate: modifying a tool in one root must not alter another.
    for entry in ['bin', 'usr', 'lib', 'lib64', 'etc']:
        if (source / entry).exists():
            shutil.copytree(source / entry, root / entry, symlinks=True)
    for entry in ['workspace', 'home/role', 'tmp', 'runs', 'cache', 'skills', 'references', 'dev']:
        p = root / entry
        p.mkdir(parents=True, exist_ok=True)
        if entry in ['workspace', 'runs']:
            os.chown(p, 65534, uid); p.chmod(0o2770)
        elif entry in ['home/role', 'tmp', 'cache']:
            os.chown(p, uid, uid); p.chmod(0o700)
        else:
            p.chmod(0o755)
    (root / 'dev/null').touch(); (root / 'dev/null').chmod(0o666)
    shutil.copyfile(UNIVERSE / 'governance/public/author-instruction.md', root / 'instruction.md')
    (root / 'instruction.md').chmod(0o644)
    from author_post_acceptance_policy import update as update_post_acceptance
    update_post_acceptance(root/'instruction.md')
    # Public material only; no evaluator files or historical author HOME/worktree.
    for src in (UNIVERSE / 'governance/public').glob('*.md'):
        shutil.copyfile(src, root / 'references' / src.name)
        (root / 'references' / src.name).chmod(0o444)
    (root / 'etc/passwd').write_text(f'role:x:{uid}:{uid}:author:/home/role:/bin/bash\n')
    (root / 'etc/group').write_text(f'role:x:{uid}:\n')
    package_binary = WORKSPACE / '.local/bin/micromamba'
    if package_binary.is_file():
        from install_role_packages import provision as install_packages
        install_packages(root, uid, uid, package_binary)
    manifest = {'name': name, 'role': 'author', 'uid': uid, 'gid': uid,
                'root': str(root), 'workspace': '/workspace', 'home': '/home/role',
                'tmp': '/tmp', 'cache': '/cache', 'runs': '/runs',
                'references': '/references', 'desired': 'stopped',
                'credentials_staged': False, 'engine_registered': False,
                'broker_registered': False, 'disk_quota_bytes': None}
    (root / 'storage.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--uid', required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(provision(args.name, args.uid), indent=2))
