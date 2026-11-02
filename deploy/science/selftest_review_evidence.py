"""Deliver exact-version author controls to the assigned evaluator, without ACS reruns."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from author_submission_gate import read_operator_file, validate_identity, validate_receipt
from evaluation_queue import atomic_json, root_file, validated_template
from publish_evidence import redact
from selftest_logs import credential_values, sanitize


def sha(data):
    return hashlib.sha256(data).hexdigest()


def publish(entry, assignment, universe):
    universe=Path(universe)
    commit,task=assignment['commit'],assignment['task']
    key=commit+'-'+sha(task.encode())[:16]
    qualified=universe/'.state/science-author-preflight'/(key+'.json')
    if not qualified.exists():return None  # Older manual admissions have no automatic packet.
    validate_identity(commit,task)
    qualified_raw=read_operator_file(qualified)
    receipt=json.loads(qualified_raw)
    if receipt.get('origin')!='automatic_selftest_handoff':return None
    identifier=assignment['evaluation_id']
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',identifier):raise ValueError('Invalid evaluation ID')
    if receipt.get('snapshot_sha256')!=assignment['snapshot_sha256']:
        raise ValueError('Selftest packet snapshot differs from assignment')
    work=Path(receipt['handoff_evidence']).parent
    if not re.fullmatch('[a-f0-9]{32}',work.name):
        raise ValueError('Invalid bound operator selftest job')
    if assignment.get('job_id') is not None and assignment['job_id']!=work.name:
        raise ValueError('Assignment selftest job mismatch')
    parent=Path(entry['root'])/'evaluation/evidence'
    if any(p.is_symlink() for p in (parent,*parent.parents)):
        raise ValueError('Linked evaluator evidence directory')
    parent.mkdir(parents=True,exist_ok=True,mode=0o750)
    os.chown(parent,0,entry['gid']);parent.chmod(0o750)
    destination=parent/identifier
    if destination.is_symlink():raise ValueError('Linked evaluator evidence packet')
    manifest_path=destination/'selftest-manifest.json'
    binding=dict(evaluation_id=identifier,commit=commit,task=task,
                 snapshot_sha256=assignment['snapshot_sha256'],qualification_sha256=sha(qualified_raw))
    if manifest_path.exists():
        manifest=json.loads(read_operator_file(manifest_path))
        if any(manifest.get(k)!=v for k,v in binding.items()):
            raise ValueError('Existing selftest packet belongs to another qualification')
        for name,info in manifest['files'].items():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts:raise ValueError('Unsafe packet file')
            if sha(read_operator_file(destination/path))!=info['sha256']:
                raise ValueError('Published selftest evidence changed')
        return manifest

    validate_receipt(receipt,commit,task)
    queue=json.loads(root_file(universe/'.config/science-evaluation/queue.json').read_text())
    registered=json.loads(root_file(queue['registry']).read_text())[identifier]
    if (registered.get('source_commit')!=commit or registered.get('source_task_path')!=task):
        raise ValueError('Registered selftest source differs from review')
    template=validated_template(registered,{'snapshot_sha256':assignment['snapshot_sha256']})
    from selftest_registration import prepare
    bridge=prepare(work,commit,task,template)
    if bridge['snapshot_sha256']!=binding['snapshot_sha256']:
        raise ValueError('Selftest source differs from registered snapshot')
    secrets=credential_values(Path(entry['root']))
    contents={'selftest-status.json':read_operator_file(work/'status.json'),
              'selftest-qualification.json':qualified_raw,
              'selftest-handoff.json':read_operator_file(work/'handoff-evidence.json'),
              'selftest-build.json':read_operator_file(work/'build/receipt.json'),
              'selftest-mechanical.json':read_operator_file(work/'mechanical.json')}
    for control in ('oracle','nop'):
        for suffix in ('-report.json','.log'):
            path=work/(control+suffix)
            if path.exists():contents['selftest/'+path.name]=read_operator_file(path)
        for path in sorted((work/'runs').glob('*-'+control+'/*/verifier/*')):
            if path.suffix not in ('.json','.txt','.log') and path.name!='pytest.exit_status':continue
            name='selftest/'+control+'/'+path.parent.parent.name+'/verifier/'+path.name
            contents[name]=read_operator_file(path)
    staging=Path(tempfile.mkdtemp(prefix='.'+identifier+'-',dir=parent))
    files={}
    try:
        for name,raw in contents.items():
            if name.endswith('.json'):
                rendered=json.dumps(redact(json.loads(raw),secrets),ensure_ascii=False,indent=2).encode()
            else:rendered=sanitize(raw.decode(errors='replace'),secrets).encode()
            if any(value and value.encode() in rendered for value in secrets):
                raise ValueError('Credential remains in selftest evidence')
            path=staging/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
            path.write_bytes(rendered);path.chmod(0o640);os.chown(path,0,entry['gid'])
            files[name]=dict(source_sha256=sha(raw),sha256=sha(rendered),bytes=len(rendered))
        manifest=dict(schema_version=1,**binding,job_id=work.name,files=files,
                      controls_passed=True,scientific_acceptance=False,sanitized=True)
        atomic_json(staging/'selftest-manifest.json',manifest,0o640)
        for path in [staging,*staging.rglob('*')]:
            os.chown(path,0,entry['gid'])
            path.chmod(0o750 if path.is_dir() else 0o640)
        if not destination.exists():staging.rename(destination)
        else:
            # Recovery may find other version-bound metadata in this directory.
            # Never overwrite different existing evidence; publish the manifest last.
            for name in files:
                target=destination/name
                if target.exists() and read_operator_file(target)!=(staging/name).read_bytes():
                    raise ValueError('Conflicting existing selftest evidence')
            for name in [*files,'selftest-manifest.json']:
                target=destination/name
                if any(p.is_symlink() for p in target.parents):raise ValueError('Linked packet directory')
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
                os.chown(target.parent,0,entry['gid']);target.parent.chmod(0o750)
                (staging/name).replace(target)
        return manifest
    finally:
        if staging.exists():shutil.rmtree(staging)
