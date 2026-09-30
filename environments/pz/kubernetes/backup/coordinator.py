#!/usr/bin/env python3
"""Host-side, fail-closed PZ staging coordinator. See backup/README.md.

Only this trusted root helper touches live data. Publication uses independent
credentials/processes and consumes ciphertext in *.ready. A reboot during an
uncertain stop deliberately leaves the writers stopped for operator inspection.
"""
import argparse
import base64
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid

DATA = Path('/srv/pz-storage/zomboid')
SPOOL = Path('/srv/pz-backup-spool')
STATE = Path('/var/lib/pz-backup')
LOCK = Path('/var/lib/pz-volumes/migration.lock')
K3S = '/usr/local/bin/k3s'
FORMAT = 'pz-backup-v1'
MANIFEST_MAX_BYTES = 512 * 1024 * 1024
SUBDIRS = {'pz-server', 'zomboid', 'steam', 'panel', 'panel-logs'}
IDENTIFIER = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{32}\Z')
TERMINAL = {'ready', 'preparation_failed', 'capture_failed'}
RECOVERABLE = {'staging_verified', 'apps_restoring', 'apps_restored', 'archiving', 'ciphertext_verified', 'ready_finalizing'}
PRESTOP = {'updater_suspended', 'preparing_recovery', 'preparation_restoring', 'collector_stopping', 'collector_stopped'}
ALLOWED_EXCLUSIONS = {'zomboid/backups', 'panel/.k8s-panel-updater/backups'}


class Refused(Exception):
    """A fixed reason safe to publish without filenames or secret values."""


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remaining(deadline):
    if deadline is None:
        return None
    seconds = deadline - time.monotonic()
    require(seconds > 0, 'staging_deadline_exceeded')
    return seconds


def atomic_json(path, value, deadline=None):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    digest = hashlib.sha256()
    with os.fdopen(fd, 'w') as output:
        for chunk in json.JSONEncoder(sort_keys=True, separators=(',', ':')).iterencode(value):
            remaining(deadline)
            output.write(chunk)
            digest.update(chunk.encode())
        output.write('\n')
        digest.update(b'\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)
    fsync_directory(path.parent)
    require(file_hash(path, deadline) == digest.hexdigest(), 'journal_readback_failed')


def read_json(path, max_bytes=512 * 1024 * 1024):
    require(not path.is_symlink(), 'json_symlink_rejected')
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_size <= max_bytes, 'json_file_invalid')
    return json.loads(path.read_text())


def trusted(path, directory=False):
    """Check every component; a writable parent can replace a trusted file."""
    require(path.is_absolute(), 'trusted_path_not_absolute')
    for component in [*reversed(path.parents), path]:
        info = component.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0
                and info.st_mode & 0o022 == 0, 'trusted_path_permissions_invalid')
    require(path.is_dir() if directory else path.is_file(), 'trusted_path_type_invalid')


def command(args, timeout=60):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused('command_unavailable_or_timed_out') from None
    require(result.returncode == 0, 'command_failed')
    return result.stdout


def configuration(path):
    trusted(path)
    cfg = read_json(path, 1024 * 1024)
    require(cfg.get('data_root') == str(DATA) and cfg.get('spool') == str(SPOOL), 'fixed_paths_required')
    for key in ('data_uuid', 'spool_uuid'):
        require(re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', cfg.get(key, '')),
                'mount_uuid_required')
    require(cfg['data_uuid'] != cfg['spool_uuid'], 'separate_spool_filesystem_required')
    require(re.fullmatch(r'age1[0-9a-z]{58}', cfg.get('age_recipient', '')), 'age_public_recipient_required')
    require(re.fullmatch(r'[0-9a-f]{40}', cfg.get('infra_revision', '')), 'infra_revision_required')
    require(re.fullmatch(r'[A-Za-z0-9._-]+', cfg.get('server_name', '')), 'server_name_required')
    for key in ('min_free_bytes', 'min_free_inodes', 'max_snapshot_bytes', 'max_entries', 'uploader_gid'):
        require(type(cfg.get(key)) is int and cfg[key] > 0, 'positive_budget_and_gid_required')
    require(type(cfg.get('stop_timeout_seconds')) is int and 1 <= cfg['stop_timeout_seconds'] <= 300,
            'stop_timeout_must_fit_game_grace_period')
    require(type(cfg.get('staging_timeout_seconds', 1800)) is int and 1 <= cfg.get('staging_timeout_seconds', 1800) <= 1800,
            'staging_timeout_must_fit_maintenance_window')
    require(cfg.get('recovery_helper') == '/opt/pz-backup/gather-recovery.py', 'fixed_recovery_helper_required')
    excluded = cfg.get('excluded_paths', [])
    require(isinstance(excluded, list) and all(isinstance(p, str) and p in ALLOWED_EXCLUSIONS for p in excluded)
            and len(excluded) == len(set(excluded)), 'unapproved_exclusions_rejected')
    trusted(Path(cfg['recovery_helper']))
    return cfg


@contextmanager
def locked(path, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(descriptor).st_mode), 'lock_not_regular_file')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('maintenance_lock_busy') from None
        yield descriptor
    finally:
        os.close(descriptor)


def check_mount(path, expected_uuid):
    require(path.is_dir() and not path.is_symlink(), 'mount_directory_invalid')
    value = json.loads(command(['findmnt', '--json', '--mountpoint', str(path), '-o', 'TARGET,UUID,FSTYPE']))
    rows = value.get('filesystems', [])
    require(len(rows) == 1 and rows[0]['target'] == str(path)
            and isinstance(rows[0].get('uuid'), str) and rows[0]['uuid'].lower() == expected_uuid.lower()
            and rows[0]['fstype'] == 'ext4', 'mount_identity_mismatch')


