#!/usr/bin/env python3
"""Read-only full remote verification under the unprivileged uploader account."""
import argparse
import json
from pathlib import Path
import sys
import remote


def verify_snapshot(client, config, snapshot_id):
    remote._check_id(snapshot_id)
    with remote._lock(config):
        commit, _marker = remote.read_commit(client, config, snapshot_id)
        for kind in ('payload', 'manifest'):
            remote._verify(client, config, commit[kind])
        # Read the marker again after the payloads; changed/missing commit is
        # ambiguous and must never authorize local cleanup.
        repeated, _ = remote.read_commit(client, config, snapshot_id)
        if remote.commit_sha256(repeated) != remote.commit_sha256(commit):
            raise remote.BackupError('Commit changed during remote verification')
        return {'format': 'pz-backup-remote-verified-v1', 'snapshot_id': snapshot_id,
                'scope': config.scope, 'commit': commit, 'commit_sha256': remote.commit_sha256(commit)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot_id')
    args = parser.parse_args()
    config = remote.RemoteConfig(**remote._private_json(Path('/etc/pz-backup/remote-upload.json')))
    client = remote.create_client(config, '/etc/pz-backup/upload.credentials.json')
    print(json.dumps(verify_snapshot(client, config, args.snapshot_id), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'failed', 'reason': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
