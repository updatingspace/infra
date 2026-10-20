#!/usr/bin/env python3
"""Explicit synthetic failure drill: restart bridge while egress is unavailable."""
import datetime,json,sqlite3,subprocess,time,urllib.request
from pathlib import Path
ROOT=Path('/opt/notify-bridge/private')
def k(*args):return subprocess.check_output(['k3s','kubectl',*args],text=True).strip()
def state():
    with sqlite3.connect('file:/srv/notify-bridge/data/bridge.db?mode=ro',uri=True) as c:
        return c.execute("SELECT id,message_id,version,sent_version,closed,recovery_sent FROM incidents WHERE grp='kuma:9000001' ORDER BY id DESC LIMIT 1").fetchone()
def main():
    ip=k('-n','notifications','get','svc','notify-bridge','-o','jsonpath={.spec.clusterIP}')
    key=(ROOT/'kuma-key').read_text().strip()
    def post(status):
        body={'monitor':{'id':9000001,'name':'Тест устойчивости доставки'},'heartbeat':{'monitorID':9000001,'status':status,'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'msg':'HTTP 502' if status==0 else 'HTTP 200'}}
        req=urllib.request.Request('http://'+ip+':8080/webhooks/kuma',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=10) as r:assert r.status==202
    prior=json.loads(k('-n','notifications','get','deployment','telegram-egress','-o','json'))['spec']['replicas']
    assert prior==1,'Expected a running single egress replica'
    try:
        k('-n','notifications','scale','deployment/telegram-egress','--replicas=0')
        for _ in range(20):
            pods=json.loads(k('-n','notifications','get','pods','-l','app.kubernetes.io/name=telegram-egress','-o','json'))['items']
            if not pods:break
            time.sleep(1)
        assert not pods,'Egress termination not complete'
        post(0);before=state();assert before and before[1]==0
        print('event accepted with egress stopped',flush=True)
        k('-n','notifications','rollout','restart','deployment/notify-bridge')
        k('-n','notifications','rollout','status','deployment/notify-bridge','--timeout=50s')
        after=state();assert after[0]==before[0] and after[1]==0
        print('queued incident survived bridge restart',flush=True)
    finally:
        k('-n','notifications','scale','deployment/telegram-egress','--replicas=1')
        k('-n','notifications','rollout','status','deployment/telegram-egress','--timeout=50s')
    for _ in range(30):
        if state()[1]>0:break
        time.sleep(2)
    assert state()[1]>0,'Queue did not drain after egress returned'
    print('queued incident delivered after egress returned',flush=True)
    post(1)
    for _ in range(20):
        s=state()
        if s[4]>0 and s[2]==s[3] and s[5]==1:break
        time.sleep(2)
    assert s[4]>0 and s[2]==s[3] and s[5]==1
    print('recovery card and recovery notification accepted by Telegram',flush=True)
if __name__=='__main__':main()
