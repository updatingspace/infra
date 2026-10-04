#!/usr/bin/env python3
"""Combine two independently verified restores into a NEW offline drill tree.

The daily snapshot owns all current state. Only the immutable components named
in its authenticated manifest are taken from the pinned full base snapshot.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import remote
import restore


def manifest_at(root):
    path = root / 'manifest.json'
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        restore.require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid())
                        and info.st_mode & 0o077 == 0, 'untrusted_restored_manifest')
        raw = source.read(restore.MAX_MANIFEST_BYTES + 1)
    restore.require(len(raw) <= restore.MAX_MANIFEST_BYTES, 'restored_manifest_exceeds_limit')
    value = remote._read_json(raw)
    restore.validate_manifest(value)
    return value


def verified_root(path):
    root = Path(path).absolute()
    restore.require(root.is_dir() and not root.is_symlink()
                    and root.stat().st_mode & 0o077 == 0, 'untrusted_restore_root')
    manifest = manifest_at(root)
    report = remote._private_json(root / 'restored.json')
    restore.require(report.get('archive_verified') is True
                    and report.get('snapshot_id') == manifest['snapshot_id']
                    and isinstance(report.get('commit_sha256'), str)
                    and remote.SHA_PATTERN.fullmatch(report['commit_sha256']),
                    'unverified_restore_report')
    return root, manifest, report


def _copy(source, destination):
    process = subprocess.run(['cp', '--archive', '--reflink=auto', '--', str(source), str(destination)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    restore.require(process.returncode == 0, 'restore_composition_copy_failed')


def compose(base_path, state_path, destination):
    base_root, base, base_report = verified_root(base_path)
    state_root, state, state_report = verified_root(state_path)
    restore.require(base.get('snapshot_profile') is None
                    and state.get('snapshot_profile') == 'state-with-base-v1',
                    'invalid_restore_profile_pair')
    reference = state['base_snapshot']
    restore.require(reference['snapshot_id'] == base['snapshot_id']
                    and reference['commit_sha256'] == base_report['commit_sha256'],
                    'base_restore_identity_mismatch')
    base_rows = {row['path']: row for row in base['files']}
    state_rows = {row['path']: row for row in state['files']}
    supplement = {}
    for component, expected_digest in reference['components'].items():
        prefix = 'data/' + component
        rows = sorted((row for name, row in base_rows.items()
                       if name == prefix or name.startswith(prefix + '/')),
                      key=lambda row: row['path'])
        restore.require(rows and rows[0]['path'] == prefix and rows[0]['type'] == 'dir'
                        and not any('hardlink' in row for row in rows)
                        and not any(row['path'] in state_rows for row in rows),
                        'base_component_inventory_conflict')
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        restore.require(digest == expected_digest, 'base_component_digest_mismatch')
        supplement.update((row['path'], row) for row in rows)
    target = restore._new_destination_path(destination)
    old_umask = os.umask(0o077)
    try:
        target.mkdir(mode=0o700)
        for name in ('data', 'recovery'):
            _copy(state_root / name, target)
        for component in reference['components']:
            source = base_root / 'data' / component
            receiver = target / 'data' / component
            restore.require(not receiver.exists() and not receiver.is_symlink(),
                            'base_component_target_exists')
            _copy(source, receiver.parent)
        combined = dict(state)
        combined['snapshot_profile'] = 'composed-state-with-base-v1'
        combined['files'] = [*state['files'], *supplement.values()]
        combined['files'].sort(key=lambda row: row['path'])
        combined['included'] = ['data/**', 'recovery/**']
        combined['excluded'] = [name for name in state.get('excluded', [])
                                if name not in reference['components']]
        expected = restore.validate_manifest(combined)
        # Adding the base directories changes ancestor mtime. Restore the
        # authenticated state metadata before full byte-by-byte verification.
        for name, row in sorted(expected.items(), key=lambda pair: pair[0].count('/'), reverse=True):
            if row['type'] == 'dir':
                restore._metadata(target / name, row)
        restore._verify_extracted(target, expected)
        remote._atomic_json(target / 'manifest.json', combined)
        remote._atomic_json(target / 'restored.json', {
            'snapshot_id': state['snapshot_id'], 'commit_sha256': state_report['commit_sha256'],
            'archive_verified': True, 'composition_verified': True,
            'base_snapshot_id': base['snapshot_id'],
            'base_commit_sha256': base_report['commit_sha256'],
            'verified_entries': len(expected), 'runtime_drill_required': True,
            'verified_at': remote.utc_now()})
        return {'snapshot_id': state['snapshot_id'], 'base_snapshot_id': base['snapshot_id'],
                'verified_entries': len(expected), 'composition_verified': True}
    finally:
        os.umask(old_umask)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-restored', required=True)
    parser.add_argument('--state-restored', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(compose(args.base_restored, args.state_restored, args.destination), sort_keys=True))
    except (restore.RestoreError, remote.BackupError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
