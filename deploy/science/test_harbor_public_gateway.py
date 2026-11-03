import socket
import ssl
import subprocess
import tempfile
import threading
import concurrent.futures
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import urlopen

from evaluation_queue import W
from harbor_public_gateway import BoundedHTTPServer, request_upstream, relay_response, compatible_git_response


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, *args):
        pass


class GatewayTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=W/'.tmp', prefix='gateway-tls-test-')
        root = Path(cls.temp.name)
        cls.cert, cls.key = root/'cert.pem', root/'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-nodes', '-keyout', str(cls.key), '-out', str(cls.cert),
            '-days', '1', '-subj', '/CN=localhost'], check=True, capture_output=True)
        cls.key.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server = BoundedHTTPServer(('127.0.0.1', 0), Health, ssl_context=context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def health(self):
        with urlopen('https://127.0.0.1:'+str(self.server.server_port),
                context=ssl._create_unverified_context(), timeout=1) as response:
            self.assertEqual(response.read(), b'OK')

    def test_idle_tcp_health_check_does_not_block_https(self):
        with socket.create_connection(self.server.server_address, timeout=1):
            self.health()
            self.health()

    def test_incomplete_handshake_expires(self):
        self.server.handshake_timeout = 0.2
        with socket.create_connection(self.server.server_address, timeout=1) as idle:
            idle.settimeout(2)
            self.assertEqual(idle.recv(1), b'')
        self.health()


class ProxySessionTests(unittest.TestCase):
    def test_public_browser_uses_notification_polling_without_rewriting_issue_content(self):
        body=b'<script>\nnotificationSettings: {"EventSourceUpdateTime":10000,"MinTimeout":10000},\n</script>\n<p>"EventSourceUpdateTime":10000</p>'
        headers=[('Content-Type','text/html; charset=utf-8'),('ETag','old-value'),('Set-Cookie','session=fixture')]
        out_headers,out=compatible_git_response('git',headers,body)
        self.assertIn(b'notificationSettings: {"EventSourceUpdateTime":0,"MinTimeout":10000}',out)
        self.assertIn(b'<p>"EventSourceUpdateTime":10000</p>',out)
        self.assertNotIn(('ETag','old-value'),out_headers)
        self.assertIn(('Set-Cookie','session=fixture'),out_headers)
        self.assertEqual(compatible_git_response('harbor',headers,body),(headers,body))

    def test_eight_unending_event_streams_do_not_starve_normal_pages(self):
        release = threading.Event()
        class Streaming(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/user/events':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.end_headers();self.wfile.flush()
                    release.wait(8)
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '10')
                    self.end_headers();self.wfile.write(b'issue page')
            def log_message(self, *args): pass
        server = BoundedHTTPServer(('127.0.0.1',0),Streaming)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base = 'http://127.0.0.1:'+str(server.server_port)
        def fetch(path):
            return request_upstream({'target':'git','path':path,'method':'GET','headers':{}},
                                    b'','fixture',base,base,base)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        try:
            streams = [pool.submit(fetch, '/user/events') for _ in range(8)]
            page = pool.submit(fetch, '/team/repo/issues/13')
            meta,body = page.result(timeout=3)
            self.assertEqual((meta['status'],body),(200,b'issue page'))
            self.assertFalse(release.is_set())
            self.assertEqual([future.result()[0]['status'] for future in streams],[204]*8)
        finally:
            release.set();pool.shutdown(wait=True)
            server.shutdown();server.server_close();thread.join()

    def test_upstream_failure_finishes_request_without_replaying_post(self):
        with patch('harbor_public_gateway.request_upstream',side_effect=TimeoutError) as request:
            meta,body=relay_response({'target':'git','method':'POST'},b'data','fixture','a','b','c')
        request.assert_called_once()
        self.assertEqual(meta['status'],502)
        self.assertIn(b'Upstream request failed',body)

    def test_login_redirect_and_multiple_cookies_reach_browser(self):
        seen = {}
        class Login(BaseHTTPRequestHandler):
            def do_POST(self):
                seen['cookie'] = self.headers.get('Cookie')
                seen['body'] = self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(302)
                self.send_header('Location', '/team/repo')
                self.send_header('Set-Cookie', 'session=one; Path=/; HttpOnly')
                self.send_header('Set-Cookie', 'csrf=two; Path=/')
                self.end_headers()
            def log_message(self, *args): pass
        server = BoundedHTTPServer(('127.0.0.1',0),Login)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            backend = 'http://127.0.0.1:'+str(server.server_port)
            meta,body = request_upstream({'target':'git','path':'/user/login','method':'POST',
                'headers':{'Cookie':'csrf=before','Content-Type':'application/x-www-form-urlencoded'}},
                b'username=fixture','bridge-fixture','http://127.0.0.1:1',backend,'http://127.0.0.1:1')
            self.assertEqual(meta['status'],302)
            self.assertEqual([v for k,v in meta['headers'] if k.lower()=='set-cookie'],
                ['session=one; Path=/; HttpOnly','csrf=two; Path=/'])
            self.assertEqual(seen['cookie'],'csrf=before')
            self.assertEqual(seen['body'],b'username=fixture')
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_bridge_rejects_arbitrary_upstream(self):
        with self.assertRaises(KeyError):
            request_upstream({'target':'http://unexpected','path':'/','method':'GET','headers':{}},
                b'','fixture','a','b','c')


if __name__ == '__main__':
    unittest.main()
