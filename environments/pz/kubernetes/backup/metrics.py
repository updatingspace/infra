#!/usr/bin/env python3
"""Read-only aggregate backup metrics; fixed labels, bounded files, no S3 credentials."""
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time

STATE = Path('/var/lib/pz-backup')
UPLOAD = Path('/var/lib/pz-backup-upload')
RETAIN = Path('/var/lib/pz-backup-retain')
FILESYSTEMS = {'data': Path('/srv/pz-storage/zomboid'), 'spool': Path('/srv/pz-backup-spool'), 'root': Path('/')}
JOBS = {'capture': 'pz-backup.service', 'upload': 'pz-backup-upload.service',
        'cleanup': 'pz-backup-cleanup.service', 'retain': 'pz-backup-retain.service',
        'migration': 'pz-disk-migration.service'}
MAX_JSON = 1024 * 1024


def read(path):
    """Return a document and distinct valid/absent/invalid state; never hide damage."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}, 'absent'
    except OSError:
        return {}, 'invalid'
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON:
            return {}, 'invalid'
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            document = json.loads(source.read(MAX_JSON + 1))
        return (document, 'valid') if isinstance(document, dict) else ({}, 'invalid')
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}, 'invalid'
    finally:
        os.close(descriptor)


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return 0
        result = parsed.timestamp()
        return result if math.isfinite(result) and result > 0 else 0
    except (AttributeError, ValueError, OverflowError):
        return 0


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def mapping(value):
    return value if isinstance(value, dict) else {}


def host_mount_devices():
    """Read the host mount namespace, outside this unit's ReadOnlyPaths binds."""
    try:
        with open('/proc/1/mountinfo', encoding='utf-8') as source:
            text = source.read(2 * MAX_JSON + 1)
        if len(text) > 2 * MAX_JSON:
            return {}
        devices = {}
        for line in text.splitlines():
            fields = line.split(' - ', 1)[0].split()
            if len(fields) < 6:
                return {}
            path = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
            device = fields[2].split(':')
            if len(device) != 2:
                return {}
            devices[path] = tuple(int(value) for value in device)
        return devices
    except (OSError, ValueError, UnicodeError):
        return {}


def host_mounted(path, devices):
    try:
        device = path.stat().st_dev
        return devices.get(str(path)) == (os.major(device), os.minor(device))
    except OSError:
        return False


def service_states():
    try:
        result = subprocess.run(['systemctl', 'show', '--no-pager',
                                 '--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStatus',
                                 *JOBS.values()], capture_output=True, text=True, timeout=3)
        # systemctl may return nonzero because an optional migration unit is
        # absent while still returning complete state for the installed jobs.
        if not result.stdout:
            return {}
        states = {}
        for block in result.stdout.strip().split('\n\n'):
            fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            if fields.get('Id') in JOBS.values():
                states[fields['Id']] = fields
        return states
    except (OSError, subprocess.SubprocessError):
        return {}


