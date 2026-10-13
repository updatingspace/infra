#!/usr/bin/env python3
"""Copy Portal media into its private Garage bucket and compare complete bodies."""
import json
from pathlib import Path
import subprocess

PROGRAM = '''import hashlib,json,sys
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
value=json.load(sys.stdin)
def client(settings):
 return boto3.client('s3',endpoint_url=settings['endpoint'],region_name=settings['region'],aws_access_key_id=settings['access_key_id'],aws_secret_access_key=settings['secret_access_key'],config=Config(signature_version='s3v4',s3={'addressing_style':'path'},connect_timeout=10,read_timeout=40,retries={'max_attempts':2}))
source=client(value['source']);target=client(value['target']);bucket=value['bucket'];proof=[]
for page in source.get_paginator('list_objects_v2').paginate(Bucket=bucket):
 for entry in page.get('Contents',[]):
  key=entry['Key'];obj=source.get_object(Bucket=bucket,Key=key);data=obj['Body'].read();sha=hashlib.sha256(data).hexdigest()
  if len(data)!=entry['Size']:raise ValueError('Source object changed while reading')
  try:
   existing=target.get_object(Bucket=bucket,Key=key)['Body'].read()
  except ClientError as error:
   if error.response['Error']['Code'] not in ('NoSuchKey','404'):raise
   options={k:obj[k] for k in ('ContentType','ContentEncoding','CacheControl','ContentDisposition','Metadata') if k in obj}
   target.put_object(Bucket=bucket,Key=key,Body=data,**options)
  else:
   if hashlib.sha256(existing).hexdigest()!=sha:raise ValueError('Destination has different data; refusing overwrite')
  copied=target.get_object(Bucket=bucket,Key=key)['Body'].read()
  if hashlib.sha256(copied).hexdigest()!=sha:raise ValueError('Destination verification failed')
  proof.append({'key':key,'size':len(data),'sha256':sha})
print(json.dumps(proof))
'''


def main():
    root = Path('/opt/updspace-portal-migration')
    source = json.loads((root / 'source-runtime.json').read_text())['services']['activity']['environment']
    target = json.loads(Path('/opt/updspace-data/garage/portal-credentials.json').read_text())
    bucket = source['NEWS_MEDIA_BUCKET']
    if bucket not in target['buckets']:
        raise ValueError('Portal bucket not declared in Garage credentials')
    value = {'source': {'endpoint': source['S3_ENDPOINT_URL'], 'region': source['S3_REGION'],
        'access_key_id': source['S3_ACCESS_KEY_ID'], 'secret_access_key': source['S3_SECRET_ACCESS_KEY']},
        'target': target, 'bucket': bucket}
    result = subprocess.run(['k3s', 'kubectl', '-n', 'updspace-portal', 'exec', '-i', 'inspect-activity',
        '--', 'python', '-c', PROGRAM], input=json.dumps(value), text=True, capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError('Media copy failed; raw output suppressed, inspect state before retrying')
    proof = json.loads(result.stdout)
    (root / 'media-copy-proof.json').write_text(json.dumps(proof, indent=2))
    print('Media verified:', len(proof), 'objects,', sum(x['size'] for x in proof), 'bytes; SHA256 match')


if __name__ == '__main__':
    main()
