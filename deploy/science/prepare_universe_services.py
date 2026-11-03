"""Create a universe-local service configuration from a stopped deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from deployment_paths import WORKSPACE, broker_socket_name, universe_root

DEFAULT_BROKER=WORKSPACE/'.state/science-role-broker/config.json'
DEFAULT_QUEUE=WORKSPACE/'.config/science-evaluation/queue.json'


def trusted_json(path):
    path=Path(path)
    info=path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
        raise ValueError('Source configuration ownership/type/mode refused')
    return json.loads(path.read_text())


def plan(root, source_broker=DEFAULT_BROKER, source_queue=DEFAULT_QUEUE):
    root=Path(root).resolve()
    if not root.is_relative_to(WORKSPACE.resolve()) or root==WORKSPACE.resolve():
        raise ValueError('Universe root must be inside workspace')
    state=root/'.state/gewu-multiverse'
    broker_out=state/'role-broker/config.json'
    queue_out=root/'.config/science-evaluation/queue.json'
    registry_out=root/'.config/science-evaluation/registry.json'
    marker=state/'deployment.json'
    destinations=[broker_out,queue_out,registry_out,marker,
                  state/'review-dispatcher/config.json',state/'author-selftest/config.json',
                  state/'command-watchdog/config.json']
    if any(p.exists() or p.is_symlink() for p in destinations):
        raise ValueError('Target service configuration already exists; refusing overwrite')
    broker=trusted_json(source_broker)
    queue=trusted_json(source_queue)
    agent_root=(root/'.config/gewu-multiverse/agents').resolve()
    if not broker.get('roles') or not broker.get('controller_uid'):
        raise ValueError('Source broker has no configured roles/controller')
    roles={}
    for name,entry in broker['roles'].items():
        raw_role_root=Path(entry['root'])
        if raw_role_root.is_symlink():
            raise ValueError('Role root symlinks are refused')
        role_root=raw_role_root.resolve()
        if not role_root.is_relative_to(agent_root) or role_root==agent_root or not role_root.is_dir():
            raise ValueError('Source role roots must already belong to the target universe')
        roles[name]=entry
    evaluators={name:entry for name,entry in roles.items() if name.startswith('evaluator-')}
    queues=queue.get('role_queues')
    if not isinstance(queues,list) or not queues:
        raise ValueError('Source evaluation queue has no role queues')
    configured={}
    for item in queues:
        name=item.get('role')
        if name not in evaluators or name in configured or item.get('evaluator_uid')!=evaluators[name]['uid']:
            raise ValueError('Evaluator queue identity does not match broker role')
        role_root=Path(evaluators[name]['root']).resolve()
        for key,owner in [('inbox',item['evaluator_uid']),('results',0)]:
            raw_path=Path(item[key])
            if raw_path.is_symlink():
                raise ValueError('Evaluator queue directory symlink refused')
            path=raw_path.resolve()
            info=raw_path.lstat()
            if not path.is_relative_to(role_root) or not stat.S_ISDIR(info.st_mode) or info.st_uid!=owner or info.st_mode&0o022:
                raise ValueError('Evaluator queue directory identity/mode refused')
        configured[name]=item
    if set(configured)!=set(evaluators):
        raise ValueError('Every configured evaluator needs exactly one queue')
    registry_source=Path(queue['registry'])
    registry=trusted_json(registry_source)
    if not isinstance(registry, dict):
        raise ValueError('Evaluation registry must be an object')
    for entry in registry.values():
        template=entry.get('template')
        if template:
            raw_template=Path(template)
            if raw_template.is_symlink():
                raise ValueError('Registered template symlink refused')
            template_path=raw_template.resolve()
            if not template_path.is_relative_to(WORKSPACE.resolve()) or not template_path.is_file():
                raise ValueError('Registered template is missing or outside workspace')
        raw_snapshot=Path(entry['snapshot'])
        if raw_snapshot.is_symlink():
            raise ValueError('Registered snapshot symlink refused')
        snapshot=raw_snapshot.resolve()
        if not snapshot.is_relative_to(WORKSPACE.resolve()) or not snapshot.is_dir():
            raise ValueError('Registered snapshot is missing or outside workspace')
    # Abstract sockets are validated against the universe identity.  A path
    # based socket can exceed Linux's AF_UNIX limit on CPFS, so use the same
    # bounded name as the runtime launcher.
    broker['socket']=broker_socket_name(root)
    queue['state']=str(state/'evaluation-queue')
    queue['registry']=str(registry_out)
    queue['broker_config']=str(broker_out)
    queue['role_queues']=[configured[name] for name in sorted(configured)]
    service_configs={
        state/'review-dispatcher/config.json':{'broker_config':str(broker_out),'state':str(state/'review-dispatcher')},
        state/'author-selftest/config.json':{'broker_config':str(broker_out),'state':str(state/'author-selftest')},
        state/'command-watchdog/config.json':{'broker_config':str(broker_out),'state':str(state/'command-watchdog')},
    }
    marker_payload={'status':'service_config_prepared','universe_root':str(root),'prepared_at':time.time(),
                    'source_broker_sha256':hashlib.sha256(Path(source_broker).read_bytes()).hexdigest(),
                    'source_registry_sha256':hashlib.sha256(registry_source.read_bytes()).hexdigest()}
    return {'root':root,'broker':(broker_out,broker),'queue':(queue_out,queue),
            'registry':(registry_out,registry),'services':service_configs,'marker':(marker,marker_payload),
            'directories':[state/'role-broker',state/'evaluation-queue',state/'review-dispatcher',
                           state/'author-selftest',state/'command-watchdog',queue_out.parent]}


def atomic_json(path,payload):
    path=Path(path)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(payload,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600)
        os.link(name,path,follow_symlinks=False)
        Path(name).unlink()
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def apply_plan(value):
    if os.geteuid()!=0:
        raise PermissionError('Run service provisioning as root')
    created=[]
    try:
        for directory in value['directories']:
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        for path,payload in (value['broker'],value['queue'],value['registry'],*value['services'].items()):
            atomic_json(path,payload);created.append(path)
        # The ready marker is the commit record and is always written last.
        atomic_json(*value['marker']);created.append(value['marker'][0])
    except BaseException:
        for path in reversed(created):Path(path).unlink(missing_ok=True)
        raise
    return {'status':'service_config_prepared','universe_root':str(value['root']),
            'files':[str(path) for path in created]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--universe-root',type=Path,default=universe_root())
    parser.add_argument('--source-broker',type=Path,default=DEFAULT_BROKER)
    parser.add_argument('--source-queue',type=Path,default=DEFAULT_QUEUE)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    value=plan(args.universe_root,args.source_broker,args.source_queue)
    result={'status':'dry_run','universe_root':str(value['root']),
            'files':[str(x) for x in [value['broker'][0],value['queue'][0],value['registry'][0],*value['services'],value['marker'][0]]]}
    if args.apply:result=apply_plan(value)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
