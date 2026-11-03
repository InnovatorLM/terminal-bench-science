#!/usr/bin/env python3
"""Root-owned local command broker; controller UID only, no host shell commands.

The controller keeps provider/admin credentials. Every requested program executes
inside one preprovisioned role root as that role's numeric UID, with a fresh env.
The UNIX socket is never placed inside any role root. Networking is unrestricted.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import resource
import re
import selectors
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import time

MAX_REQUEST = 16 * 1024 * 1024
MAX_OUTPUT = 1048576
BROKER_LOG = None


def trusted(path, directory=False, owner=0):
    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError('Broker configuration/root must be root-owned and not externally writable')
    if directory != stat.S_ISDIR(info.st_mode):
        raise ValueError('Unexpected configuration/root type')
    return path.resolve(strict=True)


def load_config(path):
    path = trusted(path)
    config = json.loads(path.read_text())
    if set(config) - {'controller_uid', 'socket', 'roles', '_lock_dir', '_forgejo_url'} or not {'controller_uid', 'socket', 'roles'} <= set(config):
        raise ValueError('Unexpected broker configuration fields')
    if not isinstance(config['controller_uid'], int) or config['controller_uid'] <= 0:
        raise ValueError('Controller must be nonroot')
    seen = set()
    for role, value in config['roles'].items():
        if set(value) != {'root', 'uid', 'gid'} or not (
            role == 'curator' or
            re.fullmatch(r'(author|evaluator)-[0-9]{2,4}', role)
        ):
            raise ValueError('Unexpected role configuration')
        root = trusted(value['root'], directory=True, owner=Path(value['root']).stat().st_uid)
        if root.stat().st_uid not in {0, config['controller_uid']}:
            raise ValueError('Role root must be owned by root or trusted controller')
        if not root.is_absolute() or value['uid'] < 20000 or value['gid'] < 20000:
            raise ValueError('Unsafe role identity/root')
        if value['uid'] in seen or value['uid'] == config['controller_uid']:
            raise ValueError('Role identities must be distinct from each other and controller')
        seen.add(value['uid'])
        if (root / 'proc/1').exists() or (root / 'proc/self/root').exists():
            raise ValueError('Host proc must not be exposed')
        value['root'] = str(root)
    roots = [Path(value['root']) for value in config['roles'].values()]
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError('Role roots must not overlap')
    socket_name = config['socket']
    if socket_name.startswith('@'):
        # Abstract UNIX sockets are names, not filesystem paths.  They are
        # deliberately short (the kernel limit is 108 bytes) and are already
        # bound to one universe by deployment_paths.broker_socket_name().
        name = socket_name[1:]
        if not name or len(socket_name.encode()) > 107:
            raise ValueError('Invalid abstract broker socket name')
    else:
        sock = Path(socket_name)
        if not sock.is_absolute() or any(sock == root or root in sock.parents for root in roots):
            raise ValueError('Socket must be outside role roots')
        trusted(sock.parent, directory=True)
    return config


def limits():
    # Runs in the single-threaded forked handler's child before chroot/dropuid.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    # Git packfiles legitimately exceed 64 MiB. A per-file cap killed
    # index-pack with SIGXFSZ and surfaced only as invalid index-pack output.
    # Retain command lifetime/output limits without capping repository files.
    resource.setrlimit(resource.RLIMIT_CPU, (14400, 14400))


def role_processes(uid):
    """Dedicated role UIDs are reserved to this broker, never shared workloads."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid:
                # Zombies cannot execute/read files and are reaped by their parent.
                status = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                if status != 'Z':
                    found.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


def clean_role(uid):
    # Also catch setsid/double-fork descendants. Never select a shared account.
    deadline = time.monotonic() + 2
    while True:
        live = role_processes(uid)
        if not live or time.monotonic() >= deadline:
            return live
        for pid in live:
            try:
                # Bind the signal to the process object, never a reused numeric
                # PID. Recheck UID after opening the handle.
                descriptor = os.pidfd_open(pid)
                try:
                    if Path('/proc', str(pid)).stat().st_uid == uid:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                finally:
                    os.close(descriptor)
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass
        time.sleep(0.01)


