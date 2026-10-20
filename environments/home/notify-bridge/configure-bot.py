#!/usr/bin/env python3
"""Interactive, local-only credential entry. No token in argv, logs or shell history."""
import getpass
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT=Path('/opt/notify-bridge/private')

def kube(*args,data=None):
    r=subprocess.run(['k3s','kubectl',*args],input=data,capture_output=True,text=True,timeout=75)
    if r.returncode: raise RuntimeError('Kubernetes operation failed')
    return r.stdout

def read_exact(sock,n):
    b=b''
    while len(b)<n:
        part=sock.recv(n-len(b))
        if not part:raise OSError('SOCKS closed')
        b+=part
    return b

class TelegramConnection(http.client.HTTPSConnection):
    def __init__(self,proxy):
        super().__init__('api.telegram.org',timeout=40,context=ssl.create_default_context())
        self.proxy=proxy
    def connect(self):
        sock=socket.create_connection((self.proxy,1080),timeout=40)
        try:
            sock.sendall(b'\x05\x01\x00')
            if read_exact(sock,2)!=b'\x05\x00':raise OSError('SOCKS auth failed')
            host=b'api.telegram.org';sock.sendall(b'\x05\x01\x00\x03'+bytes([len(host)])+host+struct.pack('!H',443))
            header=read_exact(sock,4)
            if header[1]!=0:raise OSError('SOCKS route failed')
            size={1:4,4:16}.get(header[3])
            if header[3]==3:size=read_exact(sock,1)[0]
            if size is None:raise OSError('SOCKS response invalid')
            read_exact(sock,size+2)
            self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except Exception:
            sock.close();raise

def api(proxy,token,method,payload):
    c=TelegramConnection(proxy)
    try:
        c.request('POST','/bot'+token+'/'+method,json.dumps(payload),{'Content-Type':'application/json'})
        r=c.getresponse();data=json.loads(r.read(2*1024*1024))
        if r.status!=200 or not data.get('ok'):raise RuntimeError('Telegram API rejected request: '+str(data.get('error_code',r.status)))
        return data['result']
    finally:c.close()

def main():
    if os.geteuid()!=0:raise RuntimeError('Run with sudo')
    os.umask(0o077)
    proxy=kube('-n','notifications','get','svc','telegram-egress','-o','jsonpath={.spec.clusterIP}').strip()
    token=getpass.getpass('Токен от BotFather (ввод скрыт): ').strip()
    if ':' not in token or '/' in token or '\n' in token:raise ValueError('Invalid token')
    me=api(proxy,token,'getMe',{})
    webhook=api(proxy,token,'getWebhookInfo',{})
    if webhook.get('url'):raise RuntimeError('This bot already has a webhook. Use a dedicated new bot.')
    # Pairing never relies on "first message wins". A random one-time code proves chat ownership.
    code=secrets.token_hex(8)
    print('Открой @'+me['username']+' и отправь ему ровно эту строку:\n/pair '+code,flush=True)
    offset=0;deadline=time.monotonic()+300;owner=None
    while time.monotonic()<deadline and owner is None:
        for u in api(proxy,token,'getUpdates',{'offset':offset,'timeout':20,'allowed_updates':['message','callback_query']}):
            offset=max(offset,u['update_id']+1);m=u.get('message',{})
            if m.get('text','').strip()=='/pair '+code and m.get('chat',{}).get('type')=='private' and not m.get('from',{}).get('is_bot'):
                owner=(m['chat']['id'],m['from']['id']);break
    if owner is None:raise RuntimeError('Pairing timed out. Run this command again.')
    for name,value in [('telegram-token',token),('telegram-chat',str(owner[0])),('telegram-users',str(owner[1]))]:
        path=ROOT/name;path.write_text(value+'\n');path.chmod(0o600)
    data={n:(ROOT/n).read_text().strip() for n in ['alertmanager-key','kuma-key','telegram-token','telegram-chat','telegram-users']}
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':'notify-bridge','namespace':'notifications'},'type':'Opaque','stringData':data}
    kube('apply','-f','-',data=json.dumps(obj))
    kube('-n','notifications','rollout','restart','deployment/notify-bridge')
    kube('-n','notifications','rollout','status','deployment/notify-bridge','--timeout=60s')
    address=kube('-n','notifications','get','svc','notify-bridge','-o','jsonpath={.spec.clusterIP}').strip()
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    alert={'status':'firing','startsAt':now,'labels':{'alertname':'NotifyBridgeAcceptance','severity':'warning'},'annotations':{'summary':'Проверка доставки после настройки бота. Можно нажать «Принял».'}}
    req=urllib.request.Request('http://'+address+':8080/webhooks/alertmanager',data=json.dumps({'alerts':[alert]}).encode(),headers={'Authorization':'Bearer '+data['alertmanager-key'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=10) as r:assert r.status==202
    print('Бот подключён. Проверочный инцидент поставлен в очередь. Убедись, что карточка пришла на iPhone. После проверки можно выполнить: sudo python3 /opt/notify-bridge/source/configure-bot.py --resolve-test')

def resolve_test():
    import sqlite3
    address=kube('-n','notifications','get','svc','notify-bridge','-o','jsonpath={.spec.clusterIP}').strip()
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    alert={'status':'resolved','startsAt':now,'endsAt':now,'labels':{'alertname':'NotifyBridgeAcceptance','severity':'warning'},'annotations':{'summary':'Проверка завершена'}}
    req=urllib.request.Request('http://'+address+':8080/webhooks/alertmanager',data=json.dumps({'alerts':[alert]}).encode(),headers={'Authorization':'Bearer '+(ROOT/'alertmanager-key').read_text().strip(),'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=10) as r:assert r.status==202
    print('Проверочный инцидент закрыт; ожидается сообщение о восстановлении.')

if __name__=='__main__':
    try:
        resolve_test() if '--resolve-test' in sys.argv else main()
    except KeyboardInterrupt:print('\nОтменено.',file=sys.stderr);sys.exit(1)
    except Exception as exc:
        # Never print exception contents: an HTTP client can embed credential-bearing paths.
        print('Настройка не завершена ('+type(exc).__name__+'). Токен не выведен. Проверь доступность egress и повтори.',file=sys.stderr);sys.exit(1)
