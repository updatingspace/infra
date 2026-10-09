#!/usr/bin/env python3
"""Publish verified ciphertext using an account with no deletion or host privileges."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import remote


def upload_queue(config, client, spool, state):
    if spool.is_symlink() or state.is_symlink():
        raise remote.BackupError('Unsafe queue directory')
    results = []
    for path in sorted(spool.glob('*.ready')):
        sid = path.name.removesuffix('.ready')
        if not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{32}', sid) or path.is_symlink():
            raise remote.BackupError('Unsafe queue entry')
        ready = json.loads((path / 'ready.json').read_text())
        if ready.get('format') != config.snapshot_format or ready.get('snapshot_id') != sid:
            raise remote.BackupError('Invalid ready manifest')
        # Always read back remote bytes, including after an ambiguous previous
        # publication. A local receipt is not authority to skip verification.
        for kind in ('payload', 'manifest'):
            f = path / (kind + '.enc')
            if ready[kind].get('path') != f.name or f.is_symlink() or not f.is_file():
                raise remote.BackupError('Unsafe ready file')
            import hashlib
            h = hashlib.sha256()
            with f.open('rb') as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''): h.update(chunk)
            if f.stat().st_size != ready[kind]['size'] or h.hexdigest() != ready[kind]['sha256']:
                raise remote.BackupError('Local ciphertext checksum mismatch')
        started = time.monotonic()
        commit = remote.upload_snapshot(client, config, snapshot_id=sid, captured_at=ready['captured_at'],
                                        payload_path=path / 'payload.enc', manifest_path=path / 'manifest.enc')
        receipt = {'format': 'pz-upload-receipt-v1', 'snapshot_id': sid, 'scope': config.scope, 'commit': commit,
                   'commit_sha256': remote.commit_sha256(commit), 'verified_at': remote.utc_now(),
                   'upload_seconds': time.monotonic() - started}
        remote._atomic_json(state / (sid + '.json'), receipt)
        remote._atomic_json(state / 'last-success.json', receipt)
        results.append({'snapshot_id': sid, 'committed': True})
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='/etc/pz-backup/remote-upload.json')
    p.add_argument('--credentials', default='/etc/pz-backup/upload.credentials.json')
    args = p.parse_args()
    status = Path('/var/lib/pz-backup-upload/remote-status.json')
    try:
        cfg = remote.RemoteConfig(**remote._private_json(Path(args.config)))
        client = remote.create_client(cfg, args.credentials)
        result = upload_queue(cfg, client, Path('/srv/pz-backup-spool'), status.parent)
        count = remote.multipart_inventory(client, cfg)
        remote.write_remote_status(status, incomplete_multipart=count)
        print(json.dumps(result))
    except Exception as error:
        remote.write_remote_status(status, error=error)
        raise


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'reason': type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
