#!/usr/bin/env python3
"""Operator-authorized root bootstrap of the one declared service client, never users."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess

ROOT = Path(__file__).resolve().parent
PRIVATE = Path('/opt/updspace-infra/private/observability-oidc.json')
KUBE = ['k3s', 'kubectl']


def literal(value):
    return json.dumps(value, ensure_ascii=True)


def read_sql():
    return "SELECT id, client_id, name, description, client_secret_hash, CAST(redirect_uris AS Utf8) AS redirect_uris, CAST(allowed_scopes AS Utf8) AS allowed_scopes, CAST(grant_types AS Utf8) AS grant_types, is_public, is_first_party FROM idp_oidcclient VIEW oidc_client_id_idx WHERE client_id = 'observability';"


def create_sql(config, credentials):
    fields = ('client_id','name','description')
    values = ["Unwrap(CAST("+literal(config[k])+" AS Utf8))" for k in fields]
    values += ['Unwrap(CAST('+literal(credentials['client_secret_hash'])+' AS Utf8))']
    values += ['Unwrap(CAST('+literal(json.dumps(config[k]))+' AS Json))' for k in ('redirect_uris','allowed_scopes','grant_types')]
    select = ', '.join(values)
    return f'''INSERT INTO idp_oidcclient (id, client_id, name, description, client_secret_hash, redirect_uris, allowed_scopes, grant_types, logo_url, response_types, is_public, is_first_party, created_at, updated_at)
SELECT Ensure(CAST({credentials['id']} AS Int64), n = 0, 'client already exists'), {select}, Unwrap(CAST('' AS Utf8)), Json('["code"]'), false, false, CurrentUtcDatetime(), CurrentUtcDatetime()
FROM (SELECT COUNT(*) AS n FROM idp_oidcclient VIEW oidc_client_id_idx WHERE client_id = 'observability');
INSERT INTO usid_audit_log (actor_user_id, action, target_type, target_id, tenant_id, meta_json, created_at)
VALUES (NULL, 'oidc_client.created', 'oidc_client', 'observability', NULL, Json('{{"source":"operator-authorized-infra-root-bootstrap","host":"updspace-home"}}'), CurrentUtcDatetime());'''


def matches(row, config, credentials):
    return (row['id'] == credentials['id'] and row['client_secret_hash'] == credentials['client_secret_hash']
            and all(row[k] == config[k] for k in ('client_id','name','description','is_public','is_first_party'))
            and all(json.loads(row[k]) == config[k] for k in ('redirect_uris','allowed_scopes','grant_types')))


def sql(query):
    command = KUBE + ['-n','updspace-data','exec','-i','id-ydb-0','--','/ydb',
        '--endpoint','grpcs://localhost:2135','--database','/local','--ca-file','/ydb_certs/ca.pem',
        '--user','root','--password-file','/ydb_admin/password','--no-discovery','sql','--format','json-unicode','-f','-']
    result = subprocess.run(command, input=query.encode(), capture_output=True)
    if result.returncode:
        raise RuntimeError('YDB operation failed or result uncertain; retain the private file and run check before retrying')
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('Credentials require a root-owned private regular file')
        return json.load(source)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--apply', action='store_true'); args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run on the VM as root')
    config = json.loads((ROOT/'client.json').read_text())
    expected_hosts = ['grafana','prometheus','alerts','errors','status']
    assert config['client_id'] == 'observability' and not config['is_public'] and not config['is_first_party']
    assert config['redirect_uris'] == [f'https://{h}.updspace.com/oauth2/callback' for h in expected_hosts]
    assert config['allowed_scopes'] == ['openid','email','profile','offline_access']
    assert config['grant_types'] == ['authorization_code','refresh_token']
    with open('/run/lock/updspace-observability-client.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = sql(read_sql())
        if len(rows) > 1:
            raise RuntimeError('Ambiguous existing client; no change made')
        credentials = private_file(PRIVATE) if PRIVATE.exists() else None
        if rows and (not credentials or not matches(rows[0], config, credentials)):
            raise RuntimeError('Existing client differs or private recovery file is missing; no rotation or update attempted')
        print(json.dumps({'client':'observability','state':'matching' if rows else 'missing','callbacks':config['redirect_uris'],'usersChanged':False}))
        if not args.apply: return
        if credentials is None:
            secret = secrets.token_urlsafe(48); salt = secrets.token_hex(16)
            digest = base64.b64encode(hashlib.pbkdf2_hmac('sha256', secret.encode(), salt.encode(), 1_000_000)).decode()
            credentials = {'id': (secrets.randbits(62) | (1 << 62)), 'client_secret': secret,
                'client_secret_hash':f'pbkdf2_sha256$1000000${salt}${digest}',
                'cookie_secret':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
            PRIVATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with os.fdopen(os.open(PRIVATE,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600),'w') as output:
                json.dump(credentials,output);output.flush();os.fsync(output.fileno())
            parent = os.open(PRIVATE.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(parent)
            finally: os.close(parent)
        if not rows:
            sql(create_sql(config, credentials))
            rows = sql(read_sql())
            assert len(rows) == 1 and matches(rows[0], config, credentials), 'Client verification failed'
        secret = {'apiVersion':'v1','kind':'Secret','metadata':{'name':'oauth2-proxy','namespace':'observability-auth'},
            'type':'Opaque','stringData':{'OAUTH2_PROXY_CLIENT_SECRET':credentials['client_secret'],
                'OAUTH2_PROXY_COOKIE_SECRET':credentials['cookie_secret']}}
        result = subprocess.run(KUBE+['apply','--server-side','--field-manager=observability-client','-f','-'],
            input=json.dumps(secret).encode(), capture_output=True)
        if result.returncode: raise RuntimeError('Client is persisted; Kubernetes Secret apply failed, rerun after checking drift')
        print('Client and session key verified; personal accounts and their privileges unchanged')


if __name__ == '__main__': main()
