#!/usr/bin/python3
"""Consistent ID-only backup: pause writers, snapshot YDB and S3, then encrypt."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tarfile
import time


BASE = Path('/srv/backups/updspace-id')
CONFIG = Path('/opt/updspace-id')


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def kube(*args):
    return run(['k3s', 'kubectl', '--request-timeout=30s', *args], timeout=240).stdout


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_state(path, state):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(state, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def restore(state, state_path):
    """Attempt every recovery action; keep the marker until all succeed."""
    # Never replay an old marker onto a replacement workload.
    for resource in state.get('resources', []):
        live = json.loads(kube('-n', resource['namespace'], 'get', resource['name'], '-o', 'json'))
        if live['metadata']['uid'] != resource['uid']:
            raise RuntimeError('Recovery target was replaced: ' + resource['name'])
        if 'replicas' in resource and live['spec']['replicas'] not in (0, resource['replicas']):
            raise RuntimeError('Recovery replica count changed by operator: ' + resource['name'])
    errors = []
    def attempt(*args):
        try:
            kube(*args)
        except (subprocess.SubprocessError, OSError) as error:
            # These commands contain no credentials. Do not log secret GET output.
            message = f"Recovery failed: {' '.join(args)} ({type(error).__name__})"
            print(message, file=sys.stderr, flush=True)
            errors.append(message)

    # Set both desired replica counts before waiting for either dependency.
    attempt('-n', 'updspace-data', 'scale', 'statefulset/id-ydb',
            '--replicas=' + str(state['ydb_replicas']))
    attempt('-n', 'updspace-id', 'scale', 'deployment/id',
            '--replicas=' + str(state['replicas']))
    for namespace, resource, replicas in [
        ('updspace-data', 'statefulset/id-ydb', state['ydb_replicas']),
        ('updspace-id', 'deployment/id', state['replicas']),
    ]:
        if replicas:
            attempt('-n', namespace, 'rollout', 'status', resource, '--timeout=180s')
    for name, suspended in state['cron_suspend'].items():
        attempt('-n', 'updspace-id', 'patch', 'cronjob', name, '--type=merge',
                '-p', json.dumps({'spec': {'suspend': suspended}}))
    if errors:
        raise RuntimeError('Recovery incomplete; marker retained for automatic retry')
    # Retain the original state as incident evidence on the VM.
    state_path.replace(state_path.with_name('last-resume-state.json'))
    sync_directory(state_path.parent)
    print('ID backup recovery verified: YDB, ID and CronJobs restored', flush=True)


def preflight(deployment, ydb, crons):
    """Verify restart dependencies before taking any writer offline."""
    for namespace, resource in [('updspace-id', 'deployment/id'),
                                ('updspace-data', 'statefulset/id-ydb')]:
        kube('-n', namespace, 'rollout', 'status', resource, '--timeout=30s')
    templates = [deployment['spec']['template'], ydb['spec']['template']]
    templates.extend(c['spec']['jobTemplate']['spec']['template'] for c in crons)
    images = {c['image'] for t in templates
              for c in t['spec'].get('initContainers', []) + t['spec']['containers']}
    for image in sorted(images):
        info = json.loads(run(['k3s', 'crictl', 'inspecti', image], timeout=30).stdout)
        image_id = info['status']['id']
        run(['k3s', 'ctr', '-n', 'k8s.io', 'images', 'label', image_id,
             'io.cri-containerd.pinned=pinned'], timeout=30)
        pinned = json.loads(run(['k3s', 'crictl', 'inspecti', image], timeout=30).stdout)
        if not pinned['status'].get('pinned'):
            raise RuntimeError('Runtime image could not be pinned: ' + image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', action='store_true', help='Only finish interrupted recovery')
    args = parser.parse_args()
    os.umask(0o077)
    BASE.mkdir(parents=True, exist_ok=True)
    with (BASE/'backup.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.resume:
                return  # The backup or another recovery process owns the state.
            raise
        state_path = BASE/'resume-state.json'
        if args.resume:
            if state_path.exists():
                restore(json.loads(state_path.read_text()), state_path)
            return
        if state_path.exists():
            raise RuntimeError('Previous backup recovery pending; run backup.py --resume')
        import boto3
        from botocore.config import Config
        deployment = json.loads(kube('-n', 'updspace-id', 'get', 'deployment/id', '-o', 'json'))
        ydb = json.loads(kube('-n', 'updspace-data', 'get', 'statefulset/id-ydb', '-o', 'json'))
        replicas = deployment['spec']['replicas']
        ydb_replicas = ydb['spec']['replicas']
        if ydb_replicas != 1 or replicas < 1:
            raise RuntimeError('ID and YDB must be running before a full backup')
        crons = json.loads(kube('-n', 'updspace-id', 'get', 'cronjobs', '-o', 'json'))['items']
        preflight(deployment, ydb, crons)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        stage = BASE/('staging-' + stamp)
        stage.mkdir(mode=0o700)
        state = {'replicas': replicas, 'ydb_replicas': ydb_replicas,
                 'cron_suspend': {x['metadata']['name']: x['spec'].get('suspend', False) for x in crons},
                 'resources': [
                     {'namespace': namespace, 'name': name, 'uid': obj['metadata']['uid'],
                      **({'replicas': obj['spec']['replicas']} if 'replicas' in obj['spec'] else {})}
                     for namespace, name, obj in [
                         ('updspace-id', 'deployment/id', deployment),
                         ('updspace-data', 'statefulset/id-ydb', ydb),
                         *(('updspace-id', 'cronjob/' + x['metadata']['name'], x) for x in crons),
                     ]]}
        save_state(state_path, state)
        started = time.monotonic()
        try:
            for name in state['cron_suspend']:
                kube('-n', 'updspace-id', 'patch', 'cronjob', name, '--type=merge', '-p', '{"spec":{"suspend":true}}')
            deadline = time.monotonic() + 600
            while any(x.get('status', {}).get('active', 0) for x in json.loads(kube('-n', 'updspace-id', 'get', 'jobs', '-o', 'json'))['items']):
                if time.monotonic() > deadline:
                    raise TimeoutError('ID jobs did not drain; backup cancelled')
                time.sleep(5)
            kube('-n', 'updspace-id', 'scale', 'deployment/id', '--replicas=0')
            kube('-n', 'updspace-id', 'wait', '--for=delete', 'pod', '-l', 'app.kubernetes.io/name=id', '--timeout=120s')
            kube('-n', 'updspace-data', 'scale', 'statefulset/id-ydb', '--replicas=0')
            kube('-n', 'updspace-data', 'wait', '--for=delete', 'pod/id-ydb-0', '--timeout=180s')
            run(['tar', '--sparse', '--exclude=id-ydb/import-source', '--exclude=id-ydb/verify-restored',
                 '-C', '/srv/updspace', '-cf', str(stage/'ydb.tar'), 'id-ydb'])
            credentials = json.loads(Path('/opt/updspace-data/garage/id-credentials.json').read_text())
            service = json.loads(kube('-n', 'updspace-data', 'get', 'service/garage', '-o', 'json'))
            assert service['spec']['selector']['app.kubernetes.io/name'] == 'garage'
            # The public edge replaces Cache-Control; backup must retain object metadata.
            endpoint = 'http://' + service['spec']['clusterIP'] + ':3900'
            s3 = boto3.client('s3', endpoint_url=endpoint, region_name=credentials['region'],
                aws_access_key_id=credentials['access_key_id'], aws_secret_access_key=credentials['secret_access_key'],
                config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}, connect_timeout=5,
                              read_timeout=30, retries={'max_attempts': 1}, response_checksum_validation='when_required'))
            media = stage/'media'
            media.mkdir(mode=0o700)
            objects = []
            for bucket in credentials['buckets']:
                for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket):
                    for item in page.get('Contents', []):
                        if item['Key'].startswith('migration-check/'):
                            continue
                        response = s3.get_object(Bucket=bucket, Key=item['Key'], IfMatch=item['ETag'])
                        name = str(len(objects)) + '.bin'
                        with (media/name).open('wb') as output:
                            shutil.copyfileobj(response['Body'], output)
                        assert (media/name).stat().st_size == item['Size'], 'Truncated object'
                        objects.append({'bucket': bucket, 'key': item['Key'], 'file': name,
                            'sha256': digest(media/name), 'size': item['Size'], 'content_type': response.get('ContentType'),
                            'metadata': response.get('Metadata', {}), 'cache_control': response.get('CacheControl'),
                            'content_disposition': response.get('ContentDisposition')})
            (media/'manifest.json').write_text(json.dumps(objects, indent=2))
            (stage/'garage-id-credentials.json').write_text(json.dumps(credentials))
            secrets = []
            for namespace, names in [('updspace-id', ['id-api', 'id-sessions', 'id-mutations', 'id-web', 'id-jobs']),
                                     ('updspace-data', ['id-ydb-admin', 'id-ydb-runtime', 'id-ydb-tls']),
                                     ('observability-auth', ['oauth2-proxy'])]:
                for name in names:
                    obj = json.loads(kube('-n', namespace, 'get', 'secret', name, '-o', 'json'))
                    secrets.append({'apiVersion': 'v1', 'kind': 'Secret', 'type': obj['type'],
                                    'metadata': {'name': name, 'namespace': namespace}, 'data': obj['data']})
            (stage/'kubernetes-secrets.json').write_text(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': secrets}))
            shutil.copy2('/opt/updspace-infra/private/observability-oidc.json', stage/'observability-oidc.json')
            shutil.copytree('/opt/updspace-data/id-ydb/certs', stage/'ydb-certs')
            for path in [CONFIG/'applications.yaml', Path('/opt/updspace-data/id-ydb/ydb.yaml')]:
                shutil.copy2(path, stage/path.name)
            (stage/'run-state.json').write_text(json.dumps(state, indent=2))
        finally:
            restore(state, state_path)
        pause_seconds = round(time.monotonic() - started, 2)
        manifest = {str(p.relative_to(stage)): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage/'manifest.json').write_text(json.dumps(manifest, indent=2))
        plain = stage/'id.tar'
        with tarfile.open(plain, 'w') as archive:
            for path in sorted(stage.iterdir()):
                if path != plain:
                    archive.add(path, arcname=path.name)
        encrypted = Path('/srv/backups/updspace-id-encrypted')
        destination = encrypted/stamp
        destination.mkdir(parents=True, mode=0o700)
        cipher = destination/'id.tar.gpg'
        run(['gpg', '--batch', '--yes', '--trust-model', 'always', '--recipient-file', str(CONFIG/'backup-recipient.asc'),
             '--output', str(cipher), '--encrypt', str(plain)])
        result = {'stamp': stamp, 'method': 'quiesced-ydb-sparse-and-id-s3', 'sha256': digest(cipher),
                  'plaintext_sha256': digest(plain), 'encrypted_bytes': cipher.stat().st_size,
                  'pause_seconds': pause_seconds, 'media_objects': len(objects), 'committed': True}
        (destination/'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
        (destination/'COMMITTED').write_text(result['sha256'] + '\n')
        owner = pwd.getpwnam('updspace_m4tveevm')
        for path in [encrypted, destination, *destination.iterdir()]:
            os.chown(path, owner.pw_uid, owner.pw_gid)
        shutil.rmtree(stage)
        print(json.dumps(result))


if __name__ == '__main__':
    main()
