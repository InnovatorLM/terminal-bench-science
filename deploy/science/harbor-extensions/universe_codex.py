"""Codex with explicit public trust and a bounded pre-model TLS check."""
import json
import shlex
from harbor.agents.installed.codex_preinstall import CodexPreinstall
from runtime_trust import ca_bundle, receipt

REMOTE_CA = '/tmp/harbor-universe-public-ca.pem'


def preflight_script():
    return '''import json,ssl,urllib.request,urllib.error
ctx=ssl.create_default_context(cafile='/tmp/harbor-universe-public-ca.pem')
try:
    r=urllib.request.urlopen('https://dawnfm.gravarc.cn/v1/models',context=ctx,timeout=20)
    status=r.status
except urllib.error.HTTPError as e:
    status=e.code
except Exception as e:
    print(json.dumps({'tls_verified':False,'error_type':type(e).__name__}))
    raise SystemExit(1)
print(json.dumps({'tls_verified':True,'http_status':status}))
raise SystemExit(0 if status in (200,401,403) else 1)
'''


class UniverseCodex(CodexPreinstall):
    def __init__(self, *args, extra_env=None, **kwargs):
        env = dict(extra_env or {})
        env.update(SSL_CERT_FILE=REMOTE_CA, REQUESTS_CA_BUNDLE=REMOTE_CA,
                   CURL_CA_BUNDLE=REMOTE_CA)
        super().__init__(*args, extra_env=env, **kwargs)

    async def install(self, environment):
        await super().install(environment)
        bundle = ca_bundle()
        await environment.upload_file(bundle, REMOTE_CA)
        result = await self.exec_as_agent(environment,
            command='python3 -c ' + shlex.quote(preflight_script()), timeout_sec=30)
        if result.return_code != 0:
            raise RuntimeError('ModelEndpointTLSPreflightFailed: inspect runtime trust/network before retrying; no solver score')
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir/'runtime-trust.json').write_text(json.dumps(
            {**receipt(bundle), 'tls_verified': True, 'endpoint': 'https://dawnfm.gravarc.cn/v1/models'}))
        # Harbor collects the remote agent directory at the end of the trial.
        # Keep the receipt there as well so directory replacement retains it.
        await environment.upload_file(self.logs_dir/'runtime-trust.json', '/logs/agent/runtime-trust.json')
