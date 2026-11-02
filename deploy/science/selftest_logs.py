"""Bounded sanitized selftest diagnostics, never raw operator logs."""
import json
import os
from pathlib import Path
import re
import base64

PATTERN=re.compile(r'(?i)((?:api[_-]?key|password|token|authorization|secret)\s*["\x27]?\s*[:=]\s*["\x27]?)([^\s,"\x27}]+)')

def sanitize(text,secrets=()):
    for secret in sorted((s for s in secrets if s),key=len,reverse=True):
        text=text.replace(secret,'[REDACTED]')
    text=re.sub(r'(?i)Bearer\s+[^\s,"\x27]+','Bearer [REDACTED]',text)
    text=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[REDACTED PRIVATE KEY]',text,flags=re.S)
    return PATTERN.sub(lambda m:m[1]+'[REDACTED]',text)

def credential_values(root):
    """Exact-value protection for the role token and operator cloud credentials."""
    values=[]
    paths=[Path(root)/'home/role/git-credential.json']
    from evaluation_queue import W
    paths += list((W/'.config/acr').glob('*.json'))
    paths += [W/'.state/docker-config-shanghai-public/config.json']
    def visit(obj):
        if isinstance(obj,dict):
            for key,value in obj.items():
                if re.search('token|password|secret|auth|key',key,re.I) and isinstance(value,str) and len(value)>7:values.append(value)
                else:visit(value)
                if key=='auth' and isinstance(value,str):
                    try:
                        decoded=base64.b64decode(value).decode()
                        values.append(decoded)
                        if ':' in decoded:values.append(decoded.split(':',1)[1])
                    except (ValueError,UnicodeError):pass
        elif isinstance(obj,list):
            for value in obj:visit(value)
    for path in paths:
        if path.is_file() and not path.is_symlink():visit(json.loads(path.read_text()))
    # Original model secret is read only into memory, never copied to published files.
    import os
    values.extend(value for key,value in os.environ.items() if re.search('API_KEY|PASSWORD|TOKEN',key) and len(value)>7)
    import shlex
    env_file=W/'.config/llm/dawnfm.env'
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if re.match(r'^\s*(?:export\s+)?OPENAI_API_KEY=',line):
                parsed=shlex.split(line.split('=',1)[1],comments=True)
                if len(parsed)==1:values.append(parsed[0])
    return values

def publish(work,root,job,gid,secrets=()):
    work,root=Path(work),Path(root)
    directory=root/'selftest/results';output=directory/(job+'.diagnostics.txt')
    sections=[]
    paths=[work/name for name in ['build.log','oracle.log','nop.log']]
    paths+=sorted((work/'build').glob('docker-*.log'))
    paths+=sorted((work/'runs').glob('*/*/verifier/test-stdout.txt'))
    paths+=sorted((work/'runs').glob('*/*/verifier/ctrf.json'))
    for path in paths:
        if not path.is_file() or path.is_symlink():continue
        with path.open('rb') as stream:
            stream.seek(0,2);start=max(0,stream.tell()-16384);stream.seek(start);raw=stream.read(16384)
        if start:raw=raw.partition(b'\n')[2]
        sections.append('## '+str(path.relative_to(work))+'\n'+sanitize(raw.decode(errors='replace'),secrets))
    text='Selftest diagnostics; bounded log tails, not a success attestation.\n'+'\n'.join(sections[-12:])
    pending=directory/(job+'.diagnostics.pending')
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o640)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chown(pending,0,gid);pending.replace(output)
    finally:pending.unlink(missing_ok=True)
    return '/selftest/results/'+output.name
