#!/usr/bin/env python3
"""Install reviewed backup code and explicitly supplied settings, always disabled.

No cloud calls, credentials, encryption keys, mount formatting, application
restarts, schedules or backup invocations. Existing enabled/active units block
installation. Changed settings require their previous SHA256 in the input.
"""
import argparse
import ast
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

SCRIPTS = ('coordinator.py', 'remote.py', 'restore.py', 'runtime-drill.py', 'oci-import.py',
           'gather-recovery.py', 'upload-queue.py', 'metrics.py', 'install.py',
           'cleanup.py', 'verify-remote.py', 'disk-migrate.py')
AUXILIARY = ('requirements.txt',)
UNITS = ('pz-backup.service', 'pz-backup.timer', 'pz-backup-recover.service',
         'pz-backup-upload.service', 'pz-backup-upload.timer', 'pz-backup-retain.service', 'pz-backup-metrics.service',
         'pz-backup-cleanup.service')
GROUP = 'pz-backup-remote'
ROLES = ('pz-backup-upload', 'pz-backup-retain')
FLAGS = ('enabled', 'upload-enabled', 'retention-enabled')
REMOTE_LOCK = '/var/lib/pz-backup-remote/remote.lock'


class Refused(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def run(args, allowed=(0,)):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    require(result.returncode in allowed, 'installation_command_failed')
    return result


def safe_components(path, root_uid=0):
    for component in [*reversed(path.parents), path]:
        if not component.exists() and not component.is_symlink():
            continue
        info = component.lstat()
        require(not stat.S_ISLNK(info.st_mode), 'destination_symlink_rejected')
        # /tmp test roots need only the symlink check above. Production paths
        # never traverse a user-controlled or world/group-writable directory.
        if root_uid == 0:
            require(info.st_uid == 0 and info.st_mode & 0o022 == 0, 'destination_not_root_controlled')


def source_files(source):
    require(source.is_dir() and not source.is_symlink(), 'source_directory_invalid')
    files = {}
    for name in SCRIPTS:
        path = source / name
        require(path.is_file() and not path.is_symlink(), 'required_source_missing')
        data = path.read_bytes()
        ast.parse(data, filename=name)
        files['/opt/pz-backup/' + name] = data
    for name in AUXILIARY:
        path = source / name
        require(path.is_file() and not path.is_symlink(), 'required_source_missing')
        files['/opt/pz-backup/' + name] = path.read_bytes()
    require(not (source / 'systemd').is_symlink(), 'source_unit_directory_symlink')
    for name in UNITS:
        path = source / 'systemd' / name
        require(path.is_file() and not path.is_symlink(), 'required_unit_missing')
        data = path.read_bytes()
        require(b'[Unit]' in data and (b'[Service]' in data or b'[Timer]' in data), 'unit_invalid')
        files['/etc/systemd/system/' + name] = data
    return files


def private_input(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0,
                'installer_settings_must_be_root_private')
        raw = stream.read(4 * 1024 * 1024 + 1)
    require(len(raw) <= 4 * 1024 * 1024, 'installer_settings_too_large')
    value = json.loads(raw)
    require(isinstance(value, dict) and set(value) <= {'coordinator', 'remote', 'expected_existing_sha256'},
            'unexpected_installer_settings')
    return value


def accounts():
    try:
        group = grp.getgrnam(GROUP)
    except KeyError:
        run(['groupadd', '--system', GROUP])
        group = grp.getgrnam(GROUP)
    result = {'gid': group.gr_gid}
    for name in ROLES:
        try:
            user = pwd.getpwnam(name)
        except KeyError:
            run(['useradd', '--system', '--gid', GROUP, '--home-dir', '/nonexistent',
                 '--shell', '/usr/sbin/nologin', name])
            user = pwd.getpwnam(name)
        require(user.pw_uid != 0 and user.pw_gid == group.gr_gid
                and user.pw_shell in {'/usr/sbin/nologin', '/sbin/nologin'}
                and user.pw_dir == '/nonexistent'
                and set(os.getgrouplist(name, user.pw_gid)) == {group.gr_gid}, 'service_account_has_unexpected_privileges')
        result[name] = user.pw_uid
    return result


def settings_files(settings, identities):
    files = {}
    coordinator = settings.get('coordinator')
    if coordinator is not None:
        require(isinstance(coordinator, dict), 'coordinator_settings_invalid')
        coordinator = dict(coordinator)
        if 'uploader_gid' in coordinator:
            require(coordinator['uploader_gid'] == identities['gid'], 'uploader_group_mismatch')
        coordinator['uploader_gid'] = identities['gid']
        # Only the OS-derived numeric group is filled; all deployment decisions
        # must be explicit in the supplied coordinator settings.
        required = {'data_root', 'spool', 'data_uuid', 'spool_uuid', 'age_recipient', 'infra_revision',
                    'server_name', 'min_free_bytes', 'min_free_inodes', 'max_snapshot_bytes', 'max_entries',
                    'stop_timeout_seconds', 'recovery_helper', 'uploader_gid'}
        require(required <= coordinator.keys(), 'coordinator_settings_incomplete')
        require(coordinator['data_root'] == '/srv/pz-storage/zomboid' and coordinator['spool'] == '/srv/pz-backup-spool'
                and coordinator['recovery_helper'] == '/opt/pz-backup/gather-recovery.py', 'coordinator_paths_invalid')
        files['config.json'] = (coordinator, 0)
    remote = settings.get('remote')
    if remote is not None:
        require(isinstance(remote, dict) and {'bucket', 'prefix', 'endpoint_url', 'lock_path'} <= remote.keys(),
                'remote_settings_incomplete')
        require(remote['lock_path'] == REMOTE_LOCK, 'shared_remote_lock_required')
        require(isinstance(remote['endpoint_url'], str) and remote['endpoint_url'].startswith('https://'),
                'remote_https_endpoint_required')
        require(not any(key in remote for key in ('access_key_id', 'secret_access_key', 'session_token', 'credentials')),
                'credentials_require_separate_delivery')
        files['remote-upload.json'] = (remote, identities['pz-backup-upload'])
        files['remote-retain.json'] = (remote, identities['pz-backup-retain'])
        # The cleanup helper compares the low-privilege verifier's result with
        # a root-controlled scope; uploader-owned config cannot broaden it.
        files['cleanup-remote.json'] = (remote, 0)
    return files


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def check_replacement(path, data, expected):
    require(not path.is_symlink(), 'existing_settings_symlink')
    if not path.exists():
        require(expected is None, 'expected_settings_missing')
        return
    previous = path.read_bytes()
    if previous != data:
        require(expected is not None and hashlib.sha256(previous).hexdigest() == expected,
                'changed_settings_require_previous_sha256')


def write_file(path, data, uid, gid, mode):
    descriptor, temporary = tempfile.mkstemp(prefix='.pz-backup-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fchown(output.fileno(), uid, gid)
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        require(path.read_bytes() == data, 'installed_file_readback_failed')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def installation_lock(path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(descriptor).st_mode), 'installation_lock_invalid')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('maintenance_in_progress') from None
        yield
    finally:
        os.close(descriptor)


def install(source, settings=None, *, root=Path('/'), runner=run, identity_provider=accounts,
            mount_check=os.path.ismount, root_uid=0):
    settings = settings or {}
    files = source_files(source)
    path = lambda value: root / value.lstrip('/')
    require(mount_check(path('/srv/pz-backup-spool')), 'mounted_spool_required_before_install')
    for name in UNITS:
        enabled = runner(['systemctl', 'is-enabled', name], allowed=(0, 1, 3, 4)).stdout.decode().strip()
        require(enabled in {'disabled', 'static', 'not-found', ''}, 'enabled_or_linked_unit_requires_operator_stop')
        active = runner(['systemctl', 'is-active', name], allowed=(0, 1, 3, 4)).stdout.decode().strip()
        require(active in {'inactive', 'unknown', ''}, 'active_or_failed_unit_requires_inspection')
    for flag in FLAGS:
        require(not path('/etc/pz-backup/' + flag).exists() and not path('/etc/pz-backup/' + flag).is_symlink(),
                'enabled_backup_configuration_requires_operator_stop')
    for filename in files:
        safe_components(path(filename), root_uid)
    for directory in ('/etc/pz-backup', '/var/lib/pz-backup', '/var/lib/pz-backup-remote',
                      '/var/lib/pz-backup-upload', '/var/lib/pz-backup-retain', '/srv/pz-backup-spool'):
        safe_components(path(directory).parent, root_uid)
        require(not path(directory).is_symlink(), 'managed_directory_symlink')
    identities = identity_provider()
    configured = settings_files(settings, identities)
    expected = settings.get('expected_existing_sha256', {})
    require(isinstance(expected, dict) and set(expected) <= set(configured), 'unexpected_replacement_digest')
    for filename, (document, _uid) in configured.items():
        check_replacement(path('/etc/pz-backup/' + filename), encoded(document), expected.get(filename))
    directories = {
        '/opt/pz-backup': (root_uid, 0, 0o755), '/etc/pz-backup': (root_uid, identities['gid'], 0o750),
        '/etc/systemd/system': (root_uid, 0, 0o755), '/var/lib/pz-backup': (root_uid, 0, 0o700),
        '/var/lib/pz-backup-remote': (root_uid, identities['gid'], 0o750),
        '/var/lib/pz-backup-upload': (identities['pz-backup-upload'], identities['gid'], 0o700),
        '/var/lib/pz-backup-retain': (identities['pz-backup-retain'], identities['gid'], 0o700),
        '/srv/pz-backup-spool': (root_uid, identities['gid'], 0o750),
    }
    for directory, (uid, gid, mode) in directories.items():
        target = path(directory)
        target.mkdir(parents=True, exist_ok=True, mode=mode)
        os.chown(target, uid, gid)
        os.chmod(target, mode)
    # Existing lock inode must remain stable while both role users use it.
    lock_path = path(REMOTE_LOCK)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        require(stat.S_ISREG(os.fstat(descriptor).st_mode), 'remote_lock_invalid')
        os.fchown(descriptor, root_uid, identities['gid'])
        os.fchmod(descriptor, 0o660)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for filename, data in files.items():
        write_file(path(filename), data, root_uid, 0, 0o755 if filename.endswith('.py') else 0o644)
    for filename, (document, uid) in configured.items():
        write_file(path('/etc/pz-backup/' + filename), encoded(document), root_uid if uid == 0 else uid, identities['gid'], 0o600)
    # Reloading definitions does not enable/start services or create timer events.
    runner(['systemctl', 'daemon-reload'])
    return {'installed_scripts': len(SCRIPTS), 'installed_units': len(UNITS),
            'services_started': False, 'timers_enabled': False, 'credentials_created': False,
            'uploader_gid': identities['gid'], 'configured_files': sorted(configured)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--config', type=Path, help='Root-private installer settings JSON; never credentials')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'root_required')
    os.umask(0o077)
    for binary in ('age', 'zstd', 'rsync', 'tar', 'findmnt', 'k3s'):
        require(shutil.which(binary) is not None, 'required_dependency_missing')
    run(['/opt/pz-backup-venv/bin/python3', '-c',
         'import boto3; s=boto3.session.Session()._session.get_service_model("s3"); '
         'assert all("IfNoneMatch" in s.operation_model(o).input_shape.members for o in ("PutObject", "CompleteMultipartUpload"))'])
    settings = private_input(args.config) if args.config else {}
    safe_components(Path('/var/lib/pz-volumes'))
    with installation_lock(Path('/var/lib/pz-volumes/migration.lock')):
        print(json.dumps(install(args.source, settings), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (Refused, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        print(json.dumps({'status': 'failed', 'reason': reason}), file=sys.stderr)
        sys.exit(1)