def free_space(path, bytes_needed, inodes_needed, cfg):
    space = os.statvfs(path)
    require(space.f_bavail * space.f_frsize >= bytes_needed + cfg['min_free_bytes'], 'insufficient_free_bytes')
    require(space.f_favail >= inodes_needed + cfg['min_free_inodes'], 'insufficient_free_inodes')


def xattrs(path):
    return {key: base64.b64encode(os.getxattr(path, key, follow_symlinks=False)).decode('ascii')
            for key in sorted(os.listxattr(path, follow_symlinks=False))}


def file_hash(path, deadline=None):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            remaining(deadline)
            digest.update(chunk)
        after = os.fstat(source.fileno())
    require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'file_changed_while_reading')
    return digest.hexdigest()


def inventory(root, *, hashes=True, max_entries=2000000, excluded=(), deadline=None):
    """Do not follow links or cross mounts. Include binary, log and backup data."""
    root = root.absolute()
    root_stat = root.lstat()
    require(stat.S_ISDIR(root_stat.st_mode), 'inventory_root_invalid')
    records = []
    pending = [root]
    hardlinks = {}
    while pending:
        remaining(deadline)
        path = pending.pop()
        info = path.lstat()
        require(info.st_dev == root_stat.st_dev, 'nested_mount_rejected')
        relative = '.' if path == root else path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        item = {'path': relative, 'uid': info.st_uid, 'gid': info.st_gid,
                'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns,
                'xattrs': xattrs(path)}
        if stat.S_ISDIR(info.st_mode):
            item['type'] = 'dir'
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            item.update(type='file', size=info.st_size)
            if info.st_nlink > 1:
                identity = (info.st_dev, info.st_ino)
                if identity in hardlinks:
                    item['hardlink'] = hardlinks[identity]
                else:
                    hardlinks[identity] = relative
            if hashes:
                item['sha256'] = file_hash(path, deadline)
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            require(not os.path.isabs(target), 'absolute_symlink_rejected')
            try:
                path.resolve(strict=True).relative_to(root)
            except (ValueError, OSError, RuntimeError):
                raise Refused('escaping_or_dangling_symlink_rejected') from None
            item.update(type='symlink', target=target)
        else:
            raise Refused('special_file_rejected')
        records.append(item)
        require(len(records) <= max_entries, 'inventory_entry_budget_exceeded')
    return sorted(records, key=lambda row: row['path'])


def estimate(records):
    # No compression assumption; sparse source files are budgeted by logical size.
    logical = sum(row.get('size', 0) for row in records)
    metadata = sum(8192 + len(json.dumps(row).encode()) * 2 for row in records)
    return logical, 2 * logical + metadata + 64 * 1024 * 1024


class Kubernetes:
    def get(self, kind, name=None, selector=None, all_namespaces=False, namespace='zomboid'):
        require(namespace in {'zomboid', 'observability'}, 'unexpected_workload_namespace')
        # --all-namespaces belongs to the get subcommand, unlike the persistent
        # namespace flag. Putting -A before get is rejected by kubectl.
        args = [K3S, 'kubectl', 'get', kind, *(['-A'] if all_namespaces else ['-n', namespace])]
        if name:
            args.append(name)
        if selector:
            args += ['-l', selector]
        return json.loads(command(args + ['-o', 'json']))

    def patch(self, kind, name, original_uid, field, previous, value, namespace='zomboid'):
        current = self.get(kind, name, namespace=namespace)
        require(current['metadata']['uid'] == original_uid, 'workload_identity_changed')
        operations = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
                      {'op': 'test', 'path': field, 'value': previous},
                      {'op': 'replace', 'path': field, 'value': value}]
        command([K3S, 'kubectl', '-n', namespace, 'patch', kind, name, '--type=json', '-p', json.dumps(operations)])

    def pods(self, app, namespace='zomboid'):
        return self.get('pods', selector='app.kubernetes.io/name=' + app, namespace=namespace)['items']

    def updater_idle(self):
        jobs = self.get('jobs', selector='app.kubernetes.io/name=panel-auto-update')['items']
        unfinished = any(not any(c.get('type') in {'Complete', 'Failed'} and c.get('status') == 'True'
                                 for c in j.get('status', {}).get('conditions', []))
                         or j['metadata'].get('deletionTimestamp') for j in jobs)
        pods = self.pods('panel-auto-update')
        active = any(p['metadata'].get('deletionTimestamp') or p.get('status', {}).get('phase')
                     not in {'Succeeded', 'Failed'} for p in pods)
        return not unfinished and not active

    def no_unknown_writers(self):
        claims = {(row['metadata']['namespace'], row['metadata']['name'])
                  for row in self.get('pvc', all_namespaces=True)['items']
                  if row.get('spec', {}).get('volumeName') in {'pz-' + name for name in SUBDIRS}}
        for pod in self.get('pods', all_namespaces=True)['items']:
            if pod.get('status', {}).get('phase') in {'Succeeded', 'Failed'} and not pod['metadata'].get('deletionTimestamp'):
                continue
            volumes = pod.get('spec', {}).get('volumes', [])
            namespace = pod['metadata']['namespace']
            containers = [*pod.get('spec', {}).get('containers', []), *pod.get('spec', {}).get('initContainers', []),
                          *pod.get('spec', {}).get('ephemeralContainers', [])]
            writable_mounts = {mount['name'] for container in containers for mount in container.get('volumeMounts', [])
                               if not mount.get('readOnly', False)}
            # Linux read-only bind mounts are not recursively read-only by
            # default. An ancestor hostPath (notably /hostfs) can expose this
            # data filesystem as a writable nested mount; include it as writer.
            nonrecursive = {mount['name'] for container in containers for mount in container.get('volumeMounts', [])
                            if mount.get('recursiveReadOnly') != 'Enabled'}
            for volume in volumes:
                host_path = volume.get('hostPath', {}).get('path')
                if host_path:
                    resolved = Path(host_path).resolve()
                    if resolved != DATA and DATA.is_relative_to(resolved) and volume.get('name') in nonrecursive:
                        writable_mounts.add(volume['name'])
            writes_data = any(v.get('name') in writable_mounts and
                             ((namespace, v.get('persistentVolumeClaim', {}).get('claimName')) in claims
                              or (v.get('hostPath', {}).get('path') and
                                  (Path(v['hostPath']['path']).resolve().is_relative_to(DATA)
                                   or DATA.is_relative_to(Path(v['hostPath']['path']).resolve())))) for v in volumes)
            if writes_data:
                app = pod['metadata'].get('labels', {}).get('app.kubernetes.io/name')
                require((namespace == 'zomboid' and app in {'zomboid', 'panel', 'panel-auto-update'})
                        or (namespace == 'observability' and app == 'otel-collector'), 'unknown_data_writer')


