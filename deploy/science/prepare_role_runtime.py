"""Provision isolated role roots for the science universe."""
import json
import os
from pathlib import Path
import shutil
import subprocess

w = Path('/mnt/data/zhanglinfeng/workspace')
from deployment_paths import broker_socket_name, deployment_state_root, universe_root
universe = universe_root(w)
source = w / '.state/role-chroot-proof'
# Python package installation is role-local: vendor pip into the immutable
# runtime source, while each role receives its own user site and cache.
host_pip = Path('/usr/local/lib/python3.10/site-packages/pip')
host_pip_info = Path('/usr/local/lib/python3.10/site-packages/pip-23.0.1.dist-info')
if host_pip.is_dir() and not (source / 'usr/local/lib/python3.10/site-packages/pip/__init__.py').is_file():
    shutil.copytree(host_pip, source / 'usr/local/lib/python3.10/site-packages/pip', dirs_exist_ok=True)
if host_pip_info.is_dir() and not (source / 'usr/local/lib/python3.10/site-packages/pip-23.0.1.dist-info/METADATA').is_file():
    shutil.copytree(host_pip_info, source / 'usr/local/lib/python3.10/site-packages/pip-23.0.1.dist-info', dirs_exist_ok=True)
pip_launcher = source / 'usr/local/bin/pip'
pip_launcher.write_text('#!/bin/sh\nexec /usr/local/bin/python3 -m pip \"$@\"\n')
pip_launcher.chmod(0o755)
control = deployment_state_root(universe) / 'role-broker'
control.mkdir(mode=0o700, parents=True, exist_ok=True)
control.chmod(0o700)
config = dict(controller_uid=65534, socket=broker_socket_name(universe), roles={})
config_root = universe / '.config/gewu-multiverse'
agents = config_root / 'agents'
agents.mkdir(exist_ok=True)
os.chown(agents, 65534, 65534)
agents.chmod(0o700)
author_count = int(os.environ.get('GEWU_AUTHOR_COUNT', '10'))
evaluator_count = int(os.environ.get('GEWU_EVALUATOR_COUNT', '3'))
uid_base = int(os.environ.get('GEWU_ROLE_UID_BASE', '21001'))
if not (1 <= author_count <= 100 and 1 <= evaluator_count <= 30 and uid_base >= 20000):
    raise ValueError('Invalid role counts or isolated UID base')
credentials_dir = Path(os.environ.get('GEWU_ROLE_CREDENTIALS_DIR', str(w / '.config/universe-roles'))).resolve()
if not credentials_dir.is_relative_to(w):
    raise ValueError('Role credentials must remain inside the workspace')
roles = ([f'author-{i:02d}' for i in range(1, author_count + 1)] +
         [f'evaluator-{i:02d}' for i in range(1, evaluator_count + 1)] + ['curator'])
if os.environ.get('GEWU_ROLE_CREDENTIALS_DIR'):
    # A fresh universe must never silently share a kind-level fallback account.
    identities = [json.loads((credentials_dir / (role + '.json')).read_text())['login'] for role in roles]
    if len(set(identities)) != len(roles):
        raise ValueError('Every role requires its own Forgejo identity')
