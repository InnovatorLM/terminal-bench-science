"""Inject current shared policy into both controller and isolated role instructions."""
from pathlib import Path
import re

BEGIN='<!-- BEGIN TASK QUALITY POLICY -->'
END='<!-- END TASK QUALITY POLICY -->'


def update(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Missing or linked role instruction')
    text=path.read_text()
    if BEGIN in text:
        start=text.index(BEGIN);end=text.index(END,start)+len(END)
        text=text[:start]+text[end:]
    # Remove superseded requirements from the detailed body as well as placing
    # current policy first; contradictory legacy prose still wastes sessions.
    text=text.replace('来源与许可','可核验的现实来源').replace('、与现有任务区别','')
    text=text.replace('提案通过后实现 Harbor 题目','记录提案后自主实现 Harbor 题目，无需等待人工提案批准')
    text=text.replace('理想目标是正确率低于 30%；这是困难目标而不是硬性淘汰线，整体正确率低于 50% 可以接受。',
        '五次独立有效评测正确率小于等于60%满足难度条件，越难越好；大于等于80%严格拒绝。')
    policy=Path(__file__).with_name('task-quality-policy.md').read_text()
    path.write_text(BEGIN+'\n'+policy+END+'\n\n'+text.lstrip())


def install(config, universe):
    for role,entry in config['roles'].items():
        if not re.fullmatch(r'(author|evaluator)-[0-9]+',role):continue
        for path in [Path(universe)/'.config/gewu-multiverse/agents'/role/'instruction.md',Path(entry['root'])/'instruction.md']:
            update(path)
            if role.startswith('author-'):
                from author_difficulty_policy import update as update_difficulty
                update_difficulty(path)