def wait_for(predicate, seconds, reason):
    deadline = time.monotonic() + seconds
    while not predicate():
        require(time.monotonic() < deadline, reason)
        time.sleep(1)


def updater_journal():
    managed = DATA / 'panel/.k8s-panel-updater'
    require(not managed.is_symlink(), 'updater_directory_symlink')
    path = DATA / 'panel/.k8s-panel-updater/journal.json'
    if not path.exists():
        require(not path.is_symlink(), 'updater_journal_symlink')
        return
    require(read_json(path).get('phase') in {'committed', 'rolled_back'}, 'updater_journal_not_terminal')


def disk_migration_ready():
    path = STATE / 'disk-migration/journal.json'
    require(not path.parent.is_symlink() and not path.is_symlink(), 'disk_migration_journal_symlink')
    if path.exists():
        require(read_json(path).get('phase') == 'complete', 'disk_migration_incomplete_requires_operator')


class ExitEvidence:
    """Subscribe before scale-down; preserve an open CRI log across pod GC.

    Container deletion alone never proves a clean exit. Require the runtime's
    post-child-exit log, containerd /tasks/exit status 0 (or retained CRI status),
    and absence of all processes in the captured container cgroup.
    """
    def __init__(self, pod):
        statuses = [s for s in pod.get('status', {}).get('containerStatuses', []) if s['name'] == 'zomboid']
        require(len(statuses) == 1 and 'running' in statuses[0].get('state', {}), 'game_not_running_cleanly')
        self.container = statuses[0]['containerID'].removeprefix('containerd://')
        require(re.fullmatch(r'[0-9a-f]{64}', self.container), 'container_id_invalid')
        inspection = json.loads(command([K3S, 'crictl', 'inspect', self.container]))
        pid = inspection['info']['pid']
        require(type(pid) is int and pid > 1, 'container_pid_unavailable')
        self.pid = pid
        self.pod_uid = pod['metadata']['uid']
        cgroups = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
        unified = [line.split(':', 2)[2] for line in cgroups if line.startswith('0::')]
        require(len(unified) == 1 and unified[0].startswith('/') and '..' not in unified[0].split('/'),
                'unified_cgroup_required')
        self.cgroup = Path('/sys/fs/cgroup') / unified[0].lstrip('/')
        log_path = Path(inspection['status']['logPath'])
        require(log_path.is_absolute() and log_path.is_relative_to('/var/log/pods') and not log_path.is_symlink(),
                'cri_log_path_invalid')
        self.log = log_path.open('rb')
        self.log.seek(0, io.SEEK_END)
        self.events = []
        self.invalid_matching_event = False
        self.process = subprocess.Popen([K3S, 'ctr', '-n', 'k8s.io', 'events'], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
        self.thread = threading.Thread(target=self.collect, daemon=True)
        self.thread.start()
        time.sleep(1)
        require(self.process.poll() is None, 'exit_event_subscription_failed')

    def valid_event(self, event):
        # ctr encodes TaskExit using protobuf JSON defaults: successful exit
        # omits exit_status. Only a fully identified, typed TaskExit may use 0.
        if not isinstance(event, dict) or set(event) - {'container_id', 'id', 'pid', 'exit_status', 'exited_at'}:
            return False
        exited_at = event.get('exited_at')
        status = event.get('exit_status', 0)
        return (event.get('container_id') == self.container and event.get('id') == self.container
                and type(event.get('pid')) is int and event['pid'] == self.pid
                and isinstance(exited_at, dict) and not set(exited_at) - {'seconds', 'nanos'}
                and type(exited_at.get('seconds')) is int and exited_at['seconds'] > 0
                and type(exited_at.get('nanos', 0)) is int and 0 <= exited_at.get('nanos', 0) < 1000000000
                and type(status) is int and 0 <= status <= 0xffffffff)

    def collect(self):
        for line in self.process.stdout:
            header, separator, body = line.partition('{')
            if not separator or header.split()[-2:] != ['k8s.io', '/tasks/exit']:
                continue
            try:
                event = json.loads('{' + body)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get('container_id') != self.container or event.get('id') != self.container:
                continue
            if self.valid_event(event):
                self.events.append(event)
            else:
                self.invalid_matching_event = True

    def verify(self):
        time.sleep(0.2)
        events = list(self.events)
        invalid = self.invalid_matching_event or any(not self.valid_event(event) for event in events)
        exited = bool(events) and not invalid and all(event.get('exit_status', 0) == 0 for event in events)
        retained_cri_exit = None
        if not events and not invalid:
            try:
                inspection = json.loads(command([K3S, 'crictl', 'inspect', self.container]))
                status_value = inspection['status']
                code = status_value.get('exitCode')
                if status_value.get('id') == self.container and status_value.get('state') == 'CONTAINER_EXITED' and type(code) is int:
                    retained_cri_exit = code
                    exited = code == 0
            except (Refused, KeyError, ValueError):
                pass
        logs = self.log.read(8 * 1024 * 1024)
        requested = b'Shutdown requested: saving world and requesting graceful quit' in logs
        child_clean = b'Game process exited with code 0' in logs
        try:
            cgroup_empty = not self.cgroup.exists() or not any(
                path.read_text().strip() for path in self.cgroup.rglob('cgroup.procs'))
        except OSError:
            cgroup_empty = None
        # Save only runtime phrase booleans; never persist raw player logs. Do
        # this before any consistency refusal, while the deleted CRI fd is open.
        proof_path = STATE / ('container-exit-' + self.container + '.json')
        atomic_json(proof_path, {'container_id': self.container, 'pod_uid': self.pod_uid, 'pid': self.pid,
                                'observed_at': utc(), 'topic': '/tasks/exit', 'namespace': 'k8s.io',
                                'events': events, 'invalid_matching_event': invalid,
                                'retained_cri_exit_code': retained_cri_exit,
                                'runtime_shutdown_requested': requested, 'runtime_child_exit_zero': child_clean,
                                'cgroup_empty': cgroup_empty})
        require(exited, 'clean_container_exit_not_observed')
        require(requested and child_clean, 'clean_game_process_exit_not_observed')
        require(cgroup_empty is True, 'game_process_still_exists')
        return {'container_id': self.container, 'exit_code': 0, 'runtime_child_exit': 0, 'cgroup_empty': True,
                'durable_evidence': str(proof_path)}

    def close(self):
        self.log.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()  # Only the event observer; never an application.
            self.process.wait()
        self.thread.join(timeout=5)


class HashTee:
    def __init__(self, source, destination=None, deadline=None):
        self.source, self.destination, self.deadline = source, destination, deadline
        self.digest, self.size = hashlib.sha256(), 0

    def read(self, size=-1):
        remaining(self.deadline)
        data = self.source.read(size)
        remaining(self.deadline)
        if data:
            if self.destination is not None:
                self.destination.write(data)
            self.digest.update(data)
            self.size += len(data)
        return data


def verify_tar(stream, records, manifest_bytes, deadline=None):
    expected = {r['path']: r for r in records}
    seen = set()
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        for member in archive:
            remaining(deadline)
            name = member.name.rstrip('/')
            require(name not in seen, 'archive_duplicate_member')
            seen.add(name)
            if name == 'manifest.json':
                require(member.isfile() and member.size == len(manifest_bytes), 'archive_manifest_mismatch')
                content = archive.extractfile(member)
                offset = 0
                for chunk in iter(lambda: content.read(1024 * 1024), b''):
                    remaining(deadline)
                    require(chunk == manifest_bytes[offset:offset + len(chunk)], 'archive_manifest_mismatch')
                    offset += len(chunk)
                require(offset == len(manifest_bytes), 'archive_manifest_mismatch')
                archive.members.clear()
                continue
            require(name in expected, 'archive_unexpected_member')
            item = expected[name]
            require((member.uid, member.gid, member.mode) == (item['uid'], item['gid'], item['mode']),
                    'archive_metadata_mismatch')
            require(int(Decimal(member.pax_headers.get('mtime', str(member.mtime))) * 10 ** 9) == item['mtime_ns'],
                    'archive_mtime_mismatch')
            if item['type'] == 'file':
                if item.get('hardlink'):
                    require(member.islnk() and member.linkname == item['hardlink'] and member.linkname in seen,
                            'archive_hardlink_mismatch')
                else:
                    require(member.isfile() and member.size == item['size'], 'archive_file_type_or_size_mismatch')
                    content = archive.extractfile(member)
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: content.read(1024 * 1024), b''):
                        remaining(deadline)
                        digest.update(chunk)
                    require(digest.hexdigest() == item['sha256'], 'archive_sha256_mismatch')
            elif item['type'] == 'dir':
                require(member.isdir(), 'archive_directory_type_mismatch')
            else:
                require(member.issym() and member.linkname == item['target'], 'archive_symlink_mismatch')
            # ACLs are also carried as binary xattrs using --xattrs-include=*.
            for key, value in item['xattrs'].items():
                if member.islnk():
                    require(expected[member.linkname]['xattrs'].get(key) == value, 'archive_hardlink_xattr_mismatch')
                    continue
                archived = member.pax_headers.get('SCHILY.xattr.' + key)
                require(archived is not None and base64.b64encode(archived.encode('utf-8', 'surrogateescape')).decode() == value,
                        'archive_xattr_mismatch')
            # Python versions before TarFile(stream=True) retain TarInfo objects
            # even in r| mode. We validate hardlinks against expected/seen rather
            # than tarfile's member index, so discard that redundant large cache.
            archive.members.clear()
    require(seen == set(expected) | {'manifest.json'}, 'archive_inventory_mismatch')
    # Consume padding/trailing records too, so encryption/hash sees every byte.
    while stream.read(1024 * 1024):
        remaining(deadline)
    remaining(deadline)


