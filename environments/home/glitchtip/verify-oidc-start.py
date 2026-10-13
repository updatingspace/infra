#!/usr/bin/env python3
"""Run inside GlitchTip: verify its real native OAuth start without a user session."""
import json
from http.cookies import SimpleCookie
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

client=build_opener(NoRedirect())
headers={'Host':'errors.updspace.com','X-Forwarded-Proto':'https','X-Forwarded-For':'127.0.0.1','Origin':'https://errors.updspace.com'}
base='http://127.0.0.1:8000'
def request(path,body=None):
    try:response=client.open(Request(base+path,body,headers=headers),timeout=10)
    except HTTPError as e:response=e
    return response

with request('/_allauth/browser/v1/config') as response:
    config=json.load(response)
    providers=config['data']['socialaccount']['providers']
    assert any(x['id']=='updspace' and x['client_id']=='observability' for x in providers)
with request('/_allauth/browser/v1/auth/session') as response:
    assert response.status==401
    cookies=SimpleCookie()
    for value in response.headers.get_all('Set-Cookie',[]):cookies.load(value)
    headers['Cookie']='; '.join(k+'='+v.value for k,v in cookies.items())
    headers['X-CSRFToken']=cookies['csrftoken'].value
headers['Content-Type']='application/x-www-form-urlencoded'
body=urlencode({'provider':'updspace','process':'login','callback_url':'https://errors.updspace.com/'}).encode()
with request('/_allauth/browser/v1/auth/provider/redirect',body) as response:
    assert response.status in (302,303), ('provider redirect',response.status,response.read()[:200])
    location=response.headers['Location']
    target=urlsplit(location);q=parse_qs(target.query)
    assert target.scheme=='https' and target.hostname=='id.updspace.com' and target.path=='/oauth/authorize'
    assert q['client_id']==['observability']
    assert q['redirect_uri']==['https://errors.updspace.com/accounts/oidc/updspace/login/callback/']
    assert q['code_challenge_method']==['S256'] and 'approval_prompt' not in q
try: response=client.open(location,timeout=10)
except HTTPError as e: response=e
with response:
    assert response.status==302 and urlsplit(response.headers['Location']).path=='/login', 'ID rejected native start'
print(json.dumps({'idLoginReached':True,'provider':'UpdSpace ID','config':200,'nativeRedirect':True,'pkce':'S256','userLoginPerformed':False}))
