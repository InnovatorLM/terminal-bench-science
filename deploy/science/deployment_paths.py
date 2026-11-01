"""Explicit deployment identity; never silently select another universe."""
import json
import hashlib
import os
import re
import socket
from pathlib import Path
WORKSPACE=Path('/mnt/data/zhanglinfeng/workspace')

def allowed_development_path(path, workspace=WORKSPACE):
    """Accept durable workspace or the explicitly selected runtime, not all /tmp."""
    resolved=Path(path).resolve();workspace=Path(workspace).resolve()
    return resolved.is_relative_to(workspace) or resolved.is_relative_to(universe_root(workspace))

def broker_socket_name(root):
    """Return a short, collision-resistant abstract socket name.

    Linux limits AF_UNIX names to 108 bytes.  A full CPFS workspace path can
    exceed that limit, so putting the universe path directly in an abstract
    socket silently makes startup fail.  The digest keeps two universes
    separate without depending on the length of their path.
    """
    root=Path(root).resolve()
    return '@gewu-broker-' + hashlib.sha256(str(root).encode()).hexdigest()[:32]

def universe_root(workspace=WORKSPACE):
    workspace=Path(workspace).resolve()
    configured=os.environ.get('GEWU_UNIVERSE_ROOT')
    path=Path(configured) if configured else workspace/'universes/my-terminal-bench-science'
    resolved=path.resolve()
    # The science deployment may explicitly opt into a local-disk runtime
    # snapshot.  It is still restricted to the named gewu runtime prefix and
    # never selected implicitly; durable source and evidence remain in the
    # workspace universe.
    local_runtime = str(resolved).startswith('/tmp/gewu-terminal-bench-science-runtime-')
    if not path.is_absolute() or (not resolved.is_relative_to(workspace) and not local_runtime) or resolved==workspace:
        raise ValueError('Universe root must be an absolute directory below workspace')
    return resolved

def deployment_state_root(root=None):
    """State owned by one deployment; never share mutable state across universes."""
    root=Path(root) if root is not None else universe_root()
    root=root.resolve()
    local_runtime = str(root).startswith('/tmp/gewu-terminal-bench-science-runtime-')
    if (not root.is_relative_to(WORKSPACE.resolve()) and not local_runtime) or root==WORKSPACE.resolve():
        raise ValueError('Deployment state root must be inside workspace')
    return root/'.state/gewu-multiverse'

def select_forgejo_port(runtime, explicit=None):
    """Keep a seeded Forgejo database and all clients on the same port."""
    if explicit:
        return _valid_forgejo_port(explicit)
    app_ini=Path(runtime)/'.local/share/gewu-multiverse/forgejo/instance/app.ini'
    if app_ini.is_file():
        match=re.search(r'^HTTP_PORT\s*=\s*(\d+)\s*$',app_ini.read_text(),re.M)
        if not match:
            raise ValueError('Forgejo app.ini is missing HTTP_PORT')
        return _valid_forgejo_port(match.group(1))
    for port in range(3000,3100):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1',port))
                return port
            except OSError:
                pass
    raise ValueError('no free Forgejo port in supported range 3000-3099')

def ensure_runtime_forgejo_port(runtime, selected):
    """Synchronize the seeded instance with the port chosen for this launch."""
    app_ini=Path(runtime)/'.local/share/gewu-multiverse/forgejo/instance/app.ini'
    if not app_ini.is_file():
        return
    text=app_ini.read_text()
    if not re.search(r'^HTTP_PORT\s*=\s*\d+\s*$',text,re.M):
        raise ValueError('Forgejo app.ini is missing HTTP_PORT')
    text=re.sub(r'^(HTTP_PORT\s*=\s*)\d+\s*$',lambda m:m.group(1)+str(_valid_forgejo_port(selected)),text,flags=re.M)
    text=re.sub(r'^(ROOT_URL\s*=\s*http://127\.0\.0\.1:)\d+(/?)\s*$',lambda m:m.group(1)+str(selected)+m.group(2),text,flags=re.M)
    app_ini.write_text(text)

def _valid_forgejo_port(value):
    try:
        port=int(value)
    except (TypeError,ValueError):
        raise ValueError('Forgejo port must be an integer in range 1-65535') from None
    if not 1<=port<=65535:
        raise ValueError('Forgejo port must be an integer in range 1-65535')
    return port

def _allowed_external_root():
    """Optional stopped role-root donor used by the /tmp hot-runtime.

    The donor must be an explicit workspace universe.  Its role roots remain
    identity-isolated and are never accepted from an arbitrary host path.
    """
    value=os.environ.get('GEWU_EXTERNAL_ROLE_ROOTS')
    if not value:
        return None
    path=Path(value).resolve()
    if not path.is_relative_to(WORKSPACE.resolve()) or path==WORKSPACE.resolve():
        raise ValueError('External role roots must be inside workspace')
    return path

def require_isolated_service_config(root=None, workspace=WORKSPACE):
    """Fail closed until each universe has its own broker and evaluator services."""
    root=Path(root) if root is not None else universe_root(workspace)
    state=deployment_state_root(root)
    broker=state/'role-broker/config.json'
    queue=root/'.config/science-evaluation/queue.json'
    required={
        'role broker': broker,
        'evaluation queue': queue,
        'review dispatcher': state/'review-dispatcher/config.json',
        'author selftest worker': state/'author-selftest/config.json',
        'command watchdog': state/'command-watchdog/config.json',
        'deployment readiness marker': state/'deployment.json',
    }
    missing=[name for name,path in required.items() if not path.is_file() or path.is_symlink()]
    if missing:
        raise RuntimeError('Universe services are not isolated/configured: '+', '.join(missing))
    try:
        broker_config=json.loads(broker.read_text())
        queue_config=json.loads(queue.read_text())
        expected_roles=(root/'.config/gewu-multiverse/agents').resolve()
        external_root=_allowed_external_root()
        external_roles=(external_root/'.config/gewu-multiverse/agents').resolve() if external_root else None
        socket=broker_config['socket']
        if socket.startswith('@'):
            if socket != broker_socket_name(root):
                raise ValueError
        else:
            socket_path=Path(socket)
            if not socket_path.resolve().is_relative_to(root):
                raise ValueError
        for role in broker_config['roles'].values():
            role_path=Path(role['root']).resolve()
            if not role_path.is_relative_to(expected_roles) and not (external_roles and role_path.is_relative_to(external_roles)):
                raise ValueError
        queues=queue_config.get('role_queues') or [queue_config]
        for queue_entry in queues:
            for key in ('inbox','results','state','registry'):
                path=Path(queue_entry.get(key,queue_config.get(key,''))).resolve()
                if not path.is_relative_to(root) and not (external_root and path.is_relative_to(external_root)):
                    raise ValueError
        for name,path in [('review dispatcher',required['review dispatcher']),
                          ('author selftest worker',required['author selftest worker']),
                          ('command watchdog',required['command watchdog'])]:
            service=json.loads(path.read_text())
            if Path(service['broker_config']).resolve()!=broker.resolve() or not Path(service['state']).resolve().is_relative_to(root):
                raise ValueError
        marker=json.loads(required['deployment readiness marker'].read_text())
        if marker.get('status')!='service_config_prepared' or Path(marker['universe_root']).resolve()!=root:
            raise ValueError
    except (OSError,KeyError,TypeError,ValueError):
        raise RuntimeError('Universe service configuration is malformed or references shared state') from None
    return state
