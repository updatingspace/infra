#!/usr/bin/env python3
"""Deliver only this updater's source and units over the existing operator SSH."""
import base64
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent
FILES = ('controller.py', 'pz-game-update.service', 'pz-game-update.timer')


def main():
    payload = {name: base64.b64encode((ROOT / name).read_bytes()).decode() for name in FILES}
    payload['metrics.py'] = base64.b64encode((ROOT.parent / 'backup/metrics.py').read_bytes()).decode()
    script = '''import base64, importlib.util, json, os, pathlib, subprocess
spec = importlib.util.spec_from_file_location('backup', '/opt/pz-backup/coordinator.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
payload = json.loads(PAYLOAD)
with b.locked(b.LOCK, create=True):
    b.disk_migration_ready()
    b.require(b.read_json(b.STATE / 'journal.json').get('phase') in b.TERMINAL, 'maintenance_incomplete')
    state = subprocess.run(['systemctl', 'is-active', 'pz-game-update.service'], capture_output=True, text=True)
    b.require(state.stdout.strip() not in {'active', 'activating', 'deactivating'}, 'updater_active')
    root = pathlib.Path('/opt/pz-game-update')
    root.mkdir(mode=0o700, exist_ok=True)
    b.trusted(root, directory=True)
    for name, encoded in payload.items():
        destination = (pathlib.Path('/opt/pz-backup/metrics.py') if name == 'metrics.py' else
                       root / name if name == 'controller.py' else pathlib.Path('/etc/systemd/system') / name)
        temporary = destination.with_suffix(destination.suffix + '.new')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600 if name == 'controller.py' else 0o644)
        with os.fdopen(fd, 'wb') as output:
            output.write(base64.b64decode(encoded, validate=True))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        b.fsync_directory(destination.parent)
    b.command(['systemctl', 'daemon-reload'])
    b.command(['systemctl', 'try-restart', 'pz-backup-metrics.service'])
    b.command(['systemctl', 'enable', '--now', 'pz-game-update.timer'])
    print('Installed game updater; hourly timer enabled')
'''.replace('PAYLOAD', repr(json.dumps(payload)))
    ssh = runpy.run_path(str(ROOT.parent / 'deploy.py'))['ssh']
    ssh('sudo -n python3 -', input=script, text=True, timeout=90)


if __name__ == '__main__':
    main()
