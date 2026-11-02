"""Install a bounded author-only policy, preserving skills and reset history."""
from pathlib import Path
import os
import re
import tempfile

BEGIN = '<!-- BEGIN AUTHOR POST ACCEPTANCE POLICY -->'
END = '<!-- END AUTHOR POST ACCEPTANCE POLICY -->'


def update(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing or linked author instruction')
    text = path.read_text()
    if BEGIN in text:
        start = text.index(BEGIN)
        end = text.index(END, start) + len(END)
        text = text[:start] + text[end:]
    text = re.sub(r'^F\. 当 evaluator[^\n]+',
                  'F. 题目正式接受后，按本文的“题目接受后的经验帖与下一题”规则，在原 proposal/PR 回复 Markdown 复盘，再主动开始下一题。',
                  text, flags=re.M)
    text = text.replace('H. 成功或放弃后写 lessons/<task-id>.md，',
                        'H. 成功后在原 proposal/PR 回复经验帖；放弃后可写 lessons/<task-id>.md，')
    policy = Path(__file__).with_name('author-post-acceptance-policy.md').read_text()
    content=BEGIN + '\n' + policy + END + '\n\n' + text.lstrip()
    info=path.stat()
    fd,pending=tempfile.mkstemp(prefix='.author-policy-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),info.st_mode & 0o777)
            os.fchown(stream.fileno(),info.st_uid,info.st_gid)
            stream.write(content)
        os.replace(pending,path)
    finally:
        Path(pending).unlink(missing_ok=True)


def install(config, universe):
    paths = set()
    for role, entry in config['roles'].items():
        if not re.fullmatch(r'author-[0-9]+', role):
            continue
        paths.update([Path(universe)/'.config/gewu-multiverse/agents'/role/'instruction.md',
                      Path(entry['root'])/'instruction.md'])
    for path in sorted(paths):
        update(path)
