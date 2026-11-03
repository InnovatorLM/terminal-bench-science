"""Public edge with an authenticated reverse connection; no model or cloud credentials."""
import argparse
import concurrent.futures
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import ssl
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

LIMIT = 147 * 1024 * 1024

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length', 'server', 'date'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def compatible_git_response(target, headers, body):
    """Tell Forgejo's public browser client to use its supported polling fallback."""
    if target != 'git' or not any(k.lower() == 'content-type' and 'text/html' in v.lower() for k,v in headers):
        return headers, body
    # Restrict the rewrite to the generated window.config property line, not issue text.
    rewritten, count = re.subn(
        rb'(?m)^(\s*notificationSettings:\s*\{[^\n}]*"EventSourceUpdateTime"\s*:\s*)-?\d+',
        rb'\g<1>0', body, count=1)
    if count:
        headers = [(k,v) for k,v in headers if k.lower() not in ('etag','content-md5','content-length','last-modified')]
        return headers, rewritten
    return headers, body


def request_upstream(meta, data, token, backend, git_backend, report_backend):
    target = meta.get('target', 'harbor')
    upstream = {'harbor': backend, 'git': git_backend, 'report': report_backend}[target]
    path = meta['path']
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('Invalid request path')
    headers = dict(meta['headers'])
    headers['X-Harbor-Bridge'] = token
    request = Request(upstream + path,
        data=data if meta['method'] not in ('GET', 'HEAD') else None,
        headers=headers, method=meta['method'])
    try:
        response = build_opener(NoRedirect()).open(request, timeout=130)
    except HTTPError as error:
        response = error
    with response:
        if response.headers.get_content_type() == 'text/event-stream':
            # This bridge transports complete responses, not unbounded streams.
            # A stale Forgejo tab may still request SSE after polling is enabled.
            # Close immediately; HTTP 204 tells EventSource not to reconnect.
            return {'status': 204, 'headers': [('Cache-Control', 'no-store')]}, b''
        # Preserve separate Set-Cookie fields and let the browser follow redirects.
        headers,body = compatible_git_response(target, list(response.headers.items()), response.read())
        return {'status': response.status, 'headers': headers}, body


def relay_response(meta, data, token, backend, git_backend, report_backend):
    """Complete failed requests too; never strand the public future or replay writes."""
    try:
        return request_upstream(meta, data, token, backend, git_backend, report_backend)
    except Exception as error:
        # Do not log URLs, headers, credentials, or upstream exception messages.
        print(json.dumps({'event': 'upstream_request_failed',
                          'target': meta.get('target', 'harbor'),
                          'error_class': type(error).__name__}), flush=True)
        return {'status': 502, 'headers': [('Content-Type', 'text/plain; charset=utf-8'),
                                          ('Cache-Control', 'no-store')]}, b'Upstream request failed; please retry.'