for index, role in enumerate(roles):
    if os.environ.get('GEWU_PROVISION_ROLE') and role != os.environ['GEWU_PROVISION_ROLE']:
        continue
    uid = uid_base + index
    root = agents / role
    root.mkdir(exist_ok=True)
    os.chown(root, 65534, 65534)
    root.chmod(0o755)
    # Immutable system tools can share root-owned inodes, not role-owned state.
    for name in ['bin', 'usr', 'lib', 'lib64', 'etc']:
        if (source / name).exists():
            # Local /tmp runtime may be on a different device than the durable
            # workspace, so hard-linking is invalid. Copy files when devices
            # differ; same-device deployments may still hard-link safely.
            def copy_or_link(a, b):
                if Path(b).exists(): return b
                try: os.link(a, b); return b
                except OSError: shutil.copy2(a, b); return b
            shutil.copytree(source / name, root / name, dirs_exist_ok=True,
                            copy_function=copy_or_link)
    for name in ['workspace', 'runs', 'cache', 'skills', 'home/role', 'tmp', 'dev']:
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        if name in ['workspace', 'runs']:
            os.chown(target, 65534, uid)
            target.chmod(0o2770)
        elif name in ['home/role', 'tmp', 'cache']:
            os.chown(target, uid, uid)
            target.chmod(0o700)
        else:
            target.chmod(0o755)
    null = root / 'dev/null'
    if not null.exists():
        null.touch()
    null.chmod(0o666)
    kind = ('evaluator' if role.startswith('evaluator') else
            'author' if role.startswith('author-') else role)
    if kind == 'curator':
        instruction = Path(__file__).resolve().parents[2] / 'docs/science-universe/curator-coordination-policy.md'
    else:
        instruction_name = 'evaluator-instruction.md' if kind == 'evaluator' else 'author-instruction.md'
        instruction = universe / 'governance' / ('private' if kind == 'evaluator' else 'public') / instruction_name
    target = root / 'instruction.md'
    shutil.copyfile(instruction, target)
    os.chown(target, 65534, 65534)
    target.chmod(0o644)
    if kind == 'author':
        from author_difficulty_policy import update as update_difficulty
        update_difficulty(root/'instruction.md')
        from author_post_acceptance_policy import update as update_post_acceptance
        update_post_acceptance(root/'instruction.md')
        from role_policy import retire_pilot_feedback
        retire_pilot_feedback(root)
    target = root / 'home/role/git-credential.json'
    credential_name = role + '.json'
    if not (credentials_dir / credential_name).is_file():
        credential_name = kind + '.json'
    creds = json.loads((credentials_dir / credential_name).read_text())
    target.write_text(json.dumps({'login': creds['login'], 'token': creds['token']}))
    os.chown(target, uid, uid)
    target.chmod(0o600)
    askpass = root / 'usr/local/bin/role-askpass'
    # Never overwrite a shared tool inode (each role gets a separate askpass file).
    if askpass.exists():
        askpass.unlink()
    askpass.write_text('#!/usr/local/bin/python3\nimport json,sys\nc=json.load(open("/home/role/git-credential.json"))\nprint(c["login"] if "Username" in sys.argv[1] else c["token"])\n')
    askpass.chmod(0o755)
    for filename, executable in [('role_codex_runner.py', 'role-codex-runner'), ('role_forgejo.py', 'role-forgejo')]:
        target = root / 'usr/local/bin' / executable
        target.unlink(missing_ok=True)
        shutil.copyfile(Path(__file__).with_name(filename), target)
        target.chmod(0o755)
    proc_self = root / 'proc/self'
    proc_self.mkdir(parents=True, exist_ok=True)
    proc_self.chmod(0o755)
    executable = proc_self / 'exe'
    executable.unlink(missing_ok=True)
    executable.symlink_to('/usr/local/bin/role-self-exe' if (root/'usr/local/bin/role-self-exe').exists() else '/usr/local/bin/codex')
    if (root / 'proc/1').exists() or (proc_self / 'root').exists():
        raise ValueError('Host proc exposure refused')
    remote = root / 'usr/lib/git-core/git-remote-https'
    if not remote.exists():
        remote.symlink_to('git-remote-http')
    package_binary = w / '.local/bin/micromamba'
    if package_binary.is_file():
        from install_role_packages import provision
        provision(root, uid, uid, package_binary)
    if role.startswith('author-'):
        from selftest_clients import install_clock
        install_clock(root)
        policy=Path(__file__).resolve().parents[2]/'docs/science-universe/author-workflow-overlay.md'
        (root/'instruction.md').write_text(policy.read_text()+chr(10)+(root/'instruction.md').read_text())
    from install_review_dispatch_policy import install as install_review_policy
    install_review_policy(root, role)
    from install_role_skills import install as install_skills
    install_skills(root, role)
    if role.startswith("author-"):
        from install_author_selftest import install as install_selftest
        install_selftest(root, uid, uid)
    config['roles'][role] = dict(root=str(root), uid=uid, gid=uid)
path = control / (('provision-' + os.environ['GEWU_PROVISION_ROLE'] + '.json') if os.environ.get('GEWU_PROVISION_ROLE') else 'config.json')
path.write_text(json.dumps(config, indent=2))
path.chmod(0o600)
print(json.dumps({'config': str(path), 'roles': list(config['roles']), 'provider_key_staged': False}))
