#!/usr/bin/env python3
"""Check public PZ settings, or apply offline without touching secrets/world data."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from policy import MANAGED_KEYS

ROOT = Path(__file__).resolve().parent
SERVER = Path('/srv/pz-storage/zomboid/zomboid/Server')
LUA = ('survival42_SandboxVars.lua', 'survival42_spawnregions.lua', 'survival42_spawnpoints.lua')
ASSIGNMENT = re.compile(rb'^([ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=)([^\r\n]*)(\r?\n)?$')
SENSITIVE = re.compile(r'(?i)(password|token|secret|webhook|credential|api.key)\s*[:=]')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def read_regular(path):
    require(path.resolve() == path.absolute(), 'symlink path refused: ' + path.name)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 2 * 1024 * 1024, 'invalid config file: ' + path.name)
        return stream.read()


def merge_ini(original, desired):
    require(isinstance(desired, dict) and set(desired) <= MANAGED_KEYS, 'unknown or protected INI key')
    require(desired.get('Public') == 'false', 'Public=false must remain explicit')
    require(all(isinstance(v, str) and not any(c in v for c in '\r\n\x00') for v in desired.values()), 'invalid INI value')
    seen = set()
    changed = []
    result = []
    for line in original.splitlines(keepends=True):
        match = ASSIGNMENT.fullmatch(line)
        if match:
            key = match[2].decode('ascii')
            require(key not in seen, 'duplicate INI key: ' + key)
            seen.add(key)
            if key in desired and match[3] != desired[key].encode('utf-8'):
                line = match[1] + desired[key].encode('utf-8') + (match[4] or b'')
                changed.append(key)
        result.append(line)
    require(set(desired) <= seen, 'managed INI key absent; review server version first')
    return b''.join(result), changed


def validate_lua(path):
    compiler = shutil.which('luac')
    require(compiler is not None, 'Lua edits require luac syntax validation; no files changed')
    result = subprocess.run([compiler, '-p', str(path)], capture_output=True)
    require(result.returncode == 0, 'Lua syntax check failed: ' + path.name)


def plan(server, profile):
    desired = json.loads(read_regular(profile / 'survival42.ini.json'), object_pairs_hook=unique)
    baseline = json.loads(read_regular(profile / 'lua-base-sha256.json'), object_pairs_hook=unique)
    require(set(baseline) == set(LUA) and all(re.fullmatch('[0-9a-f]{64}', v) for v in baseline.values()), 'invalid Lua baseline')
    original = read_regular(server / 'survival42.ini')
    updated, keys = merge_ini(original, desired)
    changes = []
    if updated != original:
        changes.append((server / 'survival42.ini', original, updated))
    for name in LUA:
        current = read_regular(server / name)
        wanted = read_regular(profile / name)
        require(not SENSITIVE.search(wanted.decode('utf-8')), 'sensitive Lua assignment refused: ' + name)
        if current != wanted:
            require(sha(current) == baseline[name], 'unreviewed live Lua change: ' + name)
            validate_lua(profile / name)
            changes.append((server / name, current, wanted))
    return changes, keys


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_file(path, data, metadata):
    fd, name = tempfile.mkstemp(prefix='.pz-config-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            info = metadata.stat()
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            shutil.copystat(metadata, temporary)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_changes(changes, backup_root):
    require(backup_root.resolve() == backup_root.absolute(), 'symlink backup path refused')
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(backup_root.stat().st_uid == os.geteuid() and backup_root.stat().st_mode & 0o077 == 0, 'backup directory must be private and owned by operator')
    require(not (backup_root / 'pending.json').exists(), 'unfinished configuration transaction')
    backup = Path(tempfile.mkdtemp(prefix='config-', dir=backup_root))
    for path, original, _ in changes:
        require(read_regular(path) == original, 'config changed during plan: ' + path.name)
        shutil.copy2(path, backup / path.name)
        if os.geteuid() == 0:
            info = path.stat()
            os.chown(backup / path.name, info.st_uid, info.st_gid)
        with (backup / path.name).open('rb') as stream:
            os.fsync(stream.fileno())
    sync_directory(backup)
    # Persistent marker blocks replay after a crash between separate file renames.
    marker = backup_root / 'pending.json'
    require(not marker.exists(), 'unfinished configuration transaction')
    with marker.open('x') as stream:
        json.dump({'backup': str(backup), 'files': [p.name for p, _, _ in changes]}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(backup_root)
    written = []
    try:
        for path, original, wanted in changes:
            require(read_regular(path) == original, 'config changed during apply: ' + path.name)
            written.append((path, original))
            replace_file(path, wanted, backup / path.name)
            require(read_regular(path) == wanted, 'config readback mismatch')
    except BaseException:
        for path, original in reversed(written):
            replace_file(path, original, backup / path.name)
        marker.unlink()
        sync_directory(backup_root)
        raise
    marker.unlink()
    sync_directory(backup_root)
    return backup


def require_stopped():
    def get(kind, name=None):
        args = ['k3s', 'kubectl', '-n', 'zomboid', 'get', kind]
        if name:
            args.append(name)
        return json.loads(subprocess.check_output(args + ['-o', 'json'], stderr=subprocess.DEVNULL))
    for kind, name in [('statefulset', 'zomboid'), ('deployment', 'panel')]:
        require(get(kind, name)['spec'].get('replicas', 1) == 0, 'game and panel must already be scaled to zero')
    require(get('cronjob', 'panel-auto-update')['spec'].get('suspend') is True, 'panel updater must remain suspended')
    require(all(p['status'].get('phase') in ('Succeeded', 'Failed') for p in get('pods')['items']), 'active or terminating PZ pod remains')


def apply_offline(profile):
    require(os.geteuid() == 0, 'apply requires root')
    source = Path('/opt/pz-backup/coordinator.py')
    for path in [*reversed(source.parents), source]:
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0, 'backup helper is not trusted')
    spec = importlib.util.spec_from_file_location('pz_backup', source)
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    with backup.locked(backup.LOCK):
        backup.disk_migration_ready()
        backup.updater_journal()
        require(backup.read_json(backup.STATE / 'journal.json').get('phase') in backup.TERMINAL, 'backup recovery must finish first')
        cfg = backup.configuration(Path('/etc/pz-backup/config.json'))
        backup.check_mount(backup.DATA, cfg['data_uuid'])
        require_stopped()
        state = Path('/var/lib/pz-game-config')
        require(not (state / 'pending.json').exists(), 'unfinished configuration transaction: inspect private backup before proceeding')
        changes, keys = plan(SERVER, profile)
        saved = write_changes(changes, state) if changes else None
        return changes, keys, saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.check:
        require(not Path('/var/lib/pz-game-config/pending.json').exists(), 'unfinished configuration transaction')
        changes, keys = plan(SERVER, ROOT)
        saved = None
    else:
        changes, keys, saved = apply_offline(ROOT)
    print(json.dumps({'mode': 'check' if args.check else 'apply', 'in_sync': not changes if args.check else True,
                      'changed_files': [p.name for p, _, _ in changes], 'changed_ini_keys': keys,
                      'private_backup': str(saved) if saved else None}))
    return 2 if args.check and changes else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        sys.exit(1)
