"""Repair small runtime basics in localized role roots without copying CPFS trees."""
from __future__ import annotations
import json, os, shutil, stat, subprocess, tempfile
from pathlib import Path


def _safe(root: Path, rel: str) -> Path:
    root=root.resolve(); p=(root/rel).resolve()
    if not p.is_relative_to(root): raise ValueError('path escapes role root')
    cur=root
    for part in Path(rel).parts:
        cur=cur/part
        if cur.is_symlink(): raise ValueError('symlink in role runtime path')
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

def repair_role(role_root: str|Path, role: str, workspace: str|Path, *, uid: int|None=None) -> dict:
    root=Path(role_root); uid=os.getuid() if uid is None else int(uid)
    root=root.resolve(); root.mkdir(parents=True,exist_ok=True)
    for rel in ('workspace','runs','cache','tmp','home/role','dev','usr/local/bin'):
        p=_safe(root,rel); p.mkdir(parents=True,exist_ok=True)
        if rel in ('workspace','runs','cache','tmp','home/role'):
            os.chown(p,uid,uid); os.chmod(p,0o700)
    null=_safe(root,'dev/null')
    if not null.exists():
        try: os.mknod(null, stat.S_IFCHR|0o666, os.makedev(1,3))
        except PermissionError: null.touch()
    os.chmod(null,0o666)
    cfg=Path(workspace)/'.config/universe-roles'/f'{role}.json'
    if not cfg.exists():
        kind='evaluator' if role.startswith('evaluator') else 'author'
        cfg=Path(workspace)/'.config/universe-roles'/f'{kind}.json'
    creds=json.loads(cfg.read_text())
    cred=_safe(root,'home/role/git-credential.json')
    _install(cred,json.dumps({'login':creds['login'],'token':creds['token']}).encode(),uid)
    ask=b'#!/usr/local/bin/python3\nimport json,sys\nc=json.load(open("/home/role/git-credential.json"))\nprint(c["login"] if "Username" in sys.argv[1] else c["token"])\n'
    _install(_safe(root,'usr/local/bin/role-askpass'),ask,0,0o755)
    src=Path(__file__).with_name('role_forgejo.py')
    _install(_safe(root,'usr/local/bin/role-forgejo'),src.read_bytes(),0,0o755)
    return probe(root)

def probe(root: str|Path) -> dict:
    root=Path(root).resolve(); out={'root':str(root)}
    for cmd in (['/usr/local/bin/python3','-c','import json,ssl; print("ok")'],['/usr/bin/git','--version'],['/usr/local/bin/role-askpass','Username']):
        try:
            r=subprocess.run(['chroot',str(root),*cmd],capture_output=True,text=True,timeout=10)
            out[' '.join(cmd[:2])]=r.returncode==0
        except Exception: out[' '.join(cmd[:2])]=False
    out['null_io']=_safe(root,'dev/null').exists()
    return out
