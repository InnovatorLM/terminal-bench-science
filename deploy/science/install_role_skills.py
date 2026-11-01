"""Install versioned, credential-free skills into isolated role roots."""
import argparse,json,os,shutil
from pathlib import Path
SOURCE=Path(__file__).with_name('skills')
BEGIN='<!-- managed-role-skills:start -->'
END='<!-- managed-role-skills:end -->'
def selected(role):
    if role.startswith('author-'):return ['acs-task-pipeline','harbor-acs-evaluation']
    if role.startswith('evaluator-'):return ['acs-task-pipeline','harbor-acs-evaluation','acs-quality-rollout-review']
    if role=='curator':return ['acs-task-pipeline','acs-review-coordination']
    raise ValueError('Unsupported role')
def install(root,role,owner=65534):
    root=Path(root);names=selected(role);skills=root/'skills';skills.mkdir(exist_ok=True)
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
    instruction=root/'instruction.md';text=instruction.read_text()
    if BEGIN in text:
        a=text.index(BEGIN);b=text.index(END,a)+len(END);text=text[:a]+text[b:]
    lines=[BEGIN,'## 可用 ACS skills（开工时先读相关技能）',
           '启动前平台生成 /capabilities.json，记录实际解释器、导入、工具、指令和技能版本。开工先读取该回执；ready=false或回执缺失时报告具体缺项，不能声称环境已通过。']
    lines.extend('- /skills/'+n+'/SKILL.md' for n in names)
    lines.extend(['技能已安装不代表云执行权限已授予；按实际入口交接，不能把本地模拟当作ACS验收。',END,''])
    instruction.write_text(chr(10).join(lines)+text.lstrip())
    if owner is not None:os.chown(instruction,owner,owner)
    instruction.chmod(0o644)
    return names
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,default=Path('/mnt/data/zhanglinfeng/workspace/.state/science-role-broker/config.json'));args=p.parse_args()
    for role,entry in json.loads(args.config.read_text())['roles'].items():print(role,install(entry['root'],role))
