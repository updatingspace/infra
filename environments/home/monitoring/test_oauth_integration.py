"""Real Grafana, disposable database and isolated OAuth provider; no production users."""
import hashlib
import base64
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler
import uuid

from build import build


@unittest.skipUnless(os.environ.get('GRAFANA_IMAGE'), 'Set GRAFANA_IMAGE for isolated native OAuth acceptance')
class GrafanaOAuthTest(unittest.TestCase):
    def test_auto_signup_exact_role_and_stable_subject(self):
        profile = {'sub':'fixture-allowed','preferred_username':'fixture-login','email':'fixture@example.invalid','name':'OAuth Fixture',
            'email_verified':True,'master_flags':{'is_staff':True,'system_admin':True,
                'status':'active','banned':False,'suspended':False}}
        state = {'profile':profile, 'challenge':None}
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path.startswith('/authorize?'):
                    query = parse_qs(urlsplit(self.path).query)
                    assert query['code_challenge_method'] == ['S256']
                    assert 'approval_prompt' not in query
                    state['challenge'] = query['code_challenge'][0]
                    target = query['redirect_uri'][0]+'?'+urlencode({'state':query['state'][0],'code':'fixture-code'})
                    self.send_response(302); self.send_header('Location',target); self.end_headers()
                elif self.path == '/userinfo':
                    assert self.headers['Authorization'] == 'Bearer fixture-token'
                    self.json(state['profile'])
                else:self.send_error(404)
            def do_POST(self):
                assert self.path == '/token'
                query = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                assert query['client_id'] == ['observability'] and query['client_secret'] == ['fixture-secret']
                assert query['code'] == ['fixture-code']
                challenge = base64.urlsafe_b64encode(hashlib.sha256(query['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                assert challenge == state['challenge']
                self.json({'access_token':'fixture-token','token_type':'Bearer','expires_in':3600,'refresh_token':'fixture-refresh'})
            def json(self,data):
                body=json.dumps(data).encode();self.send_response(200)
                self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
        provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        threading.Thread(target=provider.serve_forever,daemon=True).start()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        resources,_=build()
        deployment=next(x for x in resources if x['kind']=='Deployment' and x['metadata']['name']=='grafana')
        env={x['name']:x['value'] for x in deployment['spec']['template']['spec']['containers'][0]['env'] if 'value' in x}
        env.update({'GF_PATHS_DATA':'/var/lib/grafana','GF_SERVER_HTTP_ADDR':'127.0.0.1','GF_SERVER_HTTP_PORT':str(port),
            'GF_SERVER_ROOT_URL':base,'GF_SECURITY_COOKIE_SECURE':'false','GF_SECURITY_ADMIN_PASSWORD':'fixture-breakglass',
            'GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET':'fixture-secret',
            'GF_AUTH_GENERIC_OAUTH_AUTH_URL':f'http://127.0.0.1:{provider.server_port}/authorize',
            'GF_AUTH_GENERIC_OAUTH_TOKEN_URL':f'http://127.0.0.1:{provider.server_port}/token',
            'GF_AUTH_GENERIC_OAUTH_API_URL':f'http://127.0.0.1:{provider.server_port}/userinfo',
            'GF_PLUGINS_PREINSTALL_DISABLED':'true'})
        name='infra-grafana-oauth-test-'+uuid.uuid4().hex[:8]
        command=['docker','run','-d','--name',name,'--network=host','--memory=768m','--cpus=1',
            '--tmpfs','/var/lib/grafana:rw,uid=472,gid=0,mode=0700,size=128m']
        for key,value in env.items():command += ['-e',key+'='+value]
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        def browser():return build_opener(HTTPCookieProcessor(CookieJar()),NoRedirect())
        def get(client,url):
            try:response=client.open(url,timeout=4)
            except HTTPError as e:response=e
            try: return response.code,dict(response.headers),response.read()
            finally: response.close()
        def login():
            client=browser();url=base+'/login'
            for _ in range(6):
                status,headers,body=get(client,url)
                if urlsplit(url).path=='/login/generic_oauth' and 'code=' in url:
                    return client
                self.assertIn(status,(302,307),body[:200])
                target=headers['Location'];url=target if target.startswith('http') else base+target
            self.fail('OAuth callback not reached')
        try:
            subprocess.run(command+[os.environ['GRAFANA_IMAGE']],check=True,capture_output=True)
            for _ in range(150):
                try:
                    if get(browser(),base+'/api/health')[0]==200:break
                except (URLError,TimeoutError,ConnectionError):pass
                time.sleep(.2)
            else:self.fail('Grafana did not become ready')
            for staff,admin in [(False,False),(True,False),(False,True)]:
                state['profile']=profile | {'sub':f'fixture-denied-{staff}-{admin}',
                    'master_flags':profile['master_flags'] | {'is_staff':staff,'system_admin':admin}}
                self.assertEqual(get(login(),base+'/api/user')[0],401)
            state['profile']=profile
            client=login();status,_,body=get(client,base+'/api/user')
            self.assertEqual(status,200,body)
            user=json.loads(body);self.assertEqual(user['login'],profile['preferred_username']);self.assertFalse(user['isGrafanaAdmin'])
            status,_,body=get(client,base+'/api/user/orgs')
            self.assertEqual(status,200);self.assertEqual(json.loads(body)[0]['role'],'Admin')
            state['profile']=profile | {'preferred_username':'fixture-renamed','email':'fixture-renamed@example.invalid','name':'Renamed Fixture'}
            status,_,body=get(login(),base+'/api/user');renamed=json.loads(body)
            self.assertEqual(status,200);self.assertEqual(renamed['id'],user['id'])
            self.assertEqual(renamed['email'],'fixture-renamed@example.invalid')
            self.assertEqual(renamed['name'],'Renamed Fixture')
            self.assertEqual(renamed['login'],'fixture-renamed')
        finally:
            subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            provider.shutdown();provider.server_close()


if __name__ == '__main__': unittest.main()
