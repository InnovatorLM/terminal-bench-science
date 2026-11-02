"""Assemble a task-derived Codex runtime with version-bound native binaries."""
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
from evaluation_queue import W

def prepare(directory, official, codex, rg):
    directory=Path(directory)
    if not directory.resolve().is_relative_to(W) or directory.exists():
        raise ValueError('Require fresh workspace runtime context')
    versions={};records={}
    for name,source in [('codex',codex),('rg',rg)]:
        source=Path(source)
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(W):
            raise ValueError('Use workspace-owned native binaries')
        result=subprocess.run([str(source),'--version'],capture_output=True,text=True,timeout=20,check=True)
        match=re.search(r'\b([0-9]+\.[0-9]+\.[0-9]+)\b',result.stdout)
        if not match:raise ValueError('Cannot identify runtime version')
        versions[name]=match[1];records[name]=dict(version=match[1],sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    directory.mkdir()
    for name,source in [('codex',codex),('rg',rg)]:
        shutil.copyfile(source,directory/name);(directory/name).chmod(0o755)
    from runtime_trust import ca_bundle, receipt
    bundle=ca_bundle();shutil.copyfile(bundle,directory/'ca-certificates.crt')
    (directory/'ca-certificates.crt').chmod(0o644)
    (directory/'Dockerfile').write_text('FROM '+official+'\nCOPY codex rg /usr/local/bin/\n'
        'COPY ca-certificates.crt /opt/gewu-runtime/ca-certificates.crt\n'
        'ENV SSL_CERT_FILE=/opt/gewu-runtime/ca-certificates.crt\n'
        'ENV REQUESTS_CA_BUNDLE=/opt/gewu-runtime/ca-certificates.crt\n'
        'ENV CURL_CA_BUNDLE=/opt/gewu-runtime/ca-certificates.crt\n'
        'RUN codex --version && rg --version\n')
    return {'harness':'codex','harness_versions':versions,'native_binaries':records,
            'public_ca_bundle':receipt(bundle),'runtime_tag':'codex-'+versions['codex']}
