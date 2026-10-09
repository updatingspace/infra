#!/usr/bin/env python3
"""Read the current YC ID topology into a private cutover/rollback directory."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess


def yc(*args, raw=False):
    command = ['yc', *args] + ([] if raw else ['--format=json'])
    result = subprocess.run(command, capture_output=True, timeout=90,
        env={**os.environ, 'YC_CLI_INITIALIZATION_SILENCE': 'true'})
    if result.returncode:
        raise RuntimeError('YC read failed: ' + ' '.join(args[:3]))
    return result.stdout if raw else json.loads(result.stdout)


def is_id(name):
    return any(name == prefix or name.startswith(prefix + '-')
               for prefix in ('updspace-id', 'updatingspace-id'))


def container(item):
    return {**item,
        'revisions': yc('serverless', 'container', 'revision', 'list', '--container-id', item['id']),
        'bindings': yc('serverless', 'container', 'list-access-bindings', '--id', item['id'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New private directory; contains runtime configuration')
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700)
    containers = [x for x in yc('serverless', 'container', 'list') if is_id(x['name'])]
    if not containers or len({x['folder_id'] for x in containers}) != 1:
        raise RuntimeError('Expected ID containers in one folder')
    with ThreadPoolExecutor(max_workers=5) as pool:
        inventory = list(pool.map(container, containers))
    ids = {x['id'] for x in containers}
    triggers = [x for x in yc('serverless', 'trigger', 'list')
                if is_id(x['name']) or any(cid in json.dumps(x['rule']) for cid in ids)]
    result = {'stamp': datetime.now(timezone.utc).isoformat(), 'containers': inventory, 'triggers': triggers,
        'folder_bindings': yc('resource-manager', 'folder', 'list-access-bindings', '--id', containers[0]['folder_id']),
        'gateway': yc('serverless', 'api-gateway', 'get', '--id', 'd5d6almt4c5i2ao9e4ha')}
    files = {
        'inventory.json': result,
        'database-bindings.json': yc('ydb', 'database', 'list-access-bindings', '--id', 'etnq1cp5vubgd8ono4t4'),
        'functions.json': [x for x in yc('serverless', 'function', 'list') if is_id(x['name'])],
    }
    for name, value in files.items():
        (args.output/name).write_text(json.dumps(value, indent=2) + '\n')
    (args.output/'gateway.yaml').write_bytes(yc('serverless', 'api-gateway', 'get-spec',
                                              '--id', 'd5d6almt4c5i2ao9e4ha', raw=True))
    print(json.dumps({'snapshot': str(args.output), 'containers': len(containers),
                      'triggers': len(triggers), 'cloud_mutations': 0}))


if __name__ == '__main__':
    main()
