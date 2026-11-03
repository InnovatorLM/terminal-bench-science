"""Idempotent author guidance for evidence-based difficulty improvements."""
import os
from pathlib import Path
import tempfile

BEGIN = '<!-- BEGIN AUTHOR DIFFICULTY POLICY -->'
END = '<!-- END AUTHOR DIFFICULTY POLICY -->'

def update(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Expected regular author instruction')
    text=path.read_text()
    if BEGIN in text:
        start=text.index(BEGIN);stop=text.index(END,start)+len(END)
        text=text[:start]+text[stop:]
    content=BEGIN+'\n'+Path(__file__).with_name('author-difficulty-policy.md').read_text()+END+'\n\n'+text.lstrip()
    info=path.stat();fd,tmp=tempfile.mkstemp(prefix='.difficulty-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),info.st_mode & 0o777);os.fchown(stream.fileno(),info.st_uid,info.st_gid);stream.write(content)
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)
