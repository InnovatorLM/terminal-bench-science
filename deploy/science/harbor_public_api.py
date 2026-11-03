"""Bounded public Harbor uploads and private per-job results; no cloud secrets in HTTP."""
import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from evaluation_queue import W, atomic_json, digest_tree

REQUIRED = ('instruction.md', 'task.toml', 'environment/Dockerfile', 'tests/test.sh')
TERMINAL = {'completed', 'failed', 'interrupted'}
GUIDE_PATH = Path(__file__).resolve().parents[2] / 'docs/science-universe/harbor-public-service.zh-CN.md'


def guide_html():
    from markdown_it import MarkdownIt
    body = MarkdownIt('commonmark', {'html': False}).enable('table').render(
        GUIDE_PATH.read_text(encoding='utf-8'))
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>使用指南 · Harbor 云端评测</title>
<style>
*{box-sizing:border-box}body{font:16px/1.75 system-ui,sans-serif;color:#172334;background:#f5f7fb;margin:0}
nav,main{max-width:1000px;margin:auto;padding:24px}nav{display:flex;gap:24px;flex-wrap:wrap}
main{background:#fff;border:1px solid #dce2eb;border-radius:12px;margin-bottom:40px;padding:32px}
a{color:#145ee5;text-underline-offset:3px;overflow-wrap:anywhere}a:focus-visible{outline:2px solid #145ee5;outline-offset:4px}
h1,h2{line-height:1.35}h1{font-size:30px}h2{font-size:23px;margin-top:40px;border-bottom:1px solid #dce2eb;padding-bottom:8px}
p,li,td{overflow-wrap:anywhere}pre{overflow:auto;background:#edf1f6;border-radius:8px;padding:20px;line-height:1.6}
code{font-family:ui-monospace,monospace;font-size:14px}table{display:block;overflow:auto;border-collapse:collapse;margin:20px 0}
th,td{border:1px solid #dce2eb;padding:10px 14px;text-align:left}th{background:#edf1f6}li{margin:6px 0}
@media(max-width:600px){nav,main{padding:18px}main{border-radius:0}h1{font-size:26px}pre{padding:12px}}
@media print{body{background:#fff}nav{display:none}main{border:0;max-width:none}pre{white-space:pre-wrap}table{display:table}}
</style></head><body><nav aria-label="导航"><a href="/">← 上传并评测</a>
<a href="/guide.md">Markdown 原文</a></nav><main>''' + body + '</main></body></html>').encode('utf-8')


def extract(raw, target):
    """Manually extract regular files only; bound count, decompressed bytes, and paths."""
    target = Path(target)
    target.mkdir(mode=0o700)
    total = 0
    seen = set()

    def write(name, size, mode, directory, stream):
        nonlocal total
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or chr(0) in name:
            raise ValueError('Unsafe archive path')
        if not path.parts:
            if directory:
                return
            raise ValueError('Empty filename')
        key = path.as_posix()
        if key in seen or len(seen) >= 10000:
            raise ValueError('Duplicate archive member or too many files')
        seen.add(key)
        total += size
        if size < 0 or total > 128 * 1024 * 1024:
            raise ValueError('Expanded archive exceeds 128 MiB')
        out = target.joinpath(*path.parts)
        if directory:
            out.mkdir(mode=0o700, parents=True, exist_ok=True)
            return
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with out.open('xb') as dest:
            remaining = size
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError('Truncated archive member')
                dest.write(chunk)
                remaining -= len(chunk)
        out.chmod(0o700 if mode & 0o111 else 0o600)

    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for member in archive.infolist():
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or member.flag_bits & 1:
                    raise ValueError('Archive links, special files and encryption are forbidden')
                with archive.open(member) as stream:
                    write(member.filename, member.file_size, mode, member.is_dir(), stream)
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:*') as archive:
            for member in archive:
                if not (member.isfile() or member.isdir()):
                    raise ValueError('Archive links and special files are forbidden')
                stream = archive.extractfile(member) if member.isfile() else None
                try:
                    write(member.name, member.size, member.mode, member.isdir(), stream)
                finally:
                    if stream:
                        stream.close()
    candidates = [target] + [p for p in target.iterdir() if p.is_dir()]
    roots = [p for p in candidates if all((p / name).is_file() for name in REQUIRED)]
    if len(roots) != 1:
        raise ValueError('Upload one Harbor task with instruction.md, task.toml, environment/Dockerfile and tests/test.sh')
    return roots[0]


class Service:
    def __init__(self, settings, config_path):
        self.settings = settings
        self.config_path = Path(config_path).resolve()
        self.root = Path(settings['state']).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs = self.root / 'runs'
        self.runs.mkdir(mode=0o700, exist_ok=True)
        self.secret = Path(settings['secret_file']).read_bytes().strip()
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.child = None
        self.model_capacity = threading.BoundedSemaphore(8)

    def model_lease(self, token):
        if not token:
            raise PermissionError('Model credential required')
        with self.lock:
            for path in self.runs.glob('*/model-lease.json'):
                lease=json.loads(path.read_text())
                if hmac.compare_digest(token,lease['token']):
                    if lease.get('revoked') or lease['expires_at']<=time.time():
                        raise PermissionError('Model credential expired')
                    usage=path.with_name('model-usage.json')
                    count=json.loads(usage.read_text())['requests'] if usage.exists() else 0
                    if count>=lease['max_requests']:
                        raise PermissionError('Model request budget exhausted')
                    atomic_json(usage, {'requests':count+1})
                    return lease
        raise PermissionError('Unknown model credential')

    def token(self, rid):
        return hmac.new(self.secret, rid.encode(), hashlib.sha256).hexdigest()

    def authorized(self, rid, token):
        return bool(re.fullmatch(r'harbor-[a-f0-9]{32}', rid)) and hmac.compare_digest(self.token(rid), token)

    def requests(self):
        return [json.loads(p.read_text()) for p in self.runs.glob('*/request.json')]

    def status(self, rid):
        root = self.runs / rid
        if not (root / 'request.json').exists():
            raise FileNotFoundError()
        path = root / 'public.json'
        return json.loads(path.read_text())

    def submit(self, raw, ip, idempotency='', attempts=None):
        if self.config_path.exists() and not json.loads(self.config_path.read_text()).get('accepting_uploads',True):
            raise OverflowError('Service validation in progress; uploads temporarily paused')
        attempts = self.settings['default_attempts'] if attempts is None else attempts
        if attempts not in (1, 5):
            raise ValueError('attempts must be 1 or 5')
        digest = hashlib.sha256(raw).hexdigest()
        client = hmac.new(self.secret, ip.encode(), hashlib.sha256).hexdigest()
        if idempotency and not re.fullmatch(r'[A-Za-z0-9._-]{8,128}', idempotency):
            raise ValueError('Idempotency-Key must contain 8-128 letters, digits, dots, underscores or hyphens')
        identity = hmac.new(self.secret, (client + ':' + idempotency).encode(), hashlib.sha256).hexdigest() if idempotency else None
        with self.lock:
            previous = self.requests()
            for item in previous:
                if identity and item.get('idempotency') == identity:
                    if item['archive_sha256'] != digest or item['attempts'] != attempts:
                        raise ValueError('Idempotency-Key already belongs to different data')
                    return self.receipt(item['run_id'])
            pending = [q for q in previous if self.status(q['run_id'])['status'] not in TERMINAL]
            if len(pending) >= self.settings['max_pending'] or sum(q['client'] == client for q in pending) >= self.settings['max_pending_per_ip']:
                raise OverflowError('Queue is full; try later')
            daily = self.settings['max_jobs_per_day']
            if daily and sum(q['created_at'] >= time.time() - 86400 for q in previous) >= daily:
                raise OverflowError('Daily service capacity reached; try later')
            rid = 'harbor-' + uuid.uuid4().hex
            root = self.runs / rid
            root.mkdir(mode=0o700)
            try:
                unpacked = root / 'unpacked'
                task = extract(raw, unpacked)
                (root / 'source').mkdir(mode=0o700)
                name = rid
                final = root / 'source' / name
                shutil.move(str(task), final)
                if unpacked.exists():
                    shutil.rmtree(unpacked)
                from harbor_public_policy import validate_task
                validate_task(final)
                # Reference solutions are optional in Harbor. The maintained checker
                # expects an oracle entry point; add a nonexecuted placeholder only
                # to the execution copy and explicitly record the normalization.
                original_digest = digest_tree(final)
                normalized = not (final / 'solution/solve.sh').exists()
                if normalized:
                    (final / 'solution').mkdir(exist_ok=True, mode=0o700)
                    (final / 'solution/solve.sh').write_text('#!/bin/sh'+chr(10)+'# No reference solution supplied; solver evaluation only.'+chr(10)+'exit 0'+chr(10))
                    (final / 'solution/solve.sh').chmod(0o700)
                request = dict(run_id=rid, task_name=name, client=client, idempotency=identity,
                               archive_sha256=digest, task_sha256=digest_tree(final),
                               uploaded_task_sha256=original_digest, reference_placeholder=normalized,
                               attempts=attempts, created_at=time.time())
                atomic_json(root / 'request.json', request)
                atomic_json(root / 'public.json', dict(run_id=rid, status='queued', requested_trials=attempts,
                            created_at=request['created_at'], task_sha256=request['task_sha256'],
                            uploaded_task_sha256=original_digest, reference_placeholder=normalized))
                return self.receipt(rid)
            except BaseException:
                shutil.rmtree(root)
                raise

    def receipt(self, rid):
        return dict(run_id=rid, token=self.token(rid), status_url='/tasks/' + rid,
                    artifacts_url='/tasks/' + rid + '/artifacts', status=self.status(rid)['status'])

    def scheduler(self):
        while not self.stopping.wait(1):
            try:
                with self.lock:
                    pending = sorted((r for r in self.requests() if self.status(r['run_id'])['status'] not in TERMINAL), key=lambda x:x['created_at'])
                    active = False
                    for req in pending:
                        root = self.runs / req['run_id']
                        pid_file = root / 'worker.json'
                        if not pid_file.exists():
                            continue
                        pid = json.loads(pid_file.read_text())['pid']
                        try:
                            cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
                            alive = b'harbor_public_worker.py' in cmdline and str(root).encode() in cmdline
                        except OSError:
                            alive = False
                        if alive:
                            active = True
                        else:
                            result = self.status(req['run_id'])
                            result.update(status='interrupted', error='Worker stopped before publishing a final result; operator review required')
                            atomic_json(root / 'public.json', result)
                    if self.child is not None and self.child.poll() is not None:
                        self.child = None
                    if active:
                        continue
                    queued = [r for r in pending if self.status(r['run_id'])['status'] == 'queued']
                    if not queued:
                        continue
                    root = self.runs / queued[0]['run_id']
                    with (root / 'worker.log').open('a') as log:
                        self.child = subprocess.Popen([str(W / 'acs-harbor-test/.venv/bin/python'),
                            str(Path(__file__).with_name('harbor_public_worker.py')), '--run', str(root),
                            '--settings', str(self.config_path)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    atomic_json(root / 'worker.json', {'pid': self.child.pid, 'started_at': time.time()})
            except Exception:
                import traceback
                traceback.print_exc()


PAGE = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Harbor 云端评测</title><style>body{font:16px system-ui;max-width:900px;margin:60px auto;padding:0 24px;background:#f5f7fb;color:#172334}h1{font-size:32px}section{background:white;padding:24px;margin:20px 0;border:1px solid #dce2eb;border-radius:12px}button{padding:12px 24px;background:#145ee5;color:white;border:0;border-radius:8px;cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf1f6;padding:16px}input,select{margin:12px 0}small{color:#586579}</style>
<h1>Harbor 云端评测</h1><p>上传题包，由 Codex 在 ACS 独立环境中执行，返回 verifier 评分和轨迹。</p>
<p><a href="/guide">使用指南：题包要求、API 示例与结果说明</a></p>
<section><h2>提交题目</h2><p>支持 .tar、.tar.gz、.zip，最大 100 MiB。题包包含 instruction.md、task.toml、environment/Dockerfile、tests/test.sh；solution/solve.sh 可选。</p>
<input id="file" type="file" accept=".tar,.gz,.tgz,.zip"><br><label>独立尝试次数 <select id="attempts"><option value="5">5 次（并行）</option><option value="1">1 次</option></select></label><br><button id="submit">上传并评测</button><p><small>Codex + DawnFM / DeepSeek。上传后保留任务凭证；服务不会公开其他用户的题包或结果。单次执行最长 4 小时，构建另计。服务有队列和每日容量限制。</small></p></section>
<section><h2>任务结果</h2><p id="status">尚未提交</p><button id="receipt" hidden>保存任务凭证</button> <button id="download" hidden>下载结果与轨迹</button><p><small>任务凭证用于查询和下载，请私密保存。刷新页面会恢复最近一次任务；更早的任务可用保存的凭证通过 API 查询。</small></p><pre id="out"></pre></section>
<script>
let job;const out=document.querySelector('#out'),status=document.querySelector('#status');
async function poll(){if(!job)return;document.querySelector('#receipt').hidden=false;try{let r=await fetch(job.status_url,{headers:{Authorization:'Bearer '+job.token}});let d=await r.json();if(!r.ok)throw Error(d.error||('HTTP '+r.status));status.textContent='任务 '+job.run_id+' · '+d.status;out.textContent=JSON.stringify(d,null,2);document.querySelector('#download').hidden=!d.artifacts_url;if(!['completed','failed','interrupted'].includes(d.status))setTimeout(poll,5000)}catch(e){status.textContent=String(e);setTimeout(poll,10000)}}
document.querySelector('#submit').onclick=async()=>{let f=document.querySelector('#file').files[0];if(!f){alert('请选择题包');return}let b=document.querySelector('#submit');b.disabled=true;status.textContent='上传中…';try{let r=await fetch('/tasks',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Harbor-Attempts':document.querySelector('#attempts').value,'Idempotency-Key':crypto.randomUUID()},body:f});let d=await r.json();if(!r.ok)throw Error(d.error);job=d;localStorage.setItem('harbor-job',JSON.stringify(job));out.textContent='请保存任务凭证：'+JSON.stringify(job,null,2);poll()}catch(e){status.textContent=String(e)}finally{b.disabled=false}};
document.querySelector('#download').onclick=async()=>{let r=await fetch(job.artifacts_url,{headers:{Authorization:'Bearer '+job.token}});if(!r.ok){alert('下载失败');return}let a=document.createElement('a');a.href=URL.createObjectURL(await r.blob());a.download=job.run_id+'.zip';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
document.querySelector('#receipt').onclick=()=>{let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(job,null,2)],{type:'application/json'}));a.download=job.run_id+'-receipt.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
try{job=JSON.parse(localStorage.getItem('harbor-job'));if(job)poll()}catch(e){}
</script></html>'''


def serve(settings, config_path, run_scheduler=True):
    svc = Service(settings, config_path)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def setup(self):
            super().setup()
            self.connection.settimeout(120)
        def log_message(self, *args):
            pass
        def send(self, code, body, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            if self.path == '/model/v1/responses':
                self.model_request();return
            if self.path != '/tasks':
                self.send(404, {'error': 'Not found'});return
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= settings['max_upload_bytes'] * 1.4 or self.headers.get('Transfer-Encoding'):
                    self.close_connection = True
                    self.send(413, {'error': 'Upload exceeds limit or Content-Length is missing'});return
                raw = self.rfile.read(n)
                if len(raw) != n:
                    raise ValueError('Incomplete upload')
                attempts = int(self.headers.get('X-Harbor-Attempts', settings['default_attempts']))
                if self.headers.get('Content-Type','').split(';')[0] == 'application/json':
                    body = json.loads(raw)
                    if set(body) - {'filename','data','attempts'}:
                        raise ValueError('Only filename, data and attempts are accepted')
                    attempts = body.get('attempts', attempts)
                    raw = base64.b64decode(body['data'], validate=True)
                if len(raw) > settings['max_upload_bytes']:
                    raise ValueError('Upload exceeds 100 MiB')
                client=self.client_address[0]
                bridge_file=settings.get('bridge_token_file')
                if bridge_file and hmac.compare_digest(self.headers.get('X-Harbor-Bridge',''),Path(bridge_file).read_text().strip()):
                    client=self.headers.get('X-Harbor-Client',client)
                result = svc.submit(raw, client, self.headers.get('Idempotency-Key',''), attempts)
                self.send(202, result)
            except OverflowError as e:
                self.send(429, {'error': str(e)})
            except (ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile, OSError) as e:
                self.send(400, {'error': str(e).replace(str(W), '[workspace]')[:500]})
        def model_request(self):
            acquired=False
            try:
                lease=svc.model_lease(self.headers.get('Authorization','').removeprefix('Bearer '))
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=8*1024*1024:
                    raise ValueError('Invalid model request size')
                body=json.loads(self.rfile.read(n))
                if body.get('model')!=lease['model']:
                    raise ValueError('Model is fixed for this evaluation')
                body['store']=False
                acquired=svc.model_capacity.acquire(timeout=3)
                if not acquired:
                    self.send(429, {'error':'Model capacity busy'});return
                import requests
                from selftest_logs import sanitize
                upstream=os.environ['OPENAI_BASE_URL'].rstrip('/')+'/responses'
                response=requests.post(upstream,json=body,
                    headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY']},timeout=(10,130),allow_redirects=False)
                if not response.ok:
                    self.send(response.status_code,{'error':'Upstream model request failed','status':response.status_code});return
                data=response.content
                if len(data)>64*1024*1024:raise ValueError('Model response too large')
                # Upstream response body is forwarded, never the upstream key/headers.
                data=data.replace(os.environ['OPENAI_API_KEY'].encode(),b'[REDACTED]')
                self.send(response.status_code,data,response.headers.get('Content-Type','application/json'))
            except PermissionError as e:
                self.send(403,{'error':str(e)})
            except Exception:
                self.send(502,{'error':'Model relay request failed'})
            finally:
                if acquired:svc.model_capacity.release()
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/':
                self.send(200, PAGE.encode(), 'text/html; charset=utf-8');return
            if path in {'/guide', '/guide/'}:
                self.send(200, guide_html(), 'text/html; charset=utf-8');return
            if path == '/guide.md':
                self.send(200, GUIDE_PATH.read_bytes(), 'text/plain; charset=utf-8');return
            if path == '/health':
                current=json.loads(svc.config_path.read_text()) if svc.config_path.exists() else settings
                self.send(200, {'status':'ok','service':'harbor-public','agent':'codex',
                    'default_attempts':settings['default_attempts'],
                    'accepting_uploads':current.get('accepting_uploads',True)});return
            match = re.fullmatch(r'/tasks/(harbor-[a-f0-9]{32})(/artifacts)?', path)
            if not match:
                self.send(404, {'error':'Not found'});return
            rid, artifact = match.groups()
            auth = self.headers.get('Authorization','').removeprefix('Bearer ')
            if not svc.authorized(rid, auth):
                self.send(403, {'error':'Valid task token required'});return
            try:
                if not artifact:
                    self.send(200, svc.status(rid));return
                p = svc.runs / rid / 'artifacts.zip'
                if not p.exists():
                    self.send(409, {'error':'Artifacts are not ready'});return
                self.send_response(200)
                self.send_header('Content-Type','application/zip')
                self.send_header('Content-Length',str(p.stat().st_size))
                self.send_header('Content-Disposition','attachment; filename="'+rid+'.zip"')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                with p.open('rb') as f:
                    shutil.copyfileobj(f,self.wfile)
            except FileNotFoundError:
                self.send(404, {'error':'Not found'})
    server = ThreadingHTTPServer((settings['host'], settings['port']), Handler)
    if run_scheduler:
        threading.Thread(target=svc.scheduler, daemon=True).start()
    return server, svc


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--settings', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    server, service = serve(json.loads(args.settings.read_text()), args.settings)
    try:
        server.serve_forever()
    finally:
        service.stopping.set()
        server.server_close()
