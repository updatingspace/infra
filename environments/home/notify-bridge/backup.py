#!/usr/bin/env python3
"""Online consistent queue snapshot, integrity check, seven-day local retention."""
import os,sqlite3,time
from pathlib import Path
os.umask(0o077)
dest=Path('/srv/notify-bridge/backups');dest.mkdir(exist_ok=True,mode=0o700)
stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime());temp=dest/(stamp+'.tmp');final=dest/(stamp+'.db')
with sqlite3.connect('file:/srv/notify-bridge/data/bridge.db?mode=ro',uri=True) as source,sqlite3.connect(temp) as target:
    source.backup(target)
    assert target.execute('pragma integrity_check').fetchone()[0]=='ok'
temp.replace(final)
for old in dest.glob('*.db'):
    if old!=final and old.stat().st_mtime<time.time()-7*86400:old.unlink()
print('notify-bridge consistent snapshot created')
