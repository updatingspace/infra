#!/usr/bin/env python3
"""Copy the inventoried cloud frontend verbatim into an immutable VM release."""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import urllib.parse
import urllib.request
import uuid

SOURCE = 'https://d5da1fs5arjv790l1uua.kr8f6hld.apigw.yandexcloud.net/'


def relative_key(key):
    if not isinstance(key, str) or not key or key.startswith('/') or any(part in ('', '.', '..') for part in key.split('/')):
        raise ValueError('Unsafe frontend object key')
    return PurePosixPath(key)


def verify(data, entry):
    etag = entry['etag'].strip('"')
    if not re.fullmatch(r'[a-f0-9]{32}', etag) or len(data) != int(entry['size']) or hashlib.md5(data).hexdigest() != etag:
        raise ValueError('Frontend content differs from inventoried cloud object')
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory', type=Path)
    args = parser.parse_args()
    entries = json.loads(args.inventory.read_text())
    for entry in entries:
        relative_key(entry['key'])
    if len({entry['key'] for entry in entries}) != len(entries) or not any(entry['key'] == 'index.html' for entry in entries):
        raise ValueError('Frontend inventory is incomplete or duplicated')
    root = Path('/srv/updspace/portal-frontend')
    release = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    target = root / 'releases' / release
    target.mkdir(mode=0o755, parents=True)
    def copy(entry):
        request = urllib.request.Request(SOURCE + urllib.parse.quote(entry['key'], safe='/'), headers={'Accept-Encoding': 'identity'})
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read()
        fingerprint = verify(data, entry)
        path = target / relative_key(entry['key'])
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
        if hashlib.sha256(path.read_bytes()).hexdigest() != fingerprint:
            raise ValueError('Frontend disk verification failed')
        return {'key': entry['key'], 'size': len(data), 'sha256': fingerprint}
    proof = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item in pool.map(copy, entries):
            proof.append(item)
            if len(proof) % 200 == 0:
                print('Verified frontend objects:', len(proof), flush=True)
    private = Path('/opt/updspace-portal-migration/frontend-proofs')
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    (private / (release + '.json')).write_text(json.dumps(proof, indent=2))
    temporary = root / ('.current-' + release)
    temporary.symlink_to('releases/' + release)
    temporary.replace(root / 'current')
    print('Frontend release', release, ':', len(proof), 'verified files,', sum(x['size'] for x in proof), 'bytes')


if __name__ == '__main__':
    main()
