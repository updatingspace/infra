#!/usr/bin/env python3
"""Reuse the existing encrypted backup transport and SHA256 validation."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'portal/sync-postgres-backups.py'
spec = importlib.util.spec_from_file_location('postgres_backup_transport', path)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)
transport.REMOTE_ROOT = '/srv/updspace/backups/glitchtip'
transport.LOCAL_ROOT = Path.home() / '.local/share/updspace-backups/glitchtip'

if __name__ == '__main__':
    transport.sync()
