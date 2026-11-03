"""Publish only the current ACME challenge to the service-owned gateway pod."""
import os
import subprocess
from pathlib import Path

W=Path('/mnt/data/zhanglinfeng/workspace')
token=os.environ['CERTBOT_TOKEN']
validation=os.environ['CERTBOT_VALIDATION']
if not token or any(not(c.isalnum() or c in '-_') for c in token):
    raise SystemExit('Invalid ACME token')
k=[str(W/'.local/bin/kubectl'),'--kubeconfig',str(W/'.config/acs/kubeconfig'),'-n','harbor-public-linfeng']
script='from pathlib import Path; import sys; p=Path("/certs/challenges"); p.mkdir(exist_ok=True); (p/sys.argv[1]).write_bytes(sys.stdin.buffer.read())'
subprocess.run(k+['exec','-i','deployment/harbor-public-gateway','--','python3','-c',script,token],input=validation.encode(),check=True,timeout=30)
