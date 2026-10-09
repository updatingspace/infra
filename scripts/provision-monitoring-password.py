#!/usr/bin/env python3
"""Run as root on the k3s host. Preserve existing credentials on repeated runs."""
import json, os, secrets, subprocess
from pathlib import Path
os.umask(0o077)
assert os.geteuid()==0,'Run as root on the k3s host'
base=Path('/opt/updspace-infra/private');base.mkdir(parents=True,exist_ok=True,mode=0o700)
p=base/'monitoring-credentials.json'
if p.exists():
 credentials=json.loads(p.read_text())
 assert credentials['username']=='monitoring' and len(credentials['password'])>=32
else:
 credentials={'username':'monitoring','password':secrets.token_urlsafe(36)}
 with p.open('x') as f:json.dump(credentials,f);f.write('\n')
assert p.stat().st_mode & 0o077 == 0,'Credentials file permissions must be 0600'
cmd=['k3s','kubectl']
old=subprocess.run(cmd+['-n','edge','get','secret','observability-edge-auth','--ignore-not-found','-o','name'],capture_output=True,text=True,check=True)
if old.stdout.strip():
 print('Existing credentials and Secret preserved; no rotation')
else:
 result=subprocess.run(cmd+['-n','edge','exec','-i','deployment/caddy','--','caddy','hash-password','--bcrypt-cost','12'],input=credentials['password']+'\n',capture_output=True,text=True,check=True)
 hashed=result.stdout.strip();assert hashed.startswith('$2') and len(hashed)==60
 secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'observability-edge-auth','namespace':'edge'},'type':'Opaque','stringData':{'password-hash':hashed}}
 subprocess.run(cmd+['create','-f','-'],input=json.dumps(secret),text=True,check=True,stdout=subprocess.DEVNULL)
 print('Created monitoring password file and edge Secret; secret values not displayed')
