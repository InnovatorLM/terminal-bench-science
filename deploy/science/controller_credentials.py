"""Read the trusted controller's private credentials without exposing values."""
import os
import stat
import tomllib
from evaluation_queue import W
from deployment_paths import universe_root


def load_controller_credentials():
    if os.geteuid() != 0:
        raise ValueError('Operator required')
    path = universe_root(W) / '.config/gewu-multiverse/credentials.toml'
    info = path.lstat()
    parent = path.parent.lstat()
    # The controller runs as65534; its credentials intentionally are not root-owned.
    # Do not relax root_file(), which protects different evaluator-owned assets.
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid not in (0, 65534) or stat.S_IMODE(info.st_mode) != 0o600
            or not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid not in (0, 65534) or stat.S_IMODE(parent.st_mode) != 0o700
            or (not path.resolve().is_relative_to(W) and not str(path).startswith('/tmp/gewu-terminal-bench-science-runtime-'))):
        raise ValueError('Controller credential ownership/type/mode refused')
    credentials = tomllib.loads(path.read_text())
    # Production universe launches opt in explicitly to the user-designated
    # live model account.  This keeps the API key out of universe snapshots and
    # prevents a stale per-universe key from silently selecting another account.
    if os.environ.get('GEWU_LIVE_MODEL_TEST') == '1':
        expected = {
            'LLM_PROVIDER': 'dawnfm',
            'OPENAI_BASE_URL': 'https://dawnfm.gravarc.cn/v1',
            'OPENAI_MODEL': 'deepseek-v4.1-flash-1017',
        }
        if any(os.environ.get(name) != value for name, value in expected.items()):
            raise ValueError('Designated live model account configuration is required')
        key = os.environ.get('OPENAI_API_KEY')
        if not key:
            raise ValueError('Designated live model credential is unavailable')
        credentials['model_key'] = key
    return credentials