def stop_children(processes):
    """Reap only subprocesses created by this archive operation, even on SIGINT."""
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def create_staging_tar(stage, manifest, deadline):
    """Write one plaintext file, then verify every byte/member from disk."""
    temporary, target = stage / 'staging.tar.partial', stage / 'staging.tar'
    require(not target.exists() and not target.is_symlink(), 'staging_tar_exists')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        # Only member and hardlink names are rewritten. Relative symlink targets
        # must keep their original text. Source DATA is fixed and named zomboid.
        require(DATA.name == 'zomboid', 'unexpected_data_root_name')
        args = ['tar', '--format=pax', '--sort=name', '--acls', '--xattrs', '--xattrs-include=*',
                '--pax-option=delete=atime,delete=ctime', '--numeric-owner', '--one-file-system',
                r'--transform=flags=rh;s,^zomboid\(/\|$\),data\1,', '--anchored',
                *['--exclude=zomboid/' + path for path in manifest['excluded']],
                '-cf', '-', '-C', str(DATA.parent), DATA.name,
                '-C', str(stage), 'recovery', 'manifest.json']
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.DEVNULL)
        try:
            try:
                code = process.wait(timeout=remaining(deadline))
            except subprocess.TimeoutExpired:
                raise Refused('staging_deadline_exceeded') from None
            require(code == 0, 'staging_tar_failed')
            output.flush()
            os.fsync(output.fileno())
            remaining(deadline)
        except BaseException:
            stop_children([process])
            raise
    fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        stream = HashTee(source, deadline=deadline)
        verify_tar(stream, manifest['files'], (stage / 'manifest.json').read_bytes(), deadline)
        result = {'path': 'staging.tar', 'sha256': stream.digest.hexdigest(), 'size': stream.size}
    require(temporary.stat().st_size == result['size'], 'staging_tar_size_changed')
    os.rename(temporary, target)
    fsync_directory(stage)
    remaining(deadline)
    return result