def render(now=None, services=None):
    now = time.time() if now is None else now
    metrics = {'pz_backup_metrics_timestamp_seconds': now}
    def document(label, path):
        data, status = read(path)
        metrics[f'pz_backup_state_file_invalid{{source="{label}"}}'] = int(status == 'invalid')
        metrics[f'pz_backup_state_file_present{{source="{label}"}}'] = int(status != 'absent')
        return data
    journal = document('capture', STATE / 'journal.json')
    receipt = document('receipt', UPLOAD / 'last-success.json')
    deletion = document('retention', RETAIN / 'deletion-journal.json')
    migration = document('migration', STATE / 'disk-migration/journal.json')
    attestation = document('restore', RETAIN / 'restore-attestation.json')
    restore_report = document('restore_result', STATE / 'restore-status.json')
    commit = mapping(receipt.get('commit'))
    captured = timestamp(commit.get('captured_at'))
    receipt_valid = receipt.get('format') == 'pz-upload-receipt-v1' and captured > 0 and captured <= now
    metrics.update({
        'pz_backup_committed_present': int(receipt_valid),
        'pz_backup_last_committed_timestamp_seconds': captured if receipt_valid else 0,
        'pz_backup_age_seconds': max(0, now - captured) if receipt_valid else now,
        'pz_backup_incomplete': int(bool(journal) and journal.get('phase') != 'ready'),
        'pz_backup_retention_incomplete': int(bool(deletion) and deletion.get('phase') != 'complete'),
        'pz_backup_migration_incomplete': int(bool(migration) and migration.get('phase') != 'complete'),
        'pz_backup_last_stage_timestamp_seconds': timestamp(journal.get('updated_at')),
        'pz_backup_capture_failed': int(journal.get('phase') in {'preparation_failed', 'capture_failed'}),
    })
    if receipt_valid:
        for metric, value in [('pz_backup_upload_seconds', receipt.get('upload_seconds')),
                              ('pz_backup_payload_bytes', mapping(commit.get('payload')).get('size'))]:
            if number(value):
                metrics[metric] = value
    for name, start, end in [('downtime', 'downtime_started_at', 'downtime_finished_at'),
                             ('stop', 'downtime_started_at', 'writers_stopped_at'),
                             ('copy', 'staging_started_at', 'staging_verified_at'),
                             ('archive', 'archive_started_at', 'ready_at')]:
        beginning, finish = timestamp(journal.get(start)), timestamp(journal.get(end))
        if beginning and finish and beginning <= finish <= now:
            metrics['pz_backup_' + name + '_seconds'] = finish - beginning
    stage_time = timestamp(journal.get('updated_at'))
    if stage_time and stage_time <= now:
        metrics['pz_backup_stage_age_seconds'] = now - stage_time
    restore_time = timestamp(attestation.get('restored_at'))
    checks = mapping(attestation.get('checks'))
    restore_valid = (attestation.get('format') == 'pz-backup-restore-attestation-v1'
                     and all(checks.get(k) is True for k in ('archive_verified', 'isolated_runtime', 'rcon_health', 'world_loaded'))
                     and 0 < restore_time <= now)
    metrics['pz_backup_restore_attested'] = int(restore_valid)
    if restore_valid:
        metrics['pz_backup_restore_timestamp_seconds'] = restore_time
        metrics['pz_backup_restore_age_seconds'] = now - restore_time
    checked = timestamp(restore_report.get('observed_at'))
    if (restore_report.get('format') == 'pz-backup-restore-status-v1' and 0 < checked <= now
            and type(restore_report.get('passed')) is bool):
        metrics['pz_backup_restore_failed'] = int(not restore_report['passed'])
        metrics['pz_backup_restore_result_age_seconds'] = now - checked
    # Producers write only aggregate facts after a completed S3 observation.
    # Missing/stale reports never become a fictitious zero remote-object count.
    for role, directory in [('upload', UPLOAD), ('retain', RETAIN)]:
        report = document(role + '_remote', directory / 'remote-status.json')
        observed = timestamp(report.get('observed_at'))
        valid = report.get('format') == 'pz-backup-remote-status-v1' and 0 < observed <= now
        metrics[f'pz_backup_remote_observation_valid{{role="{role}"}}'] = int(valid)
        if valid:
            metrics[f'pz_backup_remote_observation_age_seconds{{role="{role}"}}'] = now - observed
            for field in ('verified_snapshots', 'incomplete_multipart', 'sha_failures', 'restore_failures'):
                if type(report.get(field)) is int and report[field] >= 0:
                    metrics[f'pz_backup_{field}{{role="{role}"}}'] = report[field]
            if type(report.get('verification_failed')) is bool:
                metrics[f'pz_backup_verification_failed{{role="{role}"}}'] = int(report['verification_failed'])
                for kind in ('checksum', 'multipart_cleanup', 'remote_io', 'protocol', 'local_input'):
                    metrics[f'pz_backup_failure_kind{{role="{role}",kind="{kind}"}}'] = int(
                        report['verification_failed'] and report.get('verification_failure_class') == kind)
    services = service_states() if services is None else services
    for label, unit in JOBS.items():
        status = services.get(unit, {})
        known = status.get('LoadState') == 'loaded'
        metrics[f'pz_backup_job_state_known{{job="{label}"}}'] = int(known)
        if known:
            metrics[f'pz_backup_job_failed{{job="{label}"}}'] = int(status.get('ActiveState') == 'failed'
                  or status.get('Result') not in (None, '', 'success'))
            metrics[f'pz_backup_job_active{{job="{label}"}}'] = int(status.get('ActiveState') == 'activating'
                  or status.get('SubState') in {'running', 'start', 'start-post', 'stop', 'stop-sigterm'})
    mount_devices = host_mount_devices()
    for label, path in FILESYSTEMS.items():
        mounted = host_mounted(path, mount_devices)
        metrics[f'pz_backup_mounted{{filesystem="{label}"}}'] = int(mounted)
        try:
            if not mounted:
                continue
            space = os.statvfs(path)
            metrics[f'pz_backup_filesystem_readable{{filesystem="{label}"}}'] = 1
            for field, value in [('free_bytes', space.f_bavail * space.f_frsize),
                                  ('total_bytes', space.f_blocks * space.f_frsize),
                                  ('free_inodes', space.f_favail), ('total_inodes', space.f_files)]:
                metrics[f'pz_backup_{field}{{filesystem="{label}"}}'] = value
        except OSError:
            metrics[f'pz_backup_filesystem_readable{{filesystem="{label}"}}'] = 0
    return '\n'.join(f'{key} {value}' for key, value in sorted(metrics.items())) + '\n'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != '/metrics':
            self.send_error(404)
            return
        try:
            payload = render().encode()
        except Exception:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', required=True)
    parser.add_argument('--port', type=int, default=9109)
    args = parser.parse_args()
    HTTPServer((args.bind, args.port), Handler).serve_forever()
