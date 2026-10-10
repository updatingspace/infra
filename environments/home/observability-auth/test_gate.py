"""Exercise the real Caddy handlers against isolated session/ID/backend fixtures."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.environ.get('CADDY_BINARY'), 'Set CADDY_BINARY for the real edge integration test')
class GateTest(unittest.TestCase):
    def test_session_authorization_and_header_boundary(self):
        state = {'policy': 204, 'checks': 0}
        picture = 'https://storage.updspace.com/updspace-id-media-ab88348a/avatars/fixture.png?signature=fixture'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.server.role == 'session':
                    self.assert_path('/oauth2/auth')
                    if self.headers.get('Cookie') != 'fixture=valid':
                        self.send_response(401); self.end_headers(); return
                    self.send_response(202)
                    self.send_header('X-Auth-Request-Access-Token', 'server-validated-token')
                    self.send_header('Set-Cookie', 'chunk1=one; Secure; HttpOnly')
                    self.send_header('Set-Cookie', 'chunk2=two; Secure; HttpOnly')
                    self.end_headers()
                elif self.server.role == 'id':
                    self.assert_path('/oauth/observability-access')
                    assert self.headers.get('Authorization') == 'Bearer server-validated-token'
                    assert not self.headers.get('Cookie')
                    state['checks'] += 1
                    self.send_response(state['policy'])
                    assert self.headers.get('X-Forwarded-Host') == 'grafana.updspace.com'
                    if self.headers.get('X-Forwarded-Uri') == '/avatar/fixture' and state['policy'] == 204:
                        self.send_header('X-Observability-Avatar', picture)
                    self.end_headers()
                else:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps(dict(self.headers)).encode())
            def assert_path(self, expected): assert self.path == expected, self.path
        servers = []
        for role in ('session', 'id', 'backend'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.role = role
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        caddy = (ROOT.parent/'edge/Caddyfile').read_text()
        snippet = caddy[caddy.index('(id_access) {'):caddy.index('\n\n\n{$PANEL_DOMAIN}')]
        snippet = snippet.replace('oauth2-proxy.observability-auth.svc.cluster.local:4180', f'127.0.0.1:{servers[0].server_port}')
        snippet = snippet.replace('id.updspace-id.svc.cluster.local:8089', f'127.0.0.1:{servers[1].server_port}')
        grafana = caddy.split('grafana.updspace.com {', 1)[1].split('\n}', 1)[0]
        grafana = grafana.replace('  import monitoring_headers', '')
        grafana = grafana.replace('grafana.observability.svc.cluster.local:3000', f'127.0.0.1:{servers[2].server_port}')
        config = '{ admin off\n auto_https off\n}\n' + snippet + f'\nhttp://:{port} {{\n bind 127.0.0.1' + grafana + '\n}'
        def request(headers, path='/api/private'):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                conn.request('GET', path, headers=headers | {'Host':'grafana.updspace.com'})
                response = conn.getresponse(); body = response.read()
                return response.status, response.getheaders(), body
            finally: conn.close()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'Caddyfile'; path.write_text(config)
            log = open(Path(temporary)/'caddy.log', 'w+')
            process = subprocess.Popen([os.environ['CADDY_BINARY'], 'run', '--config', str(path), '--adapter', 'caddyfile'], stdout=log, stderr=log)
            try:
                for _ in range(50):
                    try: request({}); break
                    except ConnectionRefusedError: time.sleep(.05)
                else:
                    log.seek(0); self.fail(log.read())
                spoof = {'X-Observability-Token':'forged', 'X-Auth-Request-Access-Token':'forged',
                    'X-Forwarded-User':'admin', 'Authorization':'Bearer forged',
                    'X-Observability-Avatar':'https://evil.invalid', 'X-Forwarded-Uri':'/avatar/fixture'}
                status, headers, _ = request(spoof)
                self.assertEqual(status, 302)
                self.assertIn(('Location', '/oauth2/start?rd=/'), headers)
                self.assertEqual(state['checks'], 0)
                status, headers, body = request(spoof | {'Cookie':'fixture=valid'})
                self.assertEqual(status, 200)
                self.assertEqual(len([x for x in headers if x[0].lower() == 'set-cookie']), 2)
                received = {key.lower():value for key,value in json.loads(body).items()}
                for name in ('authorization', 'x-observability-token', 'x-auth-request-access-token', 'x-forwarded-user', 'x-observability-avatar'):
                    self.assertNotIn(name, received)
                for policy in (403, 401, 503, 204):
                    state['policy'] = policy
                    self.assertEqual(request({'Cookie':'fixture=valid'})[0], 200 if policy == 204 else policy)
                self.assertEqual(state['checks'], 5, 'ID must run on every request without an authorization cache')
                status, headers, _ = request(spoof | {'Cookie':'fixture=valid'}, '/avatar/fixture')
                self.assertEqual(status, 302)
                self.assertIn(('Location', picture), headers)
                self.assertFalse(any(name.lower() == 'x-observability-avatar' for name, _ in headers))
                self.assertEqual(request(spoof, '/avatar/fixture')[0], 302)
                self.assertEqual(request(spoof | {'Cookie':'fixture=valid'}, '/avatar/other-user')[0], 200)
                for policy in (403, 401, 503):
                    state['policy'] = policy
                    self.assertEqual(request(spoof | {'Cookie':'fixture=valid'}, '/avatar/fixture')[0], policy)
            finally:
                process.terminate(); process.wait(timeout=10); log.close()
                for server in servers: server.shutdown(); server.server_close()


if __name__ == '__main__': unittest.main()
