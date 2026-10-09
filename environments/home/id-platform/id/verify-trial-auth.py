#!/usr/bin/python3
# Never run against the final cutover database.
import base64, hashlib, hmac, http.client, json, os, subprocess, time, uuid
from http.cookies import SimpleCookie
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if not Path('/opt/updspace-id/TRIAL_ONLY').is_file():
    raise SystemExit('Synthetic authentication acceptance is allowed only on an explicitly marked trial database')

K=['k3s','kubectl'];NS=['-n','updspace-id']
def sql(query):
    p=subprocess.run(K+['-n','updspace-data','exec','-i','id-ydb-0','--','/ydb','--endpoint','grpcs://localhost:2135','--database','/local','--ca-file','/ydb_certs/ca.pem','--user','root','--password-file','/ydb_admin/password','--no-discovery','sql','--format','json-unicode','-f','-'],input=query.encode(),capture_output=True)
    if p.returncode:raise RuntimeError(p.stderr.decode())
    return p.stdout.decode()

svc=json.loads(subprocess.check_output(K+NS+['get','svc','id','-o','json']))['spec']['clusterIP']
account=-2100000000; identity=str(uuid.uuid4());email='id-migration-acceptance@example.invalid'
assert json.loads(sql(f'SELECT COUNT(*) AS n FROM auth_user WHERE id={account};'))['n']==0
password='Synthetic пароль 🔐 with unicode and more than 72 bytes Synthetic пароль 🔐 with unicode and more than 72 bytes '
hashed='argon2$argon2id$v=19$m=102400,t=2,p=8$U3ludGhldGljR29sZGVuU2FsdDEyMw$Q/uhIlhHnraeVEMP4b/SvQx5Gjb04zC0bEmIq6OPnUo'
sql(f"""
UPSERT INTO auth_user (id,password,is_active,username,first_name,last_name,email,is_staff,is_superuser,date_joined) VALUES ({account},'{hashed}',true,'id-migration-acceptance','','','{email}',false,false,CurrentUtcDatetime());
UPSERT INTO accounts_accountemaillookup (user_id,email_key) VALUES ({account},'{email}');
UPSERT INTO account_emailaddress (id,user_id,email,verified,primary) VALUES ({account},{account},'{email}',true,true);
UPSERT INTO usid_user (user_id,username,display_name,email,email_verified,status,system_admin,created_at) VALUES (Uuid('{identity}'),'id-migration-acceptance','Synthetic migration test','{email}',true,'active',false,CurrentUtcDatetime());
UPSERT INTO accounts_accountidentity (user_id,identity_id,public_subject,created_at) VALUES ({account},Uuid('{identity}'),'migration-synthetic-subject',CurrentUtcDatetime());
""")
jar={}
def request(method,path,body=None,csrf=True):
    conn=http.client.HTTPConnection(svc,8089,timeout=15)
    headers={'Host':'id.updspace.com','X-Forwarded-Proto':'https','User-Agent':'ID migration synthetic acceptance','Cookie':'; '.join(k+'='+v for k,v in jar.items())}
    if body is not None:
        headers.update({'Content-Type':'application/json','Origin':'https://id.updspace.com'})
        if csrf:headers['X-CSRFToken']=jar.get('csrftoken','')
    conn.request(method,path,json.dumps(body) if body is not None else None,headers)
    r=conn.getresponse();data=r.read();headers=r.getheaders()
    for k,v in headers:
        if k.lower()=='set-cookie':
            c=SimpleCookie();c.load(v)
            for name,value in c.items():jar[name]=value.value
    conn.close()
    try:data=json.loads(data)
    except ValueError:pass
    return r.status,headers,data

def login(extra=None,csrf=True):
    status,_,data=request('GET','/api/v1/auth/form_token?purpose=login');assert status==200
    body={'email':email,'password':password,'form_token':data['form_token']}
    body.update(extra or {})
    return request('POST','/api/v1/auth/login',body,csrf)

result={}
s,_,d=login(csrf=False);assert s==403 and d['code']=='CSRF_FAILED';result['csrf_rejected']=True
s,_,d=login({'password':'incorrect-synthetic-password'});assert s==401 and d['code']=='INVALID_CREDENTIALS';result['wrong_password_rejected']=True
s,h,d=login();assert s==200 and d['user']['email']==email,(s,list(d));result['password_login']=True
assert any(k.lower()=='set-cookie' and v.startswith('sessionid=') and 'Secure' in v and 'HttpOnly' in v for k,v in h);result['secure_session_cookie']=True
s,_,d=request('GET','/api/v1/auth/me');assert s==200 and d['user']['email']==email;result['session_read']=True
s,_,d=request('GET','/api/v1/auth/sessions');assert s==200;result['sessions_route']=True
s,h,d=request('GET','/account');assert s==200
csp=dict((k.lower(),v) for k,v in h).get('content-security-policy','');assert 'https://storage.updspace.com' in csp and 'storage.yandexcloud.net' not in csp;result['account_csp']=True
s,_,d=request('POST','/api/v1/auth/logout',{});assert s==200,(s,d);result['logout']=True
s,_,d=request('GET','/api/v1/auth/me');assert s==200 and d['user'] is None;result['session_revoked']=True
source=json.load(open('/opt/updspace-id/cloud-runtime-source.json'))
key=base64.b64decode(source['api']['environment']['ID_MFA_SEAL_KEY_B64'])
nonce=os.urandom(12);secret='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
sealed='id-mfa-v1:'+str(account)+':totp:'+base64.urlsafe_b64encode(nonce+AESGCM(key).encrypt(nonce,secret.encode(),f'updspace-id:mfa:totp:account:{account}'.encode())).decode().rstrip('=')
payload=json.dumps({'secret':sealed})
sql(f"UPSERT INTO mfa_authenticator (id,user_id,type,data,created_at) VALUES ({account},{account},'totp',Json('{payload}'),CurrentUtcDatetime());")
s,_,d=login();assert s==401 and d['code']=='MFA_REQUIRED';result['mfa_required']=True
if int(time.time())%30>25:time.sleep(31-int(time.time())%30)
mac=hmac.new(b'12345678901234567890',(int(time.time())//30).to_bytes(8,'big'),hashlib.sha1).digest();offset=mac[-1]&15
code=str((int.from_bytes(mac[offset:offset+4],'big')&0x7fffffff)%1000000).zfill(6)
s,_,d=login({'mfa_code':code});assert s==200,(s,d.get('code'));result['sealed_totp_login']=True
s,_,d=login({'mfa_code':code});assert s==401 and d['code']=='INVALID_CREDENTIALS';result['totp_replay_rejected']=True
result['synthetic_account']=account
result['trial_database_only']=True
Path('/opt/updspace-id/auth-acceptance.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
