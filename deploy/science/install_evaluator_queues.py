"""Provision isolated evaluation request/result folders for current evaluator UIDs."""
import json
import os
from pathlib import Path
W=Path('/mnt/data/zhanglinfeng/workspace')
def install():
    broker_path=W/'.state/science-role-broker/config.json'
    roles=json.loads(broker_path.read_text())['roles']
    path=W/'.config/science-evaluation/queue.json'
    config=json.loads(path.read_text())
    queues=[]
    for name,role in sorted(roles.items()):
        if not name.startswith('evaluator-'):continue
        base=Path(role['root'])/'evaluation'
        base.mkdir(exist_ok=True);base.chmod(0o755)
        os.chown(base,0,role['gid'])
        for folder in ('inbox','results'):
            directory=base/folder;directory.mkdir(exist_ok=True)
            os.chown(directory,role['uid'] if folder=='inbox' else 0,role['gid'])
            directory.chmod(0o700 if folder=='inbox' else 0o750)
        from evaluation_queue import evaluation_catalog
        registry=json.loads(Path(config['registry']).read_text())
        catalog=base/'catalog.json'
        catalog.write_text(json.dumps(evaluation_catalog(registry),indent=2))
        os.chown(catalog,0,role['gid']);catalog.chmod(0o640)
        queues.append(dict(role=name,evaluator_uid=role['uid'],inbox=str(base/'inbox'),results=str(base/'results')))
    if not queues:raise ValueError('No current evaluator roles')
    config['role_queues']=queues
    config['broker_config']=str(broker_path)
    config['max_jobs_per_day']=0
    for key in ('inbox','results','evaluator_uid'):config.pop(key,None)
    pending=path.with_suffix('.pending');pending.write_text(json.dumps(config,indent=2));pending.chmod(0o600);pending.replace(path)
    print(json.dumps({'evaluator_queues':[q['role'] for q in queues],'automatic_daily_quota':False}))
if __name__=='__main__':install()
