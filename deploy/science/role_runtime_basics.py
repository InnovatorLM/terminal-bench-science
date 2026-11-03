"""Repair small runtime basics in localized role roots without copying CPFS trees."""
from __future__ import annotations
import errno, json, os, re, stat, subprocess, tempfile
from pathlib import Path


def _safe(root: Path, rel: str, *, allow_leaf_symlink: bool = False) -> Path:
    root=root.resolve(); p=(root/rel).resolve()
    if not p.is_relative_to(root): raise ValueError('path escapes role root')
    cur=root
    parts=Path(rel).parts
    for index,part in enumerate(parts):
        cur=cur/part
        if cur.is_symlink():
            if not (allow_leaf_symlink and index == len(parts)-1):
                raise ValueError('symlink in role runtime path')
            # A localized chroot intentionally aliases git-remote-https to the
            # immutable git-remote-http helper. Permit that leaf alias only when
            # its resolved target remains inside this role root.
            if not cur.resolve().is_relative_to(root):
                raise ValueError('symlink escapes role runtime path')
    return p

def _install(path: Path, data: bytes, uid: int, mode: int=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink(): raise ValueError('refusing symlink destination')
        return
    fd,tmp=tempfile.mkstemp(prefix='.install-',dir=str(path.parent))
    try:
        os.write(fd,data); os.fchmod(fd,mode); os.fchown(fd,uid,uid); os.close(fd)
        os.replace(tmp,path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def install_comparison_tool(role_root: str|Path):
    """Supply the host's small, immutable comparison tool without touching packages."""
    root=Path(role_root)
    for name in ('cmp','seq','nohup'):
        source=Path('/usr/bin')/name
        if not source.is_file() or source.stat().st_uid!=0 or not os.access(source,os.X_OK):
            raise ValueError('Trusted host executable unavailable: '+name)
        _install(_safe(root,'usr/local/bin/'+name),source.read_bytes(),0,0o755)

def repair_role(role_root: str|Path, role: str, workspace: str|Path, *, uid: int, controller_uid: int=65534) -> dict:
    root=Path(role_root); uid=int(uid)
    if os.geteuid()!=0 or uid<20000 or uid==controller_uid:
        raise ValueError('Operator and dedicated role UID required')
    if not re.fullmatch(r'(?:author|evaluator)-[0-9]{2,4}|curator',role):
        raise ValueError('Invalid role name')
    if root.is_symlink() or root.resolve()!=root or not root.is_dir():
        raise ValueError('Role root must be a real absolute directory')
    info=root.stat()
    if info.st_uid not in (0,controller_uid) or info.st_mode & 0o022:
        raise ValueError('Untrusted role root')
    # The role root is a chroot. Its immutable system paths must be
    # traversable by the dedicated role UID even when an older launcher
    # accidentally applied the controller's private 0700 mode recursively.
    # Private homes, caches and workspaces are repaired separately below.
    for rel in ('bin', 'usr', 'lib', 'lib64', 'etc'):
        p=_safe(root,rel)
        if not p.is_dir():
            continue
        os.chown(p,0,0); os.chmod(p,0o755)
    etc=_safe(root,'etc')
    if etc.is_dir():
        for p in etc.rglob('*'):
            if p.is_symlink():
                continue
            if p.is_dir():
                os.chown(p,0,0); os.chmod(p,0o755)
            elif p.is_file():
                os.chown(p,0,0); os.chmod(p,0o644)
    # Git invokes this helper by name for every HTTP(S) clone. It is an
    # immutable executable, but older localized copies retained mode 0644,
    # producing the misleading "remote-http is not a git command" startup
    # failure after the /etc permission issue was repaired.
    remote_http=_safe(root,'usr/lib/git-core/git-remote-http')
    if remote_http.is_file() and not remote_http.is_symlink():
        os.chown(remote_http,0,0); os.chmod(remote_http,0o755)
    remote_https=_safe(root,'usr/lib/git-core/git-remote-https',allow_leaf_symlink=True)
    if remote_https.exists() and not remote_https.is_symlink() and remote_https.is_file():
        os.chown(remote_https,0,0); os.chmod(remote_https,0o755)
    for rel in ('workspace','runs','cache','tmp','home/role','dev','usr/local/bin'):
        p=_safe(root,rel); p.mkdir(parents=True,exist_ok=True)
        if rel=='workspace':
            # Controller inspects .git and role writes checkout through broker.
            # Sharing only this dedicated group preserves both identities.
            os.chown(p,controller_uid,uid); os.chmod(p,0o2770)
        elif rel in ('runs','cache','tmp','home/role'):
            os.chown(p,uid,uid); os.chmod(p,0o700)
    null=_safe(root,'dev/null')
    if not null.exists():
        try: os.mknod(null, stat.S_IFCHR|0o666, os.makedev(1,3))
        except OSError as error:
            if error.errno not in (errno.EPERM,errno.EACCES,errno.ENOTSUP):raise
            null.touch()
    os.chmod(null,0o666)
    cred=_safe(root,'home/role/git-credential.json')
    if not cred.exists():
        cfg=Path(workspace)/'.config/universe-roles'/f'{role}.json'
        if not cfg.exists():
            kind='evaluator' if role.startswith('evaluator') else 'author' if role.startswith('author') else 'curator'
            cfg=Path(workspace)/'.config/universe-roles'/f'{kind}.json'
        info=cfg.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('Unsafe credential source')
        creds=json.loads(cfg.read_text())
        _install(cred,json.dumps({'login':creds['login'],'token':creds['token']}).encode(),uid)
    if not stat.S_ISREG(cred.stat().st_mode):raise ValueError('Unsafe credential destination')
    os.chown(cred,uid,uid);cred.chmod(0o600)
    ask=b'#!/usr/local/bin/python3\nimport json,sys\nc=json.load(open("/home/role/git-credential.json"))\nprint(c["login"] if "Username" in sys.argv[1] else c["token"])\n'
    _install(_safe(root,'usr/local/bin/role-askpass'),ask,0,0o755)
    src=Path(__file__).with_name('role_forgejo.py')
    _install(_safe(root,'usr/local/bin/role-forgejo'),src.read_bytes(),0,0o755)
    install_comparison_tool(root)
    return probe(root,uid)

def probe(root: str|Path, uid: int) -> dict:
    root=Path(root).resolve()
    code='''import json,os,ssl,subprocess
from pathlib import Path
c=json.loads(Path('/home/role/git-credential.json').read_text())
checks={}
for prompt,key in [('Username','login'),('Password','token')]:
 p=subprocess.run(['/usr/local/bin/role-askpass',prompt],capture_output=True,text=True)
 checks[key]=p.returncode==0 and p.stdout.strip()==c[key]
with open('/dev/null','r+b'):pass
p=subprocess.run(['/usr/bin/git','--version'],capture_output=True)
comparison=subprocess.run(['/usr/local/bin/cmp','/etc/gitconfig','/etc/gitconfig'],capture_output=True) if Path('/etc/gitconfig').is_file() else subprocess.run(['/usr/local/bin/cmp','/usr/local/bin/cmp','/usr/local/bin/cmp'],capture_output=True)
helper=Path('/usr/lib/git-core/git-remote-http')
probe=Path('/workspace/.role-runtime-probe');probe.write_text('ok');probe.unlink()
assert all(checks.values()) and p.returncode==0 and comparison.returncode==0 and helper.is_file() and os.access(helper,os.X_OK)
print(json.dumps({'uid':os.getuid(),'git_auth_helper':True,'git_remote_http':True,'python_ssl':True,'null_io':True,'workspace_write':True,'cmp':True}))
'''
    command=['/usr/bin/setpriv','--no-new-privs','/usr/sbin/chroot',
             '--userspec='+str(uid)+':'+str(uid),'--groups='+str(uid),str(root),
             '/usr/bin/env','-i','HOME=/home/role','PATH=/usr/local/bin:/usr/bin:/bin',
             'PYTHONDONTWRITEBYTECODE=1','/usr/local/bin/python3','-c',code]
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=30)
        observed=json.loads(result.stdout) if result.returncode==0 else {}
        return dict(role_uid=uid,ready=result.returncode==0 and observed.get('uid')==uid,checks=observed)
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        return dict(role_uid=uid,ready=False,error_type=type(error).__name__)
