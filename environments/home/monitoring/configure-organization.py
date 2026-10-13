#!/usr/bin/env python3
"""Rename the existing Grafana organization, preserving its users and dashboards."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parent
DATABASE = Path('/srv/pz-monitoring/grafana/grafana.db')
KUBE = ['k3s', 'kubectl', '-n', 'observability']


def reconcile(connection, config, apply=False):
    row = connection.execute('SELECT name FROM org WHERE id=?', (config['id'],)).fetchone()
    assert row and row[0] in ('Main Org.', config['name']), 'Unexpected organization; no rename'
    assert not connection.execute('SELECT id FROM org WHERE name=? AND id<>?',
                                  (config['name'], config['id'])).fetchone(), 'Name already belongs to another organization'
    changed = row[0] != config['name']
    if apply and changed:
        with connection:
            cursor = connection.execute('UPDATE org SET name=?,updated=CURRENT_TIMESTAMP WHERE id=? AND name=?',
                                        (config['name'], config['id'], row[0]))
            assert cursor.rowcount == 1, 'Organization changed concurrently'
    return {'id': config['id'], 'name': config['name'] if apply else row[0], 'drift': changed and not apply}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--apply', action='store_true'); args = parser.parse_args()
    if os.geteuid() != 0: raise SystemExit('Run as root on the VM')
    config = json.loads((ROOT/'organization.json').read_text())
    assert config == {'id': 1, 'name': 'UpdatingSpace LLC'}
    assert DATABASE.is_file() and not DATABASE.is_symlink()
    with open('/run/lock/grafana-organization.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(f'file:{DATABASE}?mode=ro', uri=True) as connection:
            result = reconcile(connection, config)
        if not args.apply or not result['drift']:
            print(json.dumps(result)); return
        subprocess.run(KUBE+['scale','deployment/grafana','--current-replicas=1','--replicas=0'], check=True)
        try:
            subprocess.run(KUBE+['wait','--for=delete','pod','-l','app.kubernetes.io/name=grafana','--timeout=90s'], check=True)
            backup = Path('/opt/updspace-infra/private')/('grafana-org-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite')
            os.close(os.open(backup, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600))
            with sqlite3.connect(DATABASE) as connection, sqlite3.connect(backup) as snapshot:
                connection.backup(snapshot)
                assert snapshot.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                result = reconcile(connection, config, apply=True)
        finally:
            subprocess.run(KUBE+['scale','deployment/grafana','--current-replicas=0','--replicas=1'], check=True)
        subprocess.run(KUBE+['rollout','status','deployment/grafana','--timeout=90s'], check=True)
        print(json.dumps(result))


if __name__ == '__main__': main()
