"""Independent evidence I/O helpers, safe to import during worker hot updates."""
import hashlib
from pathlib import Path
import stat


def sha256_stream(stream):
    """Hash a file-like object on Python versions without hashlib.file_digest."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def evidence_file(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError('Evidence path escaped job')
    if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError('Evidence symlink refused')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Evidence file type refused')
    return path


def read_file(path, root):
    return evidence_file(path, root).read_bytes()


def digest_file(path, root):
    with evidence_file(path, root).open('rb') as stream:
        return sha256_stream(stream)