def execute_command(config, request, connection, gateway_env=()):
    if set(request) != {'role', 'cwd', 'argv', 'timeout'}:
        raise ValueError('Unexpected request fields')
    role = config['roles'][request['role']]
    if role_processes(role['uid']):
        raise ValueError('Dedicated role UID has unexpected live processes')
    root = Path(role['root'])
    # The engine holds its configuration roots to 0700. Only the trusted broker
    # grants traversal within the chroot; roles cannot change this directory.
    root.chmod(0o755)
    workspace = root / 'workspace'
    if workspace.stat().st_uid == config['controller_uid']:
        workspace.chmod(0o2770)
    cwd = Path(request['cwd'])
    if not cwd.is_absolute() or '..' in cwd.parts:
        raise ValueError('Invalid working directory')
    # Resolve on the host first. Symlinks must not turn controller bookkeeping
    # into a host path escape; actual command resolution occurs after chroot.
    host_cwd = (root / str(cwd).lstrip('/')).resolve(strict=True)
    if not host_cwd.is_relative_to(root) or not host_cwd.is_dir():
        raise ValueError('Working directory escaped role root')
    argv = request['argv']
    if not isinstance(argv, list) or not argv or len(argv) > 256 or any(not isinstance(arg, str) or '\0' in arg for arg in argv):
        raise ValueError('Invalid command arguments')
    timeout = request['timeout']
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 14400:
        raise ValueError('Invalid command timeout')
    # No command string is interpreted on the host. chroot discards supplementary
    # groups; no_new_privs prevents staged setuid/file-capability escalation.
    command = ['/usr/bin/setpriv', '--no-new-privs', '/usr/sbin/chroot',
               '--userspec=' + str(role['uid']) + ':' + str(role['gid']),
               '--groups=' + str(role['gid']), str(root), '/usr/bin/env', '-i',
               'PATH=/home/role/.local/bin:/home/role/.local/env/bin:/usr/local/bin:/usr/bin:/bin', 'HOME=/home/role',
               'TMPDIR=/tmp', 'LD_LIBRARY_PATH=/home/role/.local/env/lib', 'PYTHONUSERBASE=/home/role/.local', 'PIP_CACHE_DIR=/home/role/.cache/pip', 'MAMBA_ROOT_PREFIX=/home/role/.local/mamba', 'CONDA_PREFIX=/home/role/.local/env', 'XDG_CACHE_HOME=/home/role/.cache',
               'XDG_CONFIG_HOME=/home/role/.config', 'XDG_STATE_HOME=/home/role/.state',
               'GEWU_FORGEJO_URL=' + config.get('_forgejo_url', 'http://127.0.0.1:3000'),
               'LANG=C', 'LC_ALL=C',
               'GIT_ASKPASS=/usr/local/bin/role-askpass', 'GIT_TERMINAL_PROMPT=0',
               *gateway_env, '/bin/bash', '-c', 'cd -- "$1" || exit; shift; exec "$@"',
               'role-exec', str(cwd), *argv]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin'},
                               close_fds=True, start_new_session=True, preexec_fn=limits)
    selector = selectors.DefaultSelector()
    selector.register(connection, selectors.EVENT_READ, 'connection')
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    chunks = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    reason = None
    try:
        while process.poll() is None or any(key.data != 'connection' for key in selector.get_map().values()):
            if time.monotonic() >= deadline:
                reason = 'timeout'
                break
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                if key.data == 'connection':
                    if not connection.recv(1, socket.MSG_PEEK):
                        reason = 'controller_disconnected'
                        break
                    raise ValueError('Only one request per connection')
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fileobj)
                else:
                    chunks[key.data].extend(block)
                    if sum(map(len, chunks.values())) > MAX_OUTPUT:
                        reason = 'output_limit'
                        break
            if reason:
                break
    finally:
        # Popen retains the leader's PID until wait/reap; descendants are cleaned
        # through dedicated-UID pidfds, including setsid/double-fork children.
        if process.poll() is None and reason is None:
            # Pipe EOF can precede the leader's exit by a scheduler tick.
            # Do not kill a successful runner during that ordinary transition.
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                reason = 'leader_exit_timeout'
        if process.poll() is None:
            process.kill()
        process.wait()
        remaining = clean_role(role['uid'])
        selector.close()
        process.stdout.close()
        process.stderr.close()
    # Redact before any output can enter native model messages or transcripts.
    secret_values = []
    credential = root / 'home/role/git-credential.json'
    if credential.exists():
        record = json.loads(credential.read_text())
        secret_values.extend(v for k,v in record.items() if k in ('token','password') and isinstance(v,str) and v)
    secret_values.extend(v.split('=',1)[1] for v in gateway_env if v.startswith('GEWU_ROLE_GATEWAY_TOKEN='))
    def clean_output(data):
        text = bytes(data[:MAX_OUTPUT]).decode(errors='replace')
        for secret in secret_values: text = text.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)(authorization[\"\s:=]+(?:bearer|token)\s+)[^\s\",]+', r'\1[REDACTED]', text)
    return {'returncode': process.returncode, 'stdout': clean_output(chunks['stdout']),
            'stderr': clean_output(chunks['stderr']), 'termination': reason,
            'cleanup_confirmed': not remaining}


def execute(config, request, connection):
    argv = request.get('argv', [])
    if not argv or argv[0] != '/usr/local/bin/role-codex-runner':
        return execute_command(config, request, connection)
    import http.server
    import secrets
    import threading
    from role_codex_gateway import handler_type
    from controller_credentials import load_controller_credentials
    if len(argv) != 2:
        raise ValueError('Invalid Codex runner invocation')
    payload = json.loads(argv[1])
    if payload.get('role') != request.get('role'):
        raise ValueError('Codex role identity mismatch')
    credentials = load_controller_credentials()
    key = credentials.get('model_keys', {}).get(request['role']) or credentials.get('model_key')
    if not isinstance(key, str) or not key:
        raise ValueError('Designated role model key missing')
    token = secrets.token_hex(32)
    gateway = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler_type(key, access_token=token))
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        return execute_command(config, request, connection, (
            'GEWU_ROLE_GATEWAY_TOKEN=' + token,
            'GEWU_ROLE_GATEWAY_PORT=' + str(gateway.server_address[1])))
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=5)


