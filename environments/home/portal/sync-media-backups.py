#!/usr/bin/env python3
"""Copy encrypted Portal media into the approved Portal backup directory."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('portal_backup_sync', Path(__file__).with_name('sync-postgres-backups.py'))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
sync.REMOTE_ROOT = '/srv/updspace/backups/portal-media'
sync.LOCAL_ROOT = Path.home() / '.local/share/updspace-backups/portal-postgres/media'
sync.ARCHIVE_NAME = 'media.tar.gpg'
if __name__ == '__main__':
    sync.sync()
