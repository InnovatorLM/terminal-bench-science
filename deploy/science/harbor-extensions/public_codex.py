"""Codex adapter that only exposes a revocable job credential to uploaded tasks."""
import json
import time
from pathlib import Path
from harbor.agents.installed.codex_preinstall import CodexPreinstall

W=Path('/mnt/data/zhanglinfeng/workspace')


class PublicCodex(CodexPreinstall):
    def __init__(self,*args,public_model_lease=None,extra_env=None,**kwargs):
        path=Path(public_model_lease).resolve(strict=True)
        if not path.is_relative_to(W/'.state/harbor-public/runs') or path.name!='model-lease.json' or path.stat().st_mode&0o077:
            raise ValueError('Require private operator-owned public model lease')
        lease=json.loads(path.read_text())
        if lease['expires_at']<=time.time() or lease.get('revoked'):
            raise ValueError('Public model lease expired')
        env=dict(extra_env or {})
        env.update(OPENAI_API_KEY=lease['token'],PUBLIC_MODEL_ACCESS_TOKEN=lease['token'],
                   OPENAI_BASE_URL=lease['base_url'],OPENAI_API_BASE=lease['base_url'],
                   CODEX_API_KEY_ENV='PUBLIC_MODEL_ACCESS_TOKEN',CODEX_FORCE_AUTH_JSON='false')
        env.pop('CODEX_AUTH_JSON_PATH',None)
        super().__init__(*args,extra_env=env,**kwargs)
