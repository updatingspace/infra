#!/usr/bin/env python3
"""Replay Kuma heartbeat history read-only; checkpoint only accepted events."""
import json,os,sqlite3,subprocess,urllib.request,time
from pathlib import Path
ROOT=Path('/opt/notify-bridge/private')

def main():
    os.umask(0o077)
    state=ROOT/'kuma-cursor.json'
    db=sqlite3.connect('file:/srv/uptime-kuma/data/kuma.db?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute('BEGIN')
    maximum=db.execute('select coalesce(max(id),0) from heartbeat').fetchone()[0]
    if state.exists():
        cursor=json.loads(state.read_text())['cursor']
        if cursor>maximum:raise RuntimeError('Kuma history rolled back; explicitly reset cursor after investigating')
        rows=db.execute('SELECT h.*,m.name,m.active FROM heartbeat h JOIN monitor m ON m.id=h.monitor_id WHERE h.id>? ORDER BY h.id LIMIT 500',(cursor,)).fetchall()
    else:
        rows=db.execute('SELECT h.*,m.name,m.active FROM monitor m JOIN heartbeat h ON h.id=(SELECT id FROM heartbeat WHERE monitor_id=m.id ORDER BY id DESC LIMIT 1) WHERE m.active=1 ORDER BY h.id').fetchall()
        cursor=maximum
    db.rollback();db.close()
    r=subprocess.run(['k3s','kubectl','--cache-dir=/opt/notify-bridge/private/kube-cache','-n','notifications','get','svc','notify-bridge','-o','jsonpath={.spec.clusterIP}'],capture_output=True,text=True,check=True,timeout=10)
    url='http://'+r.stdout.strip()+':8080/webhooks/kuma'
    key=(ROOT/'kuma-key').read_text().strip()
    for row in rows:
        if row['active']:
            payload={'monitor':{'id':row['monitor_id'],'name':row['name']},'heartbeat':{'id':row['id'],'monitorID':row['monitor_id'],'status':row['status'],'time':row['time'],'msg':row['msg'] or ''}}
            request=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=10) as response:
                if response.status!=202:raise RuntimeError('bridge did not accept event')
        cursor=max(cursor,row['id'])
    temp=state.with_suffix('.tmp');temp.write_text(json.dumps({'cursor':cursor,'last_success':int(time.time())}));temp.chmod(0o600);os.replace(temp,state)
    print('Kuma reconciliation accepted %d observations'%len(rows))
if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('Kuma reconciliation failed: '+type(exc).__name__)