class BoundedHTTPServer(ThreadingHTTPServer):
    """Accept promptly even when a TCP health check never starts TLS."""
    request_queue_size = 64
    handshake_timeout = 5

    def __init__(self, address, handler, ssl_context=None):
        self.ssl_context = ssl_context
        self.connection_slots = threading.BoundedSemaphore(64)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            if self.ssl_context is not None:
                request.settimeout(self.handshake_timeout)
                request = self.ssl_context.wrap_socket(request, server_side=True)
            super().process_request_thread(request, client_address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            self.connection_slots.release()


def pack(meta, body):
    header = json.dumps(meta).encode()
    return struct.pack('!I',len(header))+header+body


def unpack(data):
    if len(data)<4:raise ValueError('Invalid envelope')
    size=struct.unpack('!I',data[:4])[0]
    if not 0<size<65536:raise ValueError('Invalid header size')
    return json.loads(data[4:4+size]),data[4+size:]


def gateway(token, cert_dir=None, public_host=None):
    pending=queue.Queue(maxsize=16)
    answers={}
    lock=threading.Lock()
    # Bound request memory and thread count at the network-facing edge.
    capacity=threading.BoundedSemaphore(12)
    large_uploads=threading.BoundedSemaphore(1)
    last_pull=[0]

    class Base(BaseHTTPRequestHandler):
        def setup(self):
            super().setup();self.connection.settimeout(120)
        def log_message(self,*args):pass
        def reply(self,status,body,headers=None):
            self.send_response(status)
            values = (headers or {}).items() if isinstance(headers, dict) else (headers or [])
            for k,v in values:
                if k.lower() not in HOP_HEADERS and '\r' not in v and '\n' not in v:
                    self.send_header(k,v)
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':self.wfile.write(body)

    class Public(Base):
        def handle_request(self):
            if self.path=='/gateway-health':
                self.reply(200,json.dumps({'status':'ok','backend_connected':time.time()-last_pull[0]<45}).encode(),{'Content-Type':'application/json'});return
            if self.path.startswith('/.well-known/acme-challenge/') and cert_dir:
                name=self.path.rsplit('/',1)[-1]
                if name and all(c.isalnum() or c in '-_' for c in name):
                    path=Path(cert_dir)/'challenges'/name
                    if path.is_file():self.reply(200,path.read_bytes());return
                self.reply(404,b'Not found');return
            if public_host and (self.path == '/git' or self.path.startswith('/git/')) and self.server.server_port != 8444:
                self.send_response(302)
                self.send_header('Location', 'https://' + public_host + ':3443/' + self.path.removeprefix('/git').lstrip('/'))
                self.send_header('Content-Length', '0');self.end_headers();return
            if public_host and self.server.server_port==8080:
                self.send_response(308)
                self.send_header('Location','https://'+public_host+self.path)
                self.send_header('Content-Length','0');self.end_headers();return
            if not capacity.acquire(blocking=False):self.reply(429,b'Service busy');return
            large=False
            try:
                if time.time()-last_pull[0]>45:self.reply(503,b'Backend reconnecting; retry shortly');return
                n=int(self.headers.get('Content-Length','0'))
                if n<0 or n>LIMIT or self.headers.get('Transfer-Encoding'):
                    self.reply(413,b'Invalid request size');return
                if n>16*1024*1024:
                    large=large_uploads.acquire(blocking=False)
                    if not large:self.reply(429,b'Another large upload is in progress');return
                body=self.rfile.read(n) if n else b''
                if len(body)!=n:self.reply(400,b'Incomplete request');return
                rid=uuid.uuid4().hex
                result=concurrent.futures.Future()
                target = 'git' if self.server.server_port == 8444 else ('report' if self.path == '/universe' or self.path.startswith('/universe/') else 'harbor')
                permitted = {'content-type','authorization','idempotency-key','x-harbor-attempts'}
                if target == 'git':
                    permitted.update({'cookie','accept','accept-language','origin','referer','x-requested-with','x-csrf-token','range','if-none-match','if-modified-since','git-protocol'})
                headers={k:v for k,v in self.headers.items() if k.lower() in permitted}
                headers['X-Harbor-Client']=self.client_address[0]
                if target == 'git' and public_host:
                    headers.update({'Host':public_host+':3443','X-Forwarded-Proto':'https','X-Forwarded-Host':public_host+':3443'})
                meta={'id':rid,'method':self.command,'path':self.path,'headers':headers,'target':target}
                with lock:answers[rid]=result
                try:
                    pending.put((rid,pack(meta,body)),timeout=1)
                    value=result.result(timeout=180)
                    self.reply(value[0]['status'],value[1],value[0]['headers'])
                except (queue.Full,concurrent.futures.TimeoutError):self.reply(504,b'Backend timeout')
                finally:
                    with lock:answers.pop(rid,None)
            except (ValueError,OSError):
                self.close_connection=True
            finally:
                if large:large_uploads.release()
                capacity.release()
        do_GET=handle_request
        do_POST=handle_request
        do_HEAD=handle_request
        do_PUT=handle_request
        do_PATCH=handle_request
        do_DELETE=handle_request
        do_OPTIONS=handle_request

    class Bridge(Base):
        def authorized(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.reply(403,b'Forbidden');return False
            return True
        def do_GET(self):
            if not self.authorized():return
            if self.path!='/pull':self.reply(404,b'Not found');return
            last_pull[0]=time.time()
            until=time.monotonic()+20
            while time.monotonic()<until:
                try:rid,data=pending.get(timeout=max(.1,until-time.monotonic()))
                except queue.Empty:break
                with lock:valid=rid in answers
                if valid:self.reply(200,data);return
            self.reply(204,b'')
        def do_POST(self):
            if not self.authorized():return
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<512*1024*1024:self.reply(413,b'Invalid size');return
            rid=self.path.removeprefix('/reply/')
            value=unpack(self.rfile.read(n))
            with lock:future=answers.get(rid)
            if future and not future.done():future.set_result(value)
            self.reply(200,b'OK')

    public=BoundedHTTPServer(('0.0.0.0',8080),Public)
    bridge=BoundedHTTPServer(('0.0.0.0',9000),Bridge)
    threading.Thread(target=bridge.serve_forever,daemon=True).start()
    if cert_dir and (Path(cert_dir)/'fullchain.pem').exists():
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(Path(cert_dir)/'fullchain.pem'),str(Path(cert_dir)/'key.pem'))
        secure=BoundedHTTPServer(('0.0.0.0',8443),Public,ssl_context=ctx)
        threading.Thread(target=secure.serve_forever,daemon=True).start()
        git_secure=BoundedHTTPServer(('0.0.0.0',8444),Public,ssl_context=ctx)
        threading.Thread(target=git_secure.serve_forever,daemon=True).start()
        def refresh_certificate():
            stamp=None
            while True:
                try:
                    current=(Path(cert_dir)/'fullchain.pem').read_bytes()
                    if current!=stamp:
                        ctx.load_cert_chain(str(Path(cert_dir)/'fullchain.pem'),str(Path(cert_dir)/'key.pem'))
                        stamp=current
                except OSError:
                    pass
                time.sleep(60)
        threading.Thread(target=refresh_certificate,daemon=True).start()
    public.serve_forever()


def connector(token, bridge, backend, git_backend='http://127.0.0.1:3000', report_backend='http://127.0.0.1:8791'):
    def loop():
        while True:
            try:
                req=Request(bridge+'/pull',headers={'Authorization':'Bearer '+token})
                with urlopen(req,timeout=30) as r:
                    if r.status==204:continue
                    meta,data=unpack(r.read())
                response_meta,response_body=relay_response(meta,data,token,backend,git_backend,report_backend)
                response=pack(response_meta,response_body)
                req=Request(bridge+'/reply/'+meta['id'],data=response,headers={'Authorization':'Bearer '+token})
                with urlopen(req,timeout=130) as r:r.read()
            except Exception:
                time.sleep(2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _:loop(),range(8)))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['gateway','connector'])
    p.add_argument('--token-file',required=True);p.add_argument('--bridge',default='http://127.0.0.1:19090')
    p.add_argument('--backend',default='http://127.0.0.1:8790');p.add_argument('--cert-dir');p.add_argument('--public-host')
    p.add_argument('--git-backend',default='http://127.0.0.1:3000')
    p.add_argument('--report-backend',default='http://127.0.0.1:8791')
    a=p.parse_args();token=Path(a.token_file).read_text().strip()
    if a.mode=='gateway':gateway(token,a.cert_dir,a.public_host)
    else:connector(token,a.bridge,a.backend,a.git_backend,a.report_backend)
