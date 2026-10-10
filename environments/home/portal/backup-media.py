#!/usr/bin/env python3
"""Take a consistent logical Portal media snapshot and encrypt it on the VM."""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import uuid

PROGRAM = '''import hashlib,io,json,os,sys,tarfile
os.environ.setdefault('DJANGO_SETTINGS_MODULE','app.settings')
import django;django.setup()
from django.conf import settings
import boto3
from botocore.client import Config
# Read object metadata before the public edge adds its cache policy.
s3=boto3.client('s3',endpoint_url='http://garage.updspace-data.svc.cluster.local:3900',region_name=settings.S3_REGION,aws_access_key_id=settings.S3_ACCESS_KEY_ID,aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,config=Config(signature_version='s3v4',s3={'addressing_style':'path'}));bucket=settings.NEWS_MEDIA_BUCKET
def inventory():
 return sorted([{'key':o['Key'],'etag':o['ETag'],'size':o['Size']} for p in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket) for o in p.get('Contents',[])],key=lambda x:x['key'])
before=inventory()
# shortcut: bounded in-memory snapshots; switch to streaming before media exceeds 64 MiB.
assert sum(o['size'] for o in before)<=64*1024*1024,'Media exceeds snapshot memory budget'
buffer=io.BytesIO();manifest={'format':1,'bucket':bucket,'objects':[]}
with tarfile.open(fileobj=buffer,mode='w') as archive:
 def add(name,data):
  item=tarfile.TarInfo(name);item.size=len(data);item.mode=0o600;archive.addfile(item,io.BytesIO(data))
 for i,o in enumerate(before):
  obj=s3.get_object(Bucket=bucket,Key=o['key']);data=obj['Body'].read()
  assert len(data)==o['size'] and obj['ETag']==o['etag'],'Media changed while reading'
  entry={**o,'file':f'objects/{i:08d}','sha256':hashlib.sha256(data).hexdigest(),'metadata':{k:obj[k] for k in ('ContentType','ContentEncoding','CacheControl','ContentDisposition','Metadata') if k in obj}}
  manifest['objects'].append(entry);add(entry['file'],data)
 assert before==inventory(),'Media inventory changed while reading'
 add('manifest.json',json.dumps(manifest,sort_keys=True).encode())
sys.stdout.buffer.write(buffer.getvalue())
'''


def verify_archive(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        expected = {'manifest.json', *(item['file'] for item in manifest['objects'])}
        members = archive.getmembers()
        assert manifest['format'] == 1 and len(members) == len(expected)
        assert {item.name for item in members} == expected and all(item.isfile() for item in members)
        for item in manifest['objects']:
            payload = archive.extractfile(item['file']).read()
            assert len(payload) == item['size'] and hashlib.sha256(payload).hexdigest() == item['sha256']
    return manifest


def main():
    os.umask(0o077)
    kube = ['k3s', 'kubectl', '-n', 'updspace-portal']
    pods = json.loads(subprocess.check_output(kube + ['get', 'pods', '-l',
        'app.kubernetes.io/name=activity,app.kubernetes.io/part-of=updspace-portal', '-o', 'json']))['items']
    ready = [p for p in pods if any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in p['status'].get('conditions', []))]
    if len(ready) != 1:
        raise RuntimeError('Expected one ready Activity pod')
    result = subprocess.run(kube + ['exec', ready[0]['metadata']['name'], '--', 'python', '-c', PROGRAM], capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError('Media snapshot failed; data output suppressed')
    manifest = verify_archive(result.stdout)
    name = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    folder = Path('/srv/updspace/backups/portal-media') / name
    folder.mkdir(mode=0o700, parents=True)
    archive = folder / 'media.tar.gpg'
    encrypted = subprocess.run(['gpg', '--batch', '--no-options', '--homedir', '/opt/updspace-data/gpg',
        '--recipient-file', '/opt/updspace-data/backup-recipient.asc', '--output', str(archive), '--encrypt'],
        input=result.stdout, capture_output=True, timeout=120)
    if encrypted.returncode:
        raise RuntimeError('Media encryption failed; incomplete folder retained')
    with archive.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    (folder / 'media.tar.gpg.sha256').write_text(sha + '  media.tar.gpg\n')
    (folder / 'COMMITTED').touch()
    print(json.dumps({'backup': name, 'objects': len(manifest['objects']), 'bytes': sum(x['size'] for x in manifest['objects']), 'inventory_stable': True}))


if __name__ == '__main__':
    main()
