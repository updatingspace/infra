#!/usr/bin/env python3
"""Delete owned ciphertext only after independent, streamed remote readback.

Never recurses, touches *.partial, trusts a receipt without remote verification,
or loads S3 credentials as root. The uploader account runs the read-only verifier.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import sys

import coordinator as c
import remote

SPOOL = Path('/srv/pz-backup-spool')
STATE = Path('/var/lib/pz-backup')
RECEIPTS = Path('/var/lib/pz-backup-upload')
REMOTE_CONFIG = Path('/etc/pz-backup/cleanup-remote.json')
FILES = ('payload.enc', 'manifest.enc', 'ready.json')
FORMAT = 'pz-backup-cleanup-v1'


def local_digest(path, owner_uid=0):
    info = path.lstat()
    c.require(stat.S_ISREG(info.st_mode) and info.st_uid == owner_uid and info.st_nlink == 1
              and info.st_mode & 0o022 == 0, 'cleanup_ciphertext_file_invalid')
    return {'sha256': c.file_hash(path), 'size': info.st_size}


def verifier(snapshot_id):
    c.require(c.IDENTIFIER.fullmatch(snapshot_id), 'cleanup_snapshot_id_invalid')
    c.trusted(Path('/opt/pz-backup/verify-remote.py'))
    raw = c.command(['runuser', '--user', 'pz-backup-upload', '--', '/opt/pz-backup-venv/bin/python3',
                     '/opt/pz-backup/verify-remote.py', snapshot_id], timeout=24 * 3600)
    c.require(len(raw) <= 128 * 1024, 'cleanup_remote_result_too_large')
    return json.loads(raw)


def read_receipt(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        uploader_uid = pwd.getpwnam('pz-backup-upload').pw_uid
        c.require(stat.S_ISREG(info.st_mode) and info.st_uid in {0, uploader_uid}
                  and info.st_mode & 0o077 == 0 and info.st_size <= 128 * 1024,
                  'cleanup_receipt_permissions_invalid')
        return json.load(source)


def verify_authorization(config, snapshot_id, ready, verify=verifier):
    c.require(ready.get('format') == config.snapshot_format and ready.get('snapshot_id') == snapshot_id,
              'cleanup_ready_identity_mismatch')
    receipt = read_receipt(RECEIPTS / (snapshot_id + '.json'))
    c.require(receipt.get('format') == 'pz-upload-receipt-v1' and receipt.get('snapshot_id') == snapshot_id,
              'cleanup_receipt_identity_mismatch')
    # Existing receipts also carry scope in their commit's exact object keys;
    # require explicit scope on new receipts for endpoint/bucket binding.
    c.require(receipt.get('scope') == config.scope, 'cleanup_receipt_scope_mismatch')
    independently = verify(snapshot_id)
    c.require(independently.get('format') == 'pz-backup-remote-verified-v1'
              and independently.get('snapshot_id') == snapshot_id and independently.get('scope') == config.scope,
              'cleanup_remote_scope_mismatch')
    commit = independently.get('commit', {})
    remote._validate_commit(config, snapshot_id, commit)
    digest = remote.commit_sha256(commit)
    c.require(digest == independently.get('commit_sha256') == receipt.get('commit_sha256')
              and remote.commit_sha256(receipt.get('commit', {})) == digest, 'cleanup_remote_commit_mismatch')
    c.require(commit.get('captured_at') == ready.get('captured_at'), 'cleanup_remote_timestamp_mismatch')
    for kind in ('payload', 'manifest'):
        c.require(ready.get(kind, {}).get('path') == kind + '.enc', 'cleanup_ready_path_invalid')
        c.require(all(commit[kind].get(field) == ready[kind].get(field) for field in ('sha256', 'size')),
                  'cleanup_remote_content_mismatch')
    return digest


def validate_journal(journal, config):
    c.require(journal.get('format') == FORMAT and journal.get('scope') == config.scope
              and journal.get('phase') in {'deleting', 'complete'}, 'cleanup_journal_invalid')
    c.require(c.IDENTIFIER.fullmatch(journal.get('snapshot_id', '')), 'cleanup_snapshot_id_invalid')
    c.require(set(journal.get('files', {})) == set(FILES), 'cleanup_journal_file_set_invalid')
    for item in journal['files'].values():
        c.require(item.get('state') in {'pending', 'deleting', 'deleted'}
                  and remote.SHA_PATTERN.fullmatch(item.get('sha256', ''))
                  and type(item.get('size')) is int and item['size'] > 0, 'cleanup_journal_file_invalid')


def cleanup_one(config, snapshot_id, journal_path, *, existing=None, verify=verifier, owner_uid=0):
    c.require(c.IDENTIFIER.fullmatch(snapshot_id), 'cleanup_snapshot_id_invalid')
    directory = SPOOL / (snapshot_id + '.ready')
    c.require(not directory.is_symlink(), 'cleanup_ready_directory_symlink')
    if directory.exists():
        info = directory.stat()
        c.require(stat.S_ISDIR(info.st_mode) and info.st_uid == owner_uid and info.st_mode & 0o022 == 0,
                  'cleanup_ready_directory_invalid')
        c.require(set(path.name for path in directory.iterdir()) <= set(FILES), 'cleanup_unexpected_ready_members')
    if existing is None:
        c.require(directory.is_dir() and set(path.name for path in directory.iterdir()) == set(FILES),
                  'cleanup_ready_members_missing')
        ready = c.read_json(directory / 'ready.json', 128 * 1024)
        authorized_digest = verify_authorization(config, snapshot_id, ready, verify)
        files = {name: {**local_digest(directory / name, owner_uid), 'state': 'pending'} for name in FILES}
        for kind in ('payload', 'manifest'):
            c.require(all(files[kind + '.enc'][field] == ready[kind][field] for field in ('sha256', 'size')),
                      'cleanup_local_content_mismatch')
        journal = {'format': FORMAT, 'scope': config.scope, 'snapshot_id': snapshot_id, 'phase': 'deleting',
                   'commit_sha256': authorized_digest, 'ready': ready, 'files': files, 'started_at': c.utc()}
        c.atomic_json(journal_path, journal)
    else:
        journal = existing
        validate_journal(journal, config)
        c.require(journal['snapshot_id'] == snapshot_id, 'cleanup_journal_snapshot_mismatch')
        authorized_digest = verify_authorization(config, snapshot_id, journal['ready'], verify)
        c.require(authorized_digest == journal['commit_sha256'], 'cleanup_remote_changed_during_resume')
    for name in FILES:
        item = journal['files'][name]
        path = directory / name
        if not path.exists() and not path.is_symlink():
            c.require(item['state'] in {'deleting', 'deleted'}, 'cleanup_pending_file_disappeared')
        else:
            actual = local_digest(path, owner_uid)
            c.require(all(actual[field] == item[field] for field in ('sha256', 'size')), 'cleanup_file_changed')
            c.require(item['state'] != 'deleted', 'cleanup_deleted_file_reappeared')
            item['state'] = 'deleting'
            c.atomic_json(journal_path, journal)
            path.unlink()
            c.fsync_directory(directory)
        item['state'] = 'deleted'
        c.atomic_json(journal_path, journal)
    if directory.exists():
        directory.rmdir()  # Fails closed on unknown content; never recursive.
        c.fsync_directory(SPOOL)
    journal.update(phase='complete', completed_at=c.utc())
    c.atomic_json(journal_path, journal)
    return {'snapshot_id': snapshot_id, 'cleaned': True}


def cleanup_queue(config, verify=verifier):
    journal_path = STATE / 'cleanup-journal.json'
    results = []
    if journal_path.exists():
        previous = c.read_json(journal_path, 256 * 1024)
        validate_journal(previous, config)
        if previous['phase'] != 'complete':
            results.append(cleanup_one(config, previous['snapshot_id'], journal_path, existing=previous, verify=verify))
    for directory in sorted(SPOOL.glob('*.ready')):
        snapshot_id = directory.name.removesuffix('.ready')
        # An uncommitted ready snapshot remains queued; it is never discarded to
        # make room. Malformed receipts fail rather than granting deletion.
        if not (RECEIPTS / (snapshot_id + '.json')).exists():
            continue
        results.append(cleanup_one(config, snapshot_id, journal_path, verify=verify))
    return results


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.umask(0o077)
    c.require(os.geteuid() == 0, 'root_required')
    c.trusted(STATE, directory=True)
    c.trusted(SPOOL, directory=True)
    cfg = remote._private_json(Path('/etc/pz-backup/config.json'))
    c.check_mount(SPOOL, cfg['spool_uuid'])
    config = remote.RemoteConfig(**remote._private_json(REMOTE_CONFIG))
    with c.locked(c.LOCK, create=True):
        c.disk_migration_ready()
        print(json.dumps(cleanup_queue(config), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        reason = str(error) if isinstance(error, c.Refused) else type(error).__name__
        print(json.dumps({'status': 'failed', 'reason': reason}), file=sys.stderr)
        sys.exit(1)
