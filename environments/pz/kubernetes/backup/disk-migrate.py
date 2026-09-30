#!/usr/bin/env python3
"""Explicit new-disk initialization and one fail-closed PZ disk cutover.

The source image is never formatted, truncated, deleted or copied as a live
filesystem image. Migration requires an off-host restore attestation, quiesces
writers with observed clean exit, hashes every copied file, and preserves the
source loop read-only. Run in a persistent systemd service, not an SSH shell.
An interrupted operation requires operator inspection; it never guesses rollback.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

import coordinator as backup
import remote

DATA = backup.DATA
SPOOL = backup.SPOOL
NEW = Path('/srv/pz-data-migration')
OLD = Path('/srv/pz-storage/zomboid-old')
IMAGE = Path('/var/lib/pz-volumes/zomboid.ext4')
STATE = Path('/var/lib/pz-backup/disk-migration')
FSTAB = Path('/etc/fstab')
BEGIN = '# BEGIN pz-k3s bounded storage (Terraform)'
END = '# END pz-k3s bounded storage (Terraform)'
SPOOL_BEGIN = '# BEGIN pz backup spool'
SPOOL_END = '# END pz backup spool'
GIB = 1024 ** 3
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
require = backup.require
command = backup.command


def block_identity(disk_id, expected_gib):
    require(re.fullmatch(r'[a-z0-9]{20}', disk_id), 'explicit_cloud_disk_id_required')
    device = Path('/dev/disk/by-id') / ('virtio-' + disk_id)
    resolved = device.resolve(strict=True)
    info = resolved.stat()
    require(stat.S_ISBLK(info.st_mode), 'target_not_block_device')
    rows = json.loads(command(['lsblk', '--json', '--bytes', '--output',
                              'NAME,PATH,TYPE,SERIAL,SIZE,FSTYPE,MOUNTPOINTS', str(resolved)]))['blockdevices']
    require(len(rows) == 1, 'target_disk_ambiguous')
    row = rows[0]
    require(row['type'] == 'disk' and row.get('serial') == disk_id
            and int(row['size']) == expected_gib * GIB, 'target_disk_identity_mismatch')
    require(not row.get('children'), 'partitioned_disk_rejected')
    holders = Path('/sys/class/block') / resolved.name / 'holders'
    require(holders.is_dir() and not any(holders.iterdir()), 'device_has_holders')
    return device, row


def initialize(disk_id, expected_gib, label, confirmation):
    require(confirmation == disk_id, 'explicit_empty_disk_initialization_required')
    require(label in {'pz-data', 'pz-backup-spool'}, 'unexpected_filesystem_label')
    device, row = block_identity(disk_id, expected_gib)
    require(not row.get('fstype') and not any(row.get('mountpoints') or []), 'disk_not_empty_or_mounted')
    signatures = json.loads(command(['wipefs', '--no-act', '--json', str(device)]))
    require(not signatures.get('signatures'), 'existing_disk_signature_rejected')
    # No force flag: mkfs itself performs a final filesystem/device check.
    command(['mkfs.ext4', '-L', label, '-m', '1', str(device)], timeout=1800)
    identity = command(['blkid', '-o', 'export', str(device)]).decode()
    fields = dict(line.split('=', 1) for line in identity.splitlines() if '=' in line)
    require(fields.get('TYPE') == 'ext4' and fields.get('LABEL') == label
            and UUID.fullmatch(fields.get('UUID', '')), 'formatted_disk_identity_unconfirmed')
    return {'disk_id': disk_id, 'uuid': fields['UUID'], 'size_gib': expected_gib, 'label': label}


def mount_record(path, expected_uuid):
    require(UUID.fullmatch(expected_uuid), 'expected_mount_uuid_required')
    backup.check_mount(path, expected_uuid)
    result = json.loads(command(['findmnt', '--json', '--mountpoint', str(path),
                                 '-o', 'SOURCE,TARGET,FSTYPE,UUID,OPTIONS,FS-OPTIONS']))['filesystems']
    require(len(result) == 1, 'mount_ambiguous')
    return result[0]


def source_identity(expected_uuid):
    row = mount_record(DATA, expected_uuid)
    require(re.fullmatch(r'/dev/loop[0-9]+', row['source']), 'source_not_original_loop')
    loops = json.loads(command(['losetup', '--json', '--output', 'NAME,BACK-FILE,OFFSET,SIZELIMIT,RO']))['loopdevices']
    matched = [item for item in loops if item['name'] == row['source']]
    require(len(matched) == 1 and matched[0]['back-file'] == str(IMAGE)
            and matched[0]['offset'] == 0 and matched[0]['sizelimit'] == 0, 'source_loop_identity_mismatch')
    info = IMAGE.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1,
            'source_image_identity_invalid')
    require(DATA.stat().st_dev != NEW.stat().st_dev, 'source_target_same_filesystem')
    return row


def read_only_source(expected_uuid):
    row = source_identity(expected_uuid)
    # A read-only VFS bind alone does not freeze the filesystem through other
    # mount namespaces. Require the ext4 superblock itself to remain read-only.
    fs_options = set(row.get('fs-options', '').split(','))
    require(row.get('fstype') == 'ext4' and 'ro' in row.get('options', '').split(',')
            and 'ro' in fs_options and 'rw' not in fs_options, 'source_not_read_only')
    return row


def replacement_fstab(original, expected_uuid):
    require(UUID.fullmatch(expected_uuid), 'target_uuid_invalid')
    require(original.count(BEGIN) == original.count(END) == 1, 'managed_fstab_block_required')
    before, rest = original.split(BEGIN, 1)
    block, after = rest.split(END, 1)
    target = str(DATA)
    def target_rows(text):
        return [line for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith('#')
                and len(line.split()) > 1 and line.split()[1] == target]
    require(not target_rows(before + after), 'unmanaged_data_mount_entry')
    matches = target_rows(block)
    require(len(matches) == 1 and matches[0].split()[0] == str(IMAGE)
            and matches[0].split()[2] == 'ext4', 'original_loop_fstab_entry_required')
    new_line = f'UUID={expected_uuid} {DATA} ext4 nodev,nosuid,noatime 0 2'
    return before + BEGIN + block.replace(matches[0], new_line, 1) + END + after


def atomic_text(path, text, mode=0o600):
    require(not path.is_symlink(), 'output_symlink_rejected')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    backup.fsync_directory(path.parent)


def spool_fstab(original, expected_uuid):
    require(UUID.fullmatch(expected_uuid), 'spool_uuid_invalid')
    require(original.count(SPOOL_BEGIN) == original.count(SPOOL_END)
            and original.count(SPOOL_BEGIN) <= 1, 'spool_fstab_block_invalid')
    expected = f'UUID={expected_uuid} {SPOOL} ext4 nodev,nosuid,noatime 0 2'
    rows = [line.split() for line in original.splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    matches = [row for row in rows if len(row) > 1 and row[1] == str(SPOOL)]
    if matches:
        require(matches == [expected.split()], 'existing_spool_fstab_mismatch')
        return original
    require(SPOOL_BEGIN not in original, 'empty_spool_fstab_block_rejected')
    return original.rstrip() + '\n\n' + SPOOL_BEGIN + '\n' + expected + '\n' + SPOOL_END + '\n'


def mount_spool(disk_id, size_gib, expected_uuid):
    """Mount an already initialized spool; this operation never formats it."""
    require(UUID.fullmatch(expected_uuid), 'spool_uuid_invalid')
    device, row = block_identity(disk_id, size_gib)
    require(row.get('fstype') == 'ext4', 'spool_must_be_initialized_ext4')
    occupied = {name for name in (row.get('mountpoints') or []) if name}
    require(occupied <= {str(SPOOL)}, 'spool_disk_mounted_elsewhere')
    fields = dict(line.split('=', 1) for line in command(['blkid', '-o', 'export', str(device)]).decode().splitlines()
                  if '=' in line)
    require(fields.get('TYPE') == 'ext4' and fields.get('LABEL') == 'pz-backup-spool'
            and fields.get('UUID', '').lower() == expected_uuid.lower(), 'spool_filesystem_identity_mismatch')
    SPOOL.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup.trusted(SPOOL, directory=True)
    already_mounted = SPOOL.is_mount()
    if already_mounted:
        mounted = mount_record(SPOOL, expected_uuid)
        require(Path(mounted['source']).resolve() == device.resolve()
                and {'rw', 'nodev', 'nosuid'}.issubset(set(mounted['options'].split(','))),
                'existing_spool_mount_mismatch')
    else:
        require(not occupied and not any(SPOOL.iterdir()), 'spool_mountpoint_not_empty')
    backup.trusted(FSTAB)
    original = FSTAB.read_text()
    updated = spool_fstab(original, expected_uuid)
    if updated != original:
        # Validate a candidate before replacing the host boot configuration.
        candidate = FSTAB.with_name('fstab.pz-spool-' + uuid.uuid4().hex)
        try:
            atomic_text(candidate, updated)
            command(['findmnt', '--verify', '--tab-file', str(candidate)])
        finally:
            candidate.unlink(missing_ok=True)
        require(FSTAB.read_text() == original, 'fstab_changed_before_spool_mount')
        atomic_text(FSTAB, updated, stat.S_IMODE(FSTAB.stat().st_mode))
        command(['systemctl', 'daemon-reload'])
    if not already_mounted:
        command(['mount', str(SPOOL)])
    mounted = mount_record(SPOOL, expected_uuid)
    require(Path(mounted['source']).resolve() == device.resolve()
            and {'rw', 'nodev', 'nosuid'}.issubset(set(mounted['options'].split(','))),
            'spool_mount_identity_unconfirmed')
    return {'disk_id': disk_id, 'uuid': expected_uuid, 'mount': str(SPOOL)}


def verify_attestation(remote_config, path):
    backup.trusted(remote_config)
    backup.trusted(path)
    cfg = remote.RemoteConfig(**backup.read_json(remote_config, 1024 * 1024))
    record = remote._attestation(cfg, path)
    restored = datetime.fromisoformat(record['restored_at'].replace('Z', '+00:00'))
    require(datetime.now(timezone.utc) - restored <= timedelta(hours=24), 'recent_full_restore_drill_required')
    return record


def writers_stopped(api):
    for kind, name in [('statefulset', 'zomboid'), ('deployment', 'panel')]:
        require(api.get(kind, name)['spec']['replicas'] == 0 and not api.pods(name), 'data_writer_still_present')
    require(api.get('cronjob', 'panel-auto-update')['spec'].get('suspend') is True
            and api.updater_idle(), 'updater_not_suspended_and_idle')
    require(api.get('deployment', 'otel-collector', namespace='observability')['spec']['replicas'] == 0
            and not api.pods('otel-collector', namespace='observability'), 'collector_still_holds_data_mounts')
    api.no_unknown_writers()
    backup.updater_journal()


def source_mount_references(device, proc=Path('/proc')):
    """Read every distinct mount namespace, including private container binds."""
    expected = f'{os.major(device)}:{os.minor(device)}'
    seen, rows = set(), []
    unescape = lambda value: re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            namespace = os.readlink(entry / 'ns/mnt')
            if namespace in seen:
                continue
            lines = (entry / 'mountinfo').read_text().splitlines()
        except FileNotFoundError:
            continue  # The process exited while inspecting it.
        seen.add(namespace)
        for line in lines:
            fields = line.split(' - ', 1)[0].split()
            require(len(fields) >= 6, 'mount_namespace_record_invalid')
            if fields[2] == expected:
                rows.append({'namespace': namespace, 'pid': int(entry.name), 'root': unescape(fields[3]),
                             'target': unescape(fields[4]), 'propagation': fields[6:]})
    return rows


def detachable_references(rows, host_namespace):
    host = [row for row in rows if row['namespace'] == host_namespace]
    if len(host) != 1 or host[0]['target'] != str(DATA) or host[0]['root'] != '/':
        return False  # Kubelet has not released a bind, or an unknown bind exists.
    shared = [item.split(':', 1)[1] for item in host[0]['propagation'] if item.startswith('shared:')]
    for row in rows:
        if row['namespace'] == host_namespace:
            continue
        # Ordinary hardened system services have slave copies that follow the
        # host's shared mount. Private container bind mounts must all be gone.
        if (len(shared) != 1 or row['root'] != '/' or row['target'] != str(DATA)
                or 'master:' + shared[0] not in row['propagation']):
            return False
    return True


def wait_mount_release(timeout=120):
    host_namespace = os.readlink('/proc/1/ns/mnt')
    require(os.readlink('/proc/self/ns/mnt') == host_namespace, 'migration_requires_host_mount_namespace')
    device = DATA.stat().st_dev
    backup.wait_for(lambda: detachable_references(source_mount_references(device), host_namespace), timeout,
                    'old_filesystem_bind_mounts_not_released')
    result = subprocess.run(['fuser', '-m', str(DATA)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=30)
    require(result.returncode in {0, 1} and not result.stdout.strip(), 'old_filesystem_process_handles_remain')


def prepare_target(disk_id, size_gib, target_uuid):
    device, row = block_identity(disk_id, size_gib)
    mounted = mount_record(NEW, target_uuid)
    require(Path(mounted['source']).resolve() == device.resolve() and row.get('fstype') == 'ext4',
            'target_mount_disk_mismatch')
    require({'rw', 'nodev', 'nosuid'}.issubset(set(mounted['options'].split(','))), 'target_mount_options_invalid')
    names = {p.name for p in NEW.iterdir()}
    require(names <= {'lost+found'}, 'new_target_has_existing_data')
    if 'lost+found' in names:
        directory = NEW / 'lost+found'
        require(directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()),
                'new_target_lost_found_not_empty')
    return device


def install_boot_guard(target_uuid, expected_host):
    helper = Path('/opt/pz-backup/disk-migrate.py')
    backup.trusted(helper)
    require(helper.read_bytes() == Path(__file__).read_bytes(), 'installed_migration_helper_differs')
    directory = Path('/etc/systemd/system/k3s.service.d')
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    backup.trusted(directory, directory=True)
    text = (f'[Unit]\nRequiresMountsFor={DATA}\nConditionPathIsMountPoint={DATA}\n'
            f'[Service]\nExecStartPre=/usr/bin/python3 {helper} --expected-host {expected_host} '
            f'check-active --target-uuid {target_uuid}\n')
    atomic_text(directory / '60-pz-data-disk.conf', text, 0o644)


def compare_copy(records, source_uuid, target_uuid, deadline=None):
    read_only_source(source_uuid)
    mount_record(NEW, target_uuid)
    # There are no exclusions during disk migration, including old local ZIPs.
    # Initial source hashes describe the frozen ext4 contents. Recheck every
    # metadata field without rereading those bytes; hash all destination files.
    source_metadata = backup.inventory(DATA, hashes=False, deadline=deadline)
    require(len(records) == len(source_metadata) and all(
        {key: value for key, value in original.items() if key != 'sha256'} == current
        for original, current in zip(records, source_metadata)), 'source_changed_during_disk_copy')
    del source_metadata
    destination = backup.inventory(NEW, deadline=deadline)
    if not any(item['path'] == 'lost+found' for item in records):
        destination = [item for item in destination if item['path'] != 'lost+found']
    require(records == destination, 'disk_copy_hash_or_metadata_mismatch')
    read_only_source(source_uuid)


def migrate(args):
    cfg = backup.configuration(Path(args.config))
    attestation = verify_attestation(Path(args.remote_config), Path(args.attestation))
    require(cfg['data_uuid'].lower() != args.target_uuid.lower(), 'migration_target_already_active')
    device = prepare_target(args.disk_id, args.size_gib, args.target_uuid)
    source = source_identity(cfg['data_uuid'])
    require(OLD.is_dir() and not OLD.is_symlink() and not OLD.is_mount() and not any(OLD.iterdir()),
            'old_mount_directory_must_be_empty')
    original_fstab = FSTAB.read_text()
    updated_fstab = replacement_fstab(original_fstab, args.target_uuid)
    boot_guard = Path('/etc/systemd/system/k3s.service.d/60-pz-data-disk.conf')
    require(not boot_guard.is_symlink(), 'boot_guard_symlink_rejected')
    original_boot_guard = boot_guard.read_bytes() if boot_guard.exists() else None
    original_image = IMAGE.stat()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup.trusted(STATE, directory=True)
    journal_path = STATE / 'journal.json'
    require(not journal_path.exists(), 'previous_disk_migration_requires_operator_review')
    # The global migration lock lives outside both old/new filesystems.
    with backup.locked(backup.LOCK):
        require(os.readlink('/proc/self/ns/mnt') == os.readlink('/proc/1/ns/mnt'),
                'migration_requires_host_mount_namespace')
        source_identity(cfg['data_uuid'])
        prepare_target(args.disk_id, args.size_gib, args.target_uuid)
        preliminary = backup.inventory(DATA, hashes=False)
        total, _ = backup.estimate(preliminary)
        backup.free_space(NEW, total, len(preliminary), cfg)
        controller = backup.Coordinator(cfg)
        controller.api.no_unknown_writers()
        controller.journal_path = journal_path
        controller.journal = {'format': 'pz-disk-migration-v1', 'snapshot_id': 'disk-' + uuid.uuid4().hex,
                              'original': controller.original_state(), 'old_uuid': cfg['data_uuid'],
                              'new_uuid': args.target_uuid, 'disk_id': args.disk_id,
                              'source_loop': source['source'], 'restore_snapshot_id': attestation['snapshot_id'],
                              'restore_commit_sha256': attestation['commit_sha256'], 'started_at': backup.utc()}
        controller.phase('preflight')
        atomic_text(STATE / 'fstab.before', original_fstab)
        controller.suspend_updater()
        evidence = None
        try:
            with ExitStack() as locks:
                updater_lock = DATA / 'panel/.k8s-panel-updater/lock'
                if updater_lock.exists():
                    locks.enter_context(backup.locked(updater_lock))
                controller.stop_collector()
                if controller.journal['original']['zomboid']['spec']['replicas']:
                    evidence = backup.ExitEvidence(controller.api.pods('zomboid')[0])
                controller.phase('writers_stopping')
                for name in ('panel', 'zomboid'):
                    previous = controller.journal['original'][name]
                    if previous['spec']['replicas']:
                        controller.api.patch(previous['kind'], name, previous['uid'], '/spec/replicas', 1, 0)
                backup.wait_for(lambda: not controller.api.pods('panel') and not controller.api.pods('zomboid'),
                                cfg['stop_timeout_seconds'], 'writers_stop_timed_out')
                proof = evidence.verify() if evidence else {'already_stopped': True}
                locks.enter_context(backup.locked(DATA / 'pz-server/.runtime.lock'))
                writers_stopped(controller.api)
                controller.phase('writers_stopped', exit_evidence=proof)
            if evidence:
                evidence.close()
                evidence = None
            # Writable lock handles themselves prevent remount(ro). Close them
            # after shutdown proof; the shared maintenance lock remains held.
            writers_stopped(controller.api)
            wait_mount_release()
            command(['mount', '-o', 'remount,ro', str(DATA)])
            read_only_source(cfg['data_uuid'])
            deadline = time.monotonic() + cfg.get('staging_timeout_seconds', 1800)
            records = backup.inventory(DATA, deadline=deadline)
            controller.phase('copying')
            command(['rsync', '-aHAX', '--numeric-ids', '--one-file-system', '--sparse',
                     str(DATA) + '/', str(NEW) + '/'], timeout=backup.remaining(deadline))
            command(['sync', '-f', str(NEW)], timeout=min(300, backup.remaining(deadline)))
            writers_stopped(controller.api)
            compare_copy(records, cfg['data_uuid'], args.target_uuid, deadline)
            controller.phase('copy_verified', file_count=len(records),
                             manifest_sha256=hashlib.sha256(remote.canonical_json(records)).hexdigest())
            writers_stopped(controller.api)
            wait_mount_release()
            require(FSTAB.read_text() == original_fstab, 'fstab_changed_during_copy')
            install_boot_guard(args.target_uuid, args.expected_host)
            atomic_text(FSTAB, updated_fstab, stat.S_IMODE(FSTAB.stat().st_mode))
            command(['systemctl', 'daemon-reload'])
            command(['findmnt', '--verify', '--tab-file', str(FSTAB)])
            controller.phase('boot_configuration_written')
            command(['umount', str(NEW)])
            command(['umount', str(DATA)])
            # The new fstab entry addresses UUID, never a volatile /dev/vdX name.
            command(['mount', str(DATA)])
            mount_record(DATA, args.target_uuid)
            require(Path(mount_record(DATA, args.target_uuid)['source']).resolve() == device.resolve(),
                    'active_data_device_mismatch')
            controller.phase('new_disk_mounted')
            # The original loop may auto-detach on umount. Reattach the preserved
            # image read-only rather than trusting a possibly reused /dev/loopN.
            command(['mount', '-t', 'ext4', '-o', 'loop,ro,nodev,nosuid,noatime', str(IMAGE), str(OLD)])
            mount_record(OLD, cfg['data_uuid'])
            current_image = IMAGE.stat()
            require((original_image.st_dev, original_image.st_ino, original_image.st_size) ==
                    (current_image.st_dev, current_image.st_ino, current_image.st_size), 'source_image_identity_changed')
            cfg['data_uuid'] = args.target_uuid
            backup.atomic_json(Path(args.config), cfg)
            controller.phase('cutover_verified', old_copy_path=str(OLD))
            controller.restore_apps()
            controller.phase('complete', completed_at=backup.utc())
        except BaseException:
            # Before modifying fstab/mount identity, a timeout/copy failure can
            # safely put the intact original filesystem back into service.
            # Preserve partial target + journal for inspection; never reverse a
            # completed/ambiguous cutover automatically.
            if controller.journal.get('phase') in backup.PRESTOP:
                controller.restore_preparation()
            elif (controller.journal.get('phase') in {'writers_stopped', 'copying', 'copy_verified'}
                  and FSTAB.read_text() == original_fstab
                  and (boot_guard.read_bytes() if boot_guard.exists() else None) == original_boot_guard):
                source_identity(cfg['data_uuid'])
                command(['mount', '-o', 'remount,rw', str(DATA)])
                require('rw' in mount_record(DATA, cfg['data_uuid'])['options'].split(','), 'source_write_mode_not_restored')
                controller.phase('copy_failure_restoring')
                controller.restore_apps()
                controller.phase('aborted_before_cutover', operator_inspection_required=True)
            raise
        finally:
            if evidence:
                evidence.close()
        return {'phase': 'complete', 'new_uuid': args.target_uuid, 'old_copy': str(OLD),
                'data_disk_id': args.disk_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-host', required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    init = commands.add_parser('initialize')
    init.add_argument('--disk-id', required=True)
    init.add_argument('--size-gib', type=int, required=True)
    init.add_argument('--label', choices=['pz-data', 'pz-backup-spool'], required=True)
    init.add_argument('--initialize-empty-disk', required=True, help='Repeat the reviewed newly-created cloud disk ID')
    spool = commands.add_parser('mount-spool')
    spool.add_argument('--disk-id', required=True)
    spool.add_argument('--size-gib', type=int, required=True)
    spool.add_argument('--target-uuid', required=True)
    change = commands.add_parser('migrate')
    change.add_argument('--config', required=True)
    change.add_argument('--remote-config', required=True)
    change.add_argument('--attestation', required=True)
    change.add_argument('--disk-id', required=True)
    change.add_argument('--size-gib', type=int, required=True)
    change.add_argument('--target-uuid', required=True)
    check = commands.add_parser('check-active')
    check.add_argument('--target-uuid', required=True)
    args = parser.parse_args()
    try:
        os.umask(0o077)
        require(os.geteuid() == 0 and socket.gethostname() == args.expected_host,
                'wrong_host_or_not_root')
        require(re.fullmatch(r'[A-Za-z0-9.-]+', args.expected_host), 'host_identifier_invalid')
        if args.action == 'initialize':
            require(args.size_gib > 0, 'disk_size_invalid')
            with backup.locked(backup.LOCK):
                result = initialize(args.disk_id, args.size_gib, args.label, args.initialize_empty_disk)
        elif args.action == 'mount-spool':
            with backup.locked(backup.LOCK):
                result = mount_spool(args.disk_id, args.size_gib, args.target_uuid)
        elif args.action == 'check-active':
            row = mount_record(DATA, args.target_uuid)
            require(not row['source'].startswith('/dev/loop') and 'rw' in row['options'].split(','),
                    'active_data_disk_invalid')
            result = {'uuid': args.target_uuid, 'mounted': True}
        else:
            result = migrate(args)
        print(json.dumps(result, sort_keys=True))
    except (backup.Refused, remote.BackupError, OSError, ValueError, KeyError) as error:
        reason = str(error) if isinstance(error, (backup.Refused, remote.BackupError)) else type(error).__name__
        print(json.dumps({'error': 'disk_migration_refused', 'action': args.action,
                          'reason': reason,
                          'recovery': 'inspect private journal; never delete old loop or force-unmount'}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
