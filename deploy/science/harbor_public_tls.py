"""Workspace-local certificate renewal and service-owned TLS secret deployment."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from cryptography import x509

W=Path('/mnt/data/zhanglinfeng/workspace')
HOST='nlb-2vwqetbd2t1ieqwdum.cn-beijing.nlb.aliyuncsslb.com'
CONFIG=W/'.config/harbor-public/acme'
STATE=W/'.state/harbor-public'
PYTHON=W/'acs-harbor-test/.venv/bin/python'


def check():
    cert=CONFIG/'live'/HOST/'fullchain.pem'
    parsed=x509.load_pem_x509_certificate(cert.read_bytes())
    days=(parsed.not_valid_after_utc-datetime.now(timezone.utc)).total_seconds()/86400
    renewed=False
    if days<30:
        env=dict(os.environ,PYTHONPATH=str(W/'.local/harbor-acme'),PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(W/'.tmp'))
        cmd=[str(PYTHON),str(W/'.local/harbor-acme/bin/certbot'),'renew','--non-interactive',
             '--config-dir',str(CONFIG),'--work-dir',str(STATE/'acme'),
             '--logs-dir',str(STATE/'acme-logs')]
        with (STATE/'certificate-renewal.log').open('a') as log:
            subprocess.run(cmd,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
        renewed=True
    digest=hashlib.sha256(cert.read_bytes()).hexdigest()
    record=STATE/'certificate-status.json'
    prior=json.loads(record.read_text()) if record.exists() else {}
    if prior.get('deployed_sha256')!=digest:
        obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':'gateway-tls','namespace':'harbor-public-linfeng'},
             'type':'kubernetes.io/tls','stringData':{'tls.crt':cert.read_text(),'tls.key':(cert.parent/'privkey.pem').read_text()}}
        p=subprocess.run([str(W/'.local/bin/kubectl'),'--kubeconfig',str(W/'.config/acs/kubeconfig'),'apply','-f','-'],
                         input=json.dumps(obj),text=True,capture_output=True,timeout=30)
        if p.returncode:raise RuntimeError('TLS secret deployment failed')
    parsed=x509.load_pem_x509_certificate(cert.read_bytes())
    record.write_text(json.dumps(dict(checked_at=datetime.now(timezone.utc).isoformat(),
        expires_at=parsed.not_valid_after_utc.isoformat(),deployed_sha256=digest,renewed=renewed),indent=2))
    record.chmod(0o600)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');a=p.parse_args()
    while True:
        try:check()
        except Exception as e:
            print('Certificate maintenance failed: '+type(e).__name__,flush=True)
            if a.once:raise
        if a.once:break
        time.sleep(12*3600)
