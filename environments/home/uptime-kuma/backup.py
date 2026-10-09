#!/usr/bin/env python3
"""Daily local SQLite snapshot; root-only because the database contains credentials."""
import datetime
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

os.umask(0o077)
data = Path('/srv/uptime-kuma/data')
backups = Path('/srv/uptime-kuma/backups')
backups.mkdir(mode=0o700, exist_ok=True)
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
destination = backups / stamp
with tempfile.TemporaryDirectory(prefix='.pending-', dir=backups) as temporary:
    staging = Path(temporary)
    with sqlite3.connect(f'file:{data}/kuma.db?mode=ro', uri=True) as source:
        with sqlite3.connect(staging / 'kuma.db') as copy:
            source.backup(copy)
            if copy.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise RuntimeError('Backup integrity check failed')
    shutil.copy2(data / 'db-config.json', staging / 'db-config.json')
    if (data / 'upload').exists():
        shutil.copytree(data / 'upload', staging / 'upload')
    staging.rename(destination)
for expired in sorted(p for p in backups.iterdir() if p.is_dir() and p.name.endswith('Z'))[:-7]:
    shutil.rmtree(expired)
print(f'Verified snapshot: {destination}')