def encrypted_archive(stage, target, manifest, recipient, staged_tar=None):
    """Verify each plaintext member while forwarding the identical bytes to age.

    Legacy directory staging remains readable for interrupted older snapshots.
    New staging uses the previously verified TAR; every retry checks its durable
    hash before starting and checks the bytes again while feeding encryption.
    """
    source = None
    processes = []
    if staged_tar is not None:
        require(isinstance(staged_tar, dict) and staged_tar.get('path') == 'staging.tar'
                and type(staged_tar.get('size')) is int and staged_tar['size'] > 0
                and isinstance(staged_tar.get('sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', staged_tar['sha256']), 'staging_tar_record_invalid')
        plain = stage / 'staging.tar'
        require(not plain.is_symlink() and plain.is_file()
                and plain.stat().st_size == staged_tar['size']
                and file_hash(plain) == staged_tar['sha256'], 'staging_tar_changed')
    tmp = target.with_suffix('.partial')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        try:
            if staged_tar is not None:
                source = os.fdopen(os.open(plain, os.O_RDONLY | os.O_NOFOLLOW), 'rb')
            else:
                tar = subprocess.Popen(['tar', '--format=pax', '--sort=name', '--acls', '--xattrs',
                                        '--xattrs-include=*', '--pax-option=delete=atime,delete=ctime', '--numeric-owner',
                                        '-C', str(stage), '-cf', '-', 'data', 'recovery', 'manifest.json'],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                processes.append(tar)
                source = tar.stdout
            zstd = subprocess.Popen(['zstd', '-T1', '-3', '--stdout'], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            processes.append(zstd)
            age = subprocess.Popen(['age', '-r', recipient], stdin=zstd.stdout, stdout=output, stderr=subprocess.DEVNULL)
            processes.append(age)
            zstd.stdout.close()
            stream = HashTee(source, zstd.stdin)
            verify_tar(stream, manifest['files'], (stage / 'manifest.json').read_bytes())
            if staged_tar is not None:
                require(stream.size == staged_tar['size'] and stream.digest.hexdigest() == staged_tar['sha256'],
                        'staging_tar_changed_during_encryption')
            zstd.stdin.close()
            require(all(process.wait(timeout=60) == 0 for process in processes), 'archive_pipeline_failed')
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            stop_children(processes)
            raise
        finally:
            if source:
                source.close()
            for process in processes:
                for handle in (process.stdin, process.stdout):
                    if handle and not handle.closed:
                        try:
                            handle.close()
                        except OSError:
                            pass
    os.rename(tmp, target)
    fsync_directory(target.parent)
    return {'sha256': file_hash(target), 'size': target.stat().st_size,
            'plaintext_sha256': stream.digest.hexdigest(), 'plaintext_size': stream.size}


def encrypt_manifest(source, target, recipient):
    tmp = target.with_suffix('.partial')
    require(not tmp.exists(), 'encryption_partial_exists')
    command(['age', '-r', recipient, '-o', str(tmp), str(source)], timeout=60)
    with tmp.open('rb') as descriptor:
        os.fsync(descriptor.fileno())
    os.rename(tmp, target)
    fsync_directory(target.parent)
    return {'sha256': file_hash(target), 'size': target.stat().st_size}


class Coordinator:
    def __init__(self, cfg, api=None):
        self.cfg, self.api = cfg, api or Kubernetes()
        self.journal_path = STATE / 'journal.json'
        self.journal = None

    def phase(self, phase, **fields):
        timestamp_key = {'writers_stopped': 'writers_stopped_at', 'staging': 'staging_started_at',
                         'staging_verified': 'staging_verified_at', 'archiving': 'archive_started_at',
                         'ready': 'ready_at'}.get(phase)
        if timestamp_key:
            self.journal.setdefault(timestamp_key, utc())
        self.journal.update(phase=phase, updated_at=utc(), **fields)
        atomic_json(self.journal_path, self.journal)
        print(json.dumps({'snapshot_id': self.journal['snapshot_id'], 'phase': phase}), flush=True)

    def preflight(self):
        check_mount(DATA, self.cfg['data_uuid'])
        check_mount(SPOOL, self.cfg['spool_uuid'])
        trusted(SPOOL, directory=True)
        require(DATA.stat().st_dev != SPOOL.stat().st_dev, 'separate_spool_device_required')
        require(SUBDIRS.issubset({p.name for p in DATA.iterdir()}), 'required_data_directories_missing')
        for name in SUBDIRS:
            require((DATA / name).is_dir() and not (DATA / name).is_symlink(), 'data_subdirectory_invalid')
        records = self.data_inventory(hashes=False)
        total, required = estimate(records)
        require(total <= self.cfg['max_snapshot_bytes'], 'snapshot_budget_exceeded')
        free_space(SPOOL, required, len(records) + 100, self.cfg)
        free_space(DATA, 0, 0, self.cfg)
        free_space(Path('/'), 0, 0, self.cfg)
        self.api.no_unknown_writers()
        return {'bytes': total, 'entries': len(records), 'required_spool_bytes': required}

    def data_inventory(self, hashes=True, deadline=None):
        exclusions = self.cfg.get('excluded_paths', [])
        if exclusions:
            require((DATA / 'zomboid/Saves').is_dir() and (DATA / 'zomboid/Server').is_dir(),
                    'primary_world_paths_required_before_backup_exclusion')
        return inventory(DATA, hashes=hashes, max_entries=self.cfg['max_entries'], excluded=exclusions, deadline=deadline)

    def original_state(self):
        original = {}
        for name, kind, namespace in [('zomboid', 'statefulset', 'zomboid'), ('panel', 'deployment', 'zomboid'),
                                     ('panel-auto-update', 'cronjob', 'zomboid'), ('otel-collector', 'deployment', 'observability')]:
            doc = self.api.get(kind, name, namespace=namespace)
            original[name] = {'kind': kind, 'namespace': namespace, 'uid': doc['metadata']['uid'], 'spec': doc['spec']}
        for name in ('zomboid', 'panel', 'otel-collector'):
            require(original[name]['spec'].get('replicas') in {0, 1}, 'unsupported_replica_count')
            pods = self.api.pods(name, namespace=original[name]['namespace'])
            if original[name]['spec']['replicas'] == 0:
                require(not pods, 'stopped_workload_still_has_pods')
            else:
                require(len(pods) == 1 and not pods[0]['metadata'].get('deletionTimestamp'), 'workload_pod_ambiguous')
        return original

    def restore_replica(self, name):
        original = self.journal['original'][name]
        kwargs = {'namespace': original['namespace']} if original.get('namespace') != 'zomboid' and original.get('namespace') else {}
        current = self.api.get(original['kind'], name, **kwargs)
        require(current['metadata']['uid'] == original['uid'], 'workload_identity_changed')
        desired = original['spec']['replicas']
        actual = current['spec']['replicas']
        require(actual in {0, desired}, 'workload_replicas_changed')
        if actual != desired:
            self.api.patch(original['kind'], name, original['uid'], '/spec/replicas', 0, desired, **kwargs)

    def stop_collector(self):
        original = self.journal['original']['otel-collector']
        self.phase('collector_stopping')
        if original['spec']['replicas']:
            self.api.patch('deployment', 'otel-collector', original['uid'], '/spec/replicas', 1, 0,
                           namespace='observability')
        wait_for(lambda: not self.api.pods('otel-collector', namespace='observability'),
                 self.cfg['stop_timeout_seconds'], 'collector_stop_timed_out')
        self.phase('collector_stopped')

    def suspend_updater(self):
        updater = self.journal['original']['panel-auto-update']
        previous = updater['spec'].get('suspend', False)
        require(type(previous) is bool, 'updater_suspend_invalid')
        self.phase('updater_suspending')
        if not previous:
            self.api.patch('cronjob', 'panel-auto-update', updater['uid'], '/spec/suspend', False, True)
        wait_for(self.api.updater_idle, 900, 'updater_did_not_become_idle')
        updater_journal()
        # A running updater may have changed replicas while draining. Record the
        # settled application state, retaining the schedule's initial setting.
        settled = self.original_state()
        settled['panel-auto-update'] = updater
        self.phase('updater_suspended', original=settled)

    def restore_apps(self):
        self.phase('apps_restoring')
        for name in ('zomboid', 'panel'):
            self.restore_replica(name)
        if 'otel-collector' in self.journal['original']:
            self.restore_replica('otel-collector')
        updater_journal()
        require(self.api.updater_idle(), 'updater_conflict_before_restore')
        updater = self.journal['original']['panel-auto-update']
        current = self.api.get('cronjob', 'panel-auto-update')
        require(current['metadata']['uid'] == updater['uid'], 'updater_identity_changed')
        if not updater['spec'].get('suspend', False) and current['spec'].get('suspend', False):
            self.api.patch('cronjob', 'panel-auto-update', updater['uid'], '/spec/suspend', True, False)
        self.phase('apps_restored', downtime_finished_at=utc())

    def restore_preparation(self):
        """Before writers_stopping, only the updater schedule was mutated."""
        for name in ('zomboid', 'panel'):
            original = self.journal['original'][name]
            current = self.api.get(original['kind'], name)
            require(current['metadata']['uid'] == original['uid']
                    and current['spec']['replicas'] == original['spec']['replicas'],
                    'workload_changed_before_preparation_rollback')
        with ExitStack() as guards:
            updater_lock = DATA / 'panel/.k8s-panel-updater/lock'
            if updater_lock.exists():
                guards.enter_context(locked(updater_lock))
            require(self.api.updater_idle(), 'updater_conflict_before_preparation_rollback')
            updater_journal()
            updater = self.journal['original']['panel-auto-update']
            current = self.api.get('cronjob', 'panel-auto-update')
            require(current['metadata']['uid'] == updater['uid'], 'updater_identity_changed')
            desired = updater['spec'].get('suspend', False)
            self.phase('preparation_restoring')
            if 'otel-collector' in self.journal['original']:
                self.restore_replica('otel-collector')
            if current['spec'].get('suspend', False) != desired:
                self.api.patch('cronjob', 'panel-auto-update', updater['uid'], '/spec/suspend', True, desired)
            self.phase('preparation_failed')

    def restore_failed_capture(self):
        proof = self.journal.get('exit_evidence', {})
        require(proof.get('already_stopped') or (proof.get('exit_code') == 0
                and proof.get('runtime_child_exit') == 0 and proof.get('cgroup_empty') is True),
                'failed_capture_has_no_clean_exit_proof')
        self.phase('capture_failure_restoring', failed_capture=True)
        self.restore_apps()
        self.phase('capture_failed')

    def capture(self, partial):
        self.suspend_updater()
        with ExitStack() as locks:
            updater_lock = DATA / 'panel/.k8s-panel-updater/lock'
            if updater_lock.exists():
                locks.enter_context(locked(updater_lock))
            updater_journal()
            recovery = partial / 'recovery'
            recovery.mkdir(mode=0o700)
            self.phase('preparing_recovery')
            command(['python3', self.cfg['recovery_helper'], '--output', str(recovery)], timeout=3600)
            index = read_json(recovery / 'index.json')
            require(index.get('complete') is True and index.get('images') and index.get('secrets'),
                    'recovery_artifacts_incomplete')
            recovery_records = inventory(recovery, hashes=False, max_entries=self.cfg['max_entries'])
            data_records = self.data_inventory(hashes=False)
            logical, required = estimate(recovery_records + data_records)
            require(logical <= self.cfg['max_snapshot_bytes'], 'snapshot_budget_exceeded')
            free_space(SPOOL, required, len(data_records) + 100, self.cfg)
            del recovery_records, data_records
            self.stop_collector()
            evidence = None
            runtime_guard = None
            original_game = self.journal['original']['zomboid']
            if original_game['spec']['replicas']:
                pod = self.api.pods('zomboid')[0]
                self.journal['game_pod_uid'] = pod['metadata']['uid']
                evidence = ExitEvidence(pod)
            try:
                self.phase('writers_stopping', downtime_started_at=utc())
                for name in ('panel', 'zomboid'):
                    previous = self.journal['original'][name]
                    if previous['spec']['replicas']:
                        self.api.patch(previous['kind'], name, previous['uid'], '/spec/replicas', 1, 0)
                wait_for(lambda: not self.api.pods('panel') and not self.api.pods('zomboid'),
                         self.cfg['stop_timeout_seconds'], 'writers_stop_timed_out')
                proof = evidence.verify() if evidence else {'already_stopped': True}
                runtime_lock = DATA / 'pz-server/.runtime.lock'
                require(runtime_lock.exists(), 'runtime_lock_missing')
                runtime_guard = locked(runtime_lock)
                runtime_guard.__enter__()
                require(self.api.updater_idle(), 'updater_started_during_stop')
                require(not self.api.pods('otel-collector', namespace='observability'), 'collector_restarted_during_stop')
                self.api.no_unknown_writers()
                self.stage_stopped(partial, proof)
            finally:
                if runtime_guard:
                    runtime_guard.__exit__(None, None, None)
                if evidence:
                    evidence.close()
            self.restore_apps()

    def stage_stopped(self, partial, proof, deadline=None):
        """Stage with callers holding writer locks and supplying verified exit proof.

        deadline is an absolute monotonic deadline, allowing a reviewed operator
        continuation to retain its original maintenance-window limit.
        """
        self.phase('writers_stopped', exit_evidence=proof)
        if deadline is None:
            deadline = time.monotonic() + self.cfg.get('staging_timeout_seconds', 1800)
        remaining(deadline)
        records = self.data_inventory(deadline=deadline)
        recovery = inventory(partial / 'recovery', max_entries=self.cfg['max_entries'], deadline=deadline)
        total, required = estimate(records + recovery)
        require(total <= self.cfg['max_snapshot_bytes'], 'snapshot_budget_exceeded')
        free_space(SPOOL, required, len(records) + 100, self.cfg)
        captured_at = utc()
        all_files = []
        for prefix, entries in [('data', records), ('recovery', recovery)]:
            for entry in entries:
                remaining(deadline)
                entry['path'] = prefix if entry['path'] == '.' else prefix + '/' + entry['path']
                if 'hardlink' in entry:
                    entry['hardlink'] = prefix + '/' + entry['hardlink']
                all_files.append(entry)
        manifest = {'format': FORMAT, 'snapshot_id': self.journal['snapshot_id'], 'captured_at': captured_at,
                    'server_name': self.cfg['server_name'], 'infra_revision': self.cfg['infra_revision'],
                    'included': ['data/**', 'recovery/**'], 'excluded': self.cfg.get('excluded_paths', []), 'files': all_files,
                    'original': self.journal['original'], 'exit_evidence': proof}
        atomic_json(partial / 'manifest.json', manifest, deadline)
        require((partial / 'manifest.json').stat().st_size <= MANIFEST_MAX_BYTES, 'manifest_size_limit_exceeded')
        self.phase('staging')
        staged_tar = create_staging_tar(partial, manifest, deadline)
        # Tar member hashes already prove every copied byte matches the initial
        # inventory. Recheck source metadata without a third content read.
        # The on-disk manifest and TAR are complete; reuse the baseline records
        # rather than retaining a third full 585k-entry inventory in memory.
        for row in records:
            remaining(deadline)
            row.pop('sha256', None)
            row['path'] = '.' if row['path'] == 'data' else row['path'].removeprefix('data/')
            if 'hardlink' in row:
                row['hardlink'] = row['hardlink'].removeprefix('data/')
        require(self.data_inventory(hashes=False, deadline=deadline) == records, 'source_changed_during_staging')
        command(['sync', '-f', str(partial)], timeout=min(300, remaining(deadline)))
        self.phase('staging_verified', captured_at=captured_at, staging_format='tar-v1', staged_tar=staged_tar,
                   manifest_sha256=file_hash(partial / 'manifest.json', deadline))

    def archive(self, partial):
        # A payload completed just before a crash may already occupy the whole
        # archive budget. Recreating it beside staging + the old ciphertext
        # would require a third full copy. Preserve it for operator inspection.
        require(not any((partial / name).exists() or (partial / name).is_symlink()
                        for name in ('payload.enc', 'manifest.enc')), 'existing_ciphertext_requires_operator_inspection')
        manifest_path = partial / 'manifest.json'
        require(file_hash(manifest_path) == self.journal['manifest_sha256'], 'staged_manifest_changed')
        manifest = read_json(manifest_path)
        self.phase('archiving')
        # Existing partial ciphertext is retained after failures, never silently
        # discarded. Recovery requires an operator to move it aside before retry.
        if self.journal.get('staging_format') == 'tar-v1':
            require('staged_tar' in self.journal, 'staging_tar_record_missing')
            payload = encrypted_archive(partial, partial / 'payload.enc', manifest, self.cfg['age_recipient'],
                                        staged_tar=self.journal['staged_tar'])
        else:
            require('staging_format' not in self.journal, 'unknown_staging_format')
            payload = encrypted_archive(partial, partial / 'payload.enc', manifest, self.cfg['age_recipient'])
        encrypted = encrypt_manifest(manifest_path, partial / 'manifest.enc', self.cfg['age_recipient'])
        ready = {'format': FORMAT, 'snapshot_id': self.journal['snapshot_id'], 'captured_at': manifest['captured_at'],
                 'payload': {'path': 'payload.enc', 'compression': 'zstd', **payload}, 'manifest': {'path': 'manifest.enc', **encrypted}}
        atomic_json(partial / 'ready.json', ready)
        self.phase('ciphertext_verified')
        self.finalize(partial)

    def finalize(self, partial):
        ready = read_json(partial / 'ready.json')
        for name in ('payload', 'manifest'):
            path = partial / (name + '.enc')
            require(file_hash(path) == ready[name]['sha256'] and path.stat().st_size == ready[name]['size'],
                    'ciphertext_readback_failed')
        self.phase('ready_finalizing')
        # Only owned staging is removed, after ciphertext and complete tar checks.
        for name in ('data', 'recovery'):
            if (partial / name).exists():
                require(not (partial / name).is_symlink(), 'staging_directory_replaced')
                shutil.rmtree(partial / name)
        plain_tar = partial / 'staging.tar'
        require(not plain_tar.is_symlink(), 'staging_tar_symlink')
        plain_tar.unlink(missing_ok=True)
        (partial / 'manifest.json').unlink(missing_ok=True)
        for name in ('payload.enc', 'manifest.enc', 'ready.json'):
            os.chown(partial / name, 0, self.cfg['uploader_gid'])
            os.chmod(partial / name, 0o640)
        os.chown(partial, 0, self.cfg['uploader_gid'])
        os.chmod(partial, 0o750)
        final = SPOOL / (self.journal['snapshot_id'] + '.ready')
        require(not final.exists(), 'ready_destination_exists')
        os.rename(partial, final)
        fsync_directory(SPOOL)
        self.phase('ready', ciphertext_bytes=ready['payload']['size'] + ready['manifest']['size'], ready_directory=str(final))

    def run(self):
        if self.journal_path.exists():
            previous = read_json(self.journal_path)
            require(previous.get('phase') in TERMINAL, 'unfinished_backup_requires_recover_or_inspection')
        measured = self.preflight()
        identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex
        partial = SPOOL / (identifier + '.partial')
        partial.mkdir(mode=0o700)
        self.journal = {'format': FORMAT, 'snapshot_id': identifier, 'started_at': utc(),
                        'original': self.original_state(), 'measured': measured}
        self.phase('preflight_complete')
        try:
            self.capture(partial)
        except BaseException:
            phase = self.journal.get('phase')
            if phase in PRESTOP:
                self.restore_preparation()
            elif phase in {'writers_stopped', 'staging'}:
                self.restore_failed_capture()
            # Unknown shutdown outcomes deliberately leave replicas untouched.
            raise
        self.archive(partial)

    def recover(self):
        self.journal = read_json(self.journal_path)
        identifier = self.journal.get('snapshot_id', '')
        require(IDENTIFIER.fullmatch(identifier), 'journal_snapshot_id_invalid')
        phase = self.journal.get('phase')
        if phase in TERMINAL:
            return
        # Recovery never interprets pod disappearance/reboot as a clean shutdown.
        require(phase in RECOVERABLE | PRESTOP | {'writers_stopped', 'staging', 'capture_failure_restoring'},
                'uncertain_backup_requires_operator_inspection')
        check_mount(DATA, self.cfg['data_uuid'])
        check_mount(SPOOL, self.cfg['spool_uuid'])
        if phase in PRESTOP:
            self.restore_preparation()
            return
        if phase in {'writers_stopped', 'staging', 'capture_failure_restoring'} or self.journal.get('failed_capture'):
            self.restore_failed_capture()
            return
        partial = SPOOL / (identifier + '.partial')
        final = SPOOL / (identifier + '.ready')
        if phase == 'ready_finalizing' and final.is_dir() and not partial.exists():
            ready = read_json(final / 'ready.json')
            for name in ('payload', 'manifest'):
                require(file_hash(final / (name + '.enc')) == ready[name]['sha256'], 'ciphertext_readback_failed')
            self.phase('ready', ready_directory=str(final))
            return
        require(partial.is_dir() and not partial.is_symlink(), 'partial_directory_missing')
        if phase in {'ciphertext_verified', 'ready_finalizing'}:
            self.finalize(partial)
            return
        if phase in {'staging_verified', 'apps_restoring'}:
            self.restore_apps()
        self.archive(partial)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/pz-backup/config.json'))
    parser.add_argument('operation', choices=['inventory', 'run', 'recover', 'status'])
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    cfg = configuration(args.config)
    trusted(STATE, directory=True)
    trusted(LOCK.parent, directory=True)
    coordinator = Coordinator(cfg)
    if args.operation == 'status':
        journal = read_json(coordinator.journal_path) if coordinator.journal_path.exists() else {}
        print(json.dumps({key: journal.get(key) for key in ('snapshot_id', 'phase', 'started_at', 'updated_at')}))
        return
    with locked(LOCK, create=True):
        disk_migration_ready()
        if args.operation == 'inventory':
            print(json.dumps(coordinator.preflight(), sort_keys=True))
        elif args.operation == 'run':
            coordinator.run()
        else:
            coordinator.recover()


if __name__ == '__main__':
    try:
        main()
    except (Refused, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # No raw exception, subprocess stderr, paths or application data in logs.
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        print(json.dumps({'status': 'failed', 'reason': reason}), file=sys.stderr)
        sys.exit(1)
