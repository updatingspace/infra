#!/usr/bin/env python3
"""Bootstrap bridge-owned directories and secrets on updspace-home only."""
import json,os,secrets,subprocess
from pathlib import Path
ROOT=Path('/opt/notify-bridge/private')
os.umask(0o077)
ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
Path('/srv/notify-bridge/data').mkdir(parents=True,exist_ok=True)
Path('/srv/notify-bridge/status').mkdir(parents=True,exist_ok=True,mode=0o755)
os.chmod('/srv/notify-bridge/status',0o755)
os.chown('/srv/notify-bridge/data',10001,10001);os.chmod('/srv/notify-bridge/data',0o700)
for key in ['alertmanager-key','kuma-key']:
    p=ROOT/key
    if not p.exists():p.write_text(secrets.token_urlsafe(48)+'\n');p.chmod(0o600)
for key in ['telegram-token','telegram-chat','telegram-users']:
    p=ROOT/key
    if not p.exists():p.touch(mode=0o600)
items=[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':'notifications','labels':{'pod-security.kubernetes.io/enforce':'restricted'}}}]
for name,data in [('notify-bridge',{n:(ROOT/n).read_text().strip() for n in ['alertmanager-key','kuma-key','telegram-token','telegram-chat','telegram-users']}),('telegram-egress-config',{'config.json':(ROOT/'xray.json').read_text()})]:
    items.append({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':'notifications'},'type':'Opaque','stringData':data})
r=subprocess.run(['k3s','kubectl','apply','-f','-'],input=json.dumps({'apiVersion':'v1','kind':'List','items':items}),capture_output=True,text=True)
if r.returncode:raise SystemExit('Secret bootstrap failed; credentials were not printed')
print('bridge directories and secrets ready')
