"""Public CA trust for minimal task images; never disable TLS verification."""
import hashlib
from pathlib import Path
import ssl

IMPORT_PATH = 'universe_codex:UniverseCodex'


def configure_agent(agent):
    """Select the adapter even when a registered legacy template names Codex.

    Harbor gives a built-in name precedence over import_path. Both fields
    cannot be retained or the trust preparation is silently bypassed.
    """
    if agent.get('name') in (None, 'codex-preinstall') and (
            agent.get('import_path') == IMPORT_PATH or
            agent.get('name') == 'codex-preinstall' and not agent.get('import_path')):
        agent.update(name=None, import_path=IMPORT_PATH)


def ca_bundle():
    import certifi
    path = Path(certifi.where()).resolve(strict=True)
    workspace = Path('/mnt/data/zhanglinfeng/workspace')
    if not path.is_relative_to(workspace) or not path.is_file():
        raise ValueError('Use the workspace-installed public CA bundle')
    if not ssl.create_default_context(cafile=str(path)).get_ca_certs():
        raise ValueError('Public CA bundle is empty')
    return path


def receipt(path):
    raw = Path(path).read_bytes()
    return {'source': 'workspace certifi public CA bundle',
            'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