def health(config, request):
    """Readiness never takes a role command lock or starts/cleans a role process.

    The execution path still enforces chroot, dedicated UID and cleanup for
    every command. A busy role therefore cannot make the broker look dead.
    """
    if set(request) == {'op'} and request['op'] == 'health':
        return {'protocol': 'role-health-v1', 'ready': True, 'role': None}
    if set(request) != {'op', 'role', 'cwd'} or request['op'] != 'health':
        raise ValueError('Invalid health request')
    role = config['roles'][request['role']]
    root = trusted(role['root'], directory=True, owner=Path(role['root']).stat().st_uid)
    if root.stat().st_uid not in {0, config['controller_uid']}:
        raise ValueError('Untrusted role root')
    cwd = Path(request['cwd'])
    if not cwd.is_absolute() or '..' in cwd.parts:
        raise ValueError('Invalid health working directory')
    inside = (root / str(cwd).lstrip('/')).resolve(strict=True)
    if not inside.is_relative_to(root / 'workspace') or not inside.is_dir():
        raise ValueError('Health working directory escaped role workspace')
    for relative in ('bin/bash', 'usr/bin/env'):
        binary = root / relative
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError('Role execution binary unavailable')
    return {'protocol': 'role-health-v1', 'ready': True, 'role': request['role']}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = {}
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in {0, self.server.config['controller_uid']}:
                raise ValueError('Controller UID required')
            self.request.settimeout(120)
            line = self.rfile.readline(MAX_REQUEST + 1)
            # Startup readiness probes authenticate SO_PEERCRED, then close
            # without sending a command. Treat that EOF as a normal probe.
            if not line:
                return
            if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                try:
                    log_path=self.server.config.get('_log_path', BROKER_LOG)
                    if log_path:
                        with open(log_path, 'a') as log:
                            log.write(f'broker refusal bytes={len(line)} max={MAX_REQUEST} newline={line.endswith(bytes([10]))}\n')
                except OSError:
                    pass
                raise ValueError('Request too large or incomplete')
            request = json.loads(line)
            self.request.settimeout(None)
            if request.get('op') == 'health':
                response = health(self.server.config, request)
                self.wfile.write(json.dumps(response).encode() + b'\n')
                return
            role = request['role']
            if role not in self.server.config['roles']:
                raise ValueError('Unknown role')
            # A UID is exclusive while a command runs and while descendants are
            # cleaned, including requests arriving on different connections.
            lock_dir = self.server.config.get('_lock_dir')
            if lock_dir:
                lock_base = Path(lock_dir)
                lock_base.mkdir(parents=True, exist_ok=True)
            else:
                lock_base = Path(self.server.config['socket'].removeprefix('@')).parent
            lock = lock_base / (role + '.lock')
            with lock.open('a') as stream:
                # Duplicate requests must not occupy broker children forever
                # behind a long command, including after their client leaves.
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                response = execute(self.server.config, request, self.request)
        except (ValueError, KeyError, OSError, TypeError, OverflowError) as error:
            # Persist broker refusals; otherwise the product only sees an empty
            # run directory and cannot distinguish startup failure from waiting.
            try:
                log_path=self.server.config.get('_log_path', BROKER_LOG)
                if log_path:
                    with open(log_path, 'a') as log:
                        log.write(f'broker refusal role={request.get("role")} error={error!s}\n')
            except OSError:
                pass
            response = {'error': 'Role command refused'}
        try:
            self.wfile.write(json.dumps(response).encode() + b'\n')
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ForkingMixIn, socketserver.UnixStreamServer):
    # Each active role owns one broker child while its Codex session is alive.
    # Three slots starved the 14-role universe: queued role requests timed out
    # before their run could write a transcript. Keep a bounded but sufficient
    # pool for the configured author/evaluator/curator set.
    max_children = 32
    block_on_close = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Broker must be launched by root')
    config = load_config(args.config)
    config['_log_path'] = str(Path(args.config).resolve().parent / 'production-broker.log')
    config.setdefault('_lock_dir', None)
    config['_forgejo_url'] = os.environ.get('GEWU_FORGEJO_URL', config.get('_forgejo_url', 'http://127.0.0.1:3000'))
    abstract = config['socket'].startswith('@')
    path = Path(config['socket'].removeprefix('@'))
    if not abstract and path.exists():
        raise SystemExit('Socket already exists; inspect live broker before restart')
    # The workspace's FUSE storage cannot create socket inodes. Linux abstract
    # sockets keep the same SO_PEERCRED boundary without any out-of-workspace file.
    address = '\0' + str(path) if abstract else str(path)
    # A 30-author universe has 34 roles. Leave one slot per role plus bounded
    # control-plane headroom; the old fixed 32 could starve the last roles.
    Server.max_children = max(32, len(config['roles']) + 8)
    server = Server(address, Handler)
    server.config = config
    if not abstract:
        os.chown(path, config['controller_uid'], config['controller_uid'])
        path.chmod(0o600)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if not abstract:
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
