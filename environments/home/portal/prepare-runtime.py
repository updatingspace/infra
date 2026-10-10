#!/usr/bin/env python3
"""Prepare Portal runtime Secrets from existing protected migration inputs on the VM."""
import argparse
import json
from pathlib import Path
import subprocess
from urllib.parse import quote

ROLES = {name: 'portal_' + ('core' if name == 'portal' else name) for name in (
    'bff', 'access', 'portal', 'activity', 'events', 'voting', 'gamification', 'featureflags')}
NAMESPACE = 'updspace-portal'


def environment(name: str, source: dict, passwords: dict, storage: dict, *, id_stage: bool = False) -> dict:
    role = ROLES[name]
    result = {key: value for key, value in source.items()
              if not key.startswith(('YC_', 'YDB_', 'YMQ_')) and not key.endswith('_INVOKE_URL')}
    result.update({
        'DB_DRIVER': 'postgres',
        'DATABASE_URL': 'postgresql://' + role + ':' + quote(passwords[role], safe='')
            + '@postgres.updspace-data.svc.cluster.local:5432/updspace?sslmode=disable',
        'ALLOWED_HOSTS': '.updspace.com,.svc.cluster.local,localhost,127.0.0.1,' + ','.join(ROLES),
        'DJANGO_SECURE_SSL_REDIRECT': '0',
        'ACCESS_BASE_URL': 'http://access:8000/api/v1',
        'ACCESS_SERVICE_URL': 'http://access:8000',
        'ACCESS_PRIVATE_INVOKE_AUTH': '0',
        'S3_ENDPOINT_URL': storage['endpoint'], 'S3_REGION': storage['region'],
        'S3_ACCESS_KEY_ID': storage['access_key_id'], 'S3_SECRET_ACCESS_KEY': storage['secret_access_key'],
        'S3_FORCE_PATH_STYLE': '1',
    })
    for key, target in [('PORTAL_SERVICE_URL', 'portal'), ('ACTIVITY_SERVICE_URL', 'activity')]:
        if key in result:
            result[key] = f'http://{target}:8000/api/v1'
    if name == 'bff':
        for service in ('portal', 'voting', 'events', 'gamification', 'featureflags'):
            result['BFF_UPSTREAM_' + service.upper() + '_URL'] = f'http://{service}:8000/api/v1'
        result['BFF_UPSTREAM_FEED_URL'] = 'http://activity:8000/api/v1'
        result['ID_BASE_URL'] = ('http://id.updspace-id.svc.cluster.local:8089/api/v1' if id_stage else 'https://id.updspace.com/api/v1')
        result['ID_PUBLIC_BASE_URL'] = 'https://id.updspace.com'
    return result


def objects(source: dict, passwords: dict, storage: dict, images: dict, *, id_stage: bool = False) -> list[dict]:
    if set(source['services']) != set(ROLES) or set(images) != set(ROLES):
        raise ValueError('Expected the eight Portal services')
    output = []
    for name, service in source['services'].items():
        if service['image'] != images[name]:
            raise ValueError('Source image differs from declared image: ' + name)
        env = environment(name, service['environment'], passwords, storage, id_stage=id_stage)
        if not env.get('DJANGO_SECRET_KEY') or not env.get('BFF_INTERNAL_HMAC_SECRET'):
            raise ValueError('Missing original application secrets: ' + name)
        output.append({'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                       'metadata': {'name': name + '-runtime', 'namespace': NAMESPACE}, 'stringData': env})
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--id-stage', action='store_true', help='Temporary local ID backend for coordinated SSO acceptance')
    args = parser.parse_args()
    root = Path('/opt/updspace-portal-migration')
    source = json.loads((root / 'source-runtime.json').read_text())
    passwords = json.loads(Path('/opt/updspace-data/credentials.json').read_text())
    storage = json.loads(Path('/opt/updspace-data/garage/portal-credentials.json').read_text())
    images = json.loads(Path(__file__).with_name('portal-images.json').read_text())
    result = objects(source, passwords, storage, images, id_stage=args.id_stage)
    if args.apply:
        process = subprocess.run(['k3s', 'kubectl', 'apply', '-f', '-'],
            input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': result}),
            text=True, capture_output=True, timeout=60)
        if process.returncode:
            raise RuntimeError('Secret apply failed; secret-bearing output suppressed')
    print(('Applied' if args.apply else 'Prepared') + ' 8 runtime Secrets; values suppressed')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Runtime preparation failed (' + type(error).__name__ + '); values suppressed') from None
