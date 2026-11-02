"""Install versioned, credential-free skills into isolated role roots."""
import argparse,hashlib,json,os,shutil,re,tempfile
from pathlib import Path
SOURCE=Path(__file__).with_name('skills')
BEGIN='<!-- managed-role-skills:start -->'
END='<!-- managed-role-skills:end -->'
RECEIPT='.manifest.json'
SCOPE='Installed resources only; capabilities.json verifies local executable readiness.'
def selected(role):
    if role.startswith('author-'):return ['acs-task-pipeline','harbor-acs-evaluation','acs-task-troubleshooting']
    if role.startswith('evaluator-'):return ['acs-task-pipeline','harbor-acs-evaluation','acs-quality-rollout-review','acs-task-troubleshooting']
    if role=='curator':return ['acs-task-pipeline','acs-review-coordination','acs-task-troubleshooting']
    raise ValueError('Unsupported role')
def bundle(path):
    """Hash every skill resource, not only the entrypoint."""
    path=Path(path)
    if path.is_symlink() or not (path/'SKILL.md').is_file():raise ValueError('Missing or linked skill bundle')
    files={}
    for item in sorted(path.rglob('*')):
        if item.is_symlink():raise ValueError('Skill resource links forbidden')
        if item.is_file():files[item.relative_to(path).as_posix()]=hashlib.sha256(item.read_bytes()).hexdigest()
    digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'sha256':digest,'files':files}

def migrate_legacy_receipt(skills,role):
    """Hide only a recognized installer receipt; never delete an arbitrary file."""
    legacy=skills/'manifest.json';target=skills/RECEIPT
    if legacy.is_symlink():raise ValueError('Legacy skill manifest is a symlink')
    if not legacy.exists():return
    if not legacy.is_file() or legacy.stat().st_size>1024*1024:
        raise ValueError('Legacy manifest is not an installer receipt')
    try:record=json.loads(legacy.read_text())
    except (ValueError,UnicodeError):raise ValueError('Legacy manifest is not an installer receipt') from None
    if (not isinstance(record,dict) or set(record)!={'role','skills','scope'}
            or record.get('role')!=role or record.get('scope')!=SCOPE
            or not isinstance(record.get('skills'),dict) or set(record['skills'])!=set(selected(role))):
        raise ValueError('Legacy manifest is not an installer receipt')
    for name,item in record['skills'].items():
        if (not isinstance(item,dict) or set(item)!={'sha256','files'}
                or not re.fullmatch('[a-f0-9]{64}',str(item.get('sha256','')))
                or not isinstance(item.get('files'),dict) or 'SKILL.md' not in item['files']
                or any(not isinstance(name,str) or not re.fullmatch('[a-f0-9]{64}',str(value)) for name,value in item['files'].items())):
            raise ValueError('Legacy manifest is not an installer receipt')
        if bundle(skills/name)!=item:
            raise ValueError('Legacy manifest does not match installed skill resources')
    if target.is_symlink():raise ValueError('Skill manifest is a symlink')
    if target.exists():
        if not target.is_file() or target.read_bytes()!=legacy.read_bytes():
            raise ValueError('Conflicting hidden skill manifest')
        legacy.unlink()
    else:legacy.rename(target)

def discovery(skills):
    """Match native skill discovery: every visible entry must be a skill directory."""
    skills=Path(skills)
    if skills.is_symlink() or not skills.is_dir():raise ValueError('Invalid skill discovery root')
    names=[]
    for entry in sorted(skills.iterdir()):
        if entry.name.startswith('.'):continue
        if (entry.is_symlink() or not entry.is_dir() or not (entry/'SKILL.md').is_file()
                or (entry/'SKILL.md').is_symlink()):
            raise ValueError('Visible skill entry must be a directory with SKILL.md: '+entry.name)
        names.append(entry.name)
    return {'ready':True,'visible_entries':names,'receipt_path':'/skills/'+RECEIPT}

def install(root,role,owner=65534):
    root=Path(root);names=selected(role);skills=root/'skills'
    if skills.is_symlink():raise ValueError('Skills root is a symlink')
    skills.mkdir(exist_ok=True)
    migrate_legacy_receipt(skills,role)
    for name in names:
        src=SOURCE/name;dst=skills/name
        if dst.is_symlink():raise ValueError('Skill destination is a symlink')
        dst.mkdir(exist_ok=True)
        for p in src.rglob('*'):
            if p.is_symlink():raise ValueError('Skill source links forbidden')
            rel=p.relative_to(src);target=dst/rel
            if p.is_dir():target.mkdir(exist_ok=True)
            else:
                if target.is_symlink():raise ValueError('Skill file links forbidden')
                shutil.copyfile(p,target);target.chmod(0o644)
                if owner is not None:os.chown(target,owner,owner)
        dst.chmod(0o755)
    receipt=skills/RECEIPT
    if receipt.is_symlink():raise ValueError('Skill manifest is a symlink')
    payload=json.dumps({'role':role,'skills':{name:bundle(skills/name) for name in names},'scope':SCOPE},ensure_ascii=False,indent=2)+'\n'
    fd,pending=tempfile.mkstemp(prefix='.manifest-pending-',dir=skills)
    try:
        with os.fdopen(fd,'w') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.chmod(pending,0o644)
        if owner is not None:os.chown(pending,owner,owner)
        os.replace(pending,receipt)
    finally:Path(pending).unlink(missing_ok=True)
    discovery(skills)
    instruction=root/'instruction.md';text=instruction.read_text()
    if BEGIN in text:
        a=text.index(BEGIN);b=text.index(END,a)+len(END);text=text[:a]+text[b:]
    lines=[BEGIN,'## 可用 ACS skills（开工时先读相关技能）',
           '启动前平台生成 /capabilities.json，记录实际解释器、导入、工具、指令和完整技能包版本；/skills/.manifest.json 列出每个资源文件摘要。开工先读取该回执；ready=false或回执缺失时报告具体缺项，不能声称环境已通过。']
    lines.extend('- /skills/'+n+'/SKILL.md' for n in names)
    lines.extend(['技能已安装不代表云执行权限已授予；按实际入口交接，不能把本地模拟当作ACS验收。',END,''])
    instruction.write_text(chr(10).join(lines)+text.lstrip())
    if owner is not None:os.chown(instruction,owner,owner)
    instruction.chmod(0o644)
    return names
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,default=Path('/mnt/data/zhanglinfeng/workspace/.state/science-role-broker/config.json'));args=p.parse_args()
    for role,entry in json.loads(args.config.read_text())['roles'].items():print(role,install(entry['root'],role))
