#!/usr/bin/env python3
"""Run once, with the OLD panel stopped. Backup db.json before calling."""
import json
import os
from pathlib import Path

path = Path('/opt/pz-stack/data/panel/db.json')
info = path.stat()
data = json.loads(path.read_text())
active = [s for s in data['servers'] if s.get('isActive')]
assert len(active) == 1, 'Expected exactly one active server'
server = active[0]
assert server.get('serverName') == 'survival42', 'Unexpected server; review migration'
server.update(rconHost='zomboid', provider='docker-local', dockerContainerName='pz-b42-zomboid-1', isRemote=False)
data['settings'].update(rconHost='zomboid', autoStartServer=False, modAutoRestartEnabled=False, modAutoRestart=False)
# Existing scheduled lifecycle jobs would require an explicit migration plan.
assert not data.get('scheduled_tasks'), 'Scheduled jobs exist; review before migration'
tmp = path.with_suffix('.json.split-tmp')
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write('\n')
    f.flush()
    os.fsync(f.fileno())
os.chown(tmp, info.st_uid, info.st_gid)
os.chmod(tmp, info.st_mode & 0o777)
os.replace(tmp, path)
print('Panel profile migrated; game autostart and automatic mod restarts disabled')
