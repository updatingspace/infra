#!/usr/bin/env python3
"""Manage existing Monium dashboards via the official Monitoring v3 gRPC API.

No create/delete: custom projects are not supported by the Terraform resource.
Update uses an etag and verifies a fresh read. Run `check` to detect UI drift.
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

FIELDS = ('name', 'description', 'labels', 'title', 'widgets', 'parametrization',
          'timeline', 'links', 'preset_items')
ENDPOINT = 'monitoring.api.cloud.yandex.net:443'
SERVICE = 'yandex.cloud.monitoring.v3.DashboardService/'

def canonical(value):
    return {k: value[k] for k in FIELDS if value.get(k) not in (None, '', [], {})}

def digest(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def rpc(method, body):
    env = os.environ.copy()
    env['PZ_YC_TOKEN'] = env.get('YC_TOKEN') or subprocess.check_output(['yc','iam','create-token'],text=True).strip()
    result = subprocess.run([env.get('GRPCURL_BIN','grpcurl'), '-expand-headers',
        '-H', 'Authorization: Bearer ${PZ_YC_TOKEN}', '-d', '@', ENDPOINT, method],
        input=json.dumps(body),text=True,capture_output=True,env=env)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action', choices=['export','check','apply'])
    parser.add_argument('path', type=pathlib.Path)
    parser.add_argument('--id')
    args=parser.parse_args()
    desired=json.loads(args.path.read_text()) if args.action!='export' else {'id':args.id}
    if not desired.get('id'): parser.error('Dashboard ID is required')
    live=rpc(SERVICE+'Get',{'dashboard_id':desired['id']})
    if args.action=='export':
        args.path.write_text(json.dumps({'id':live['id'],**canonical(live)},ensure_ascii=False,indent=2)+'\n')
        print(live['id']+' exported');return
    if digest(live)==digest(desired):
        print(live['id']+' unchanged');return
    if args.action=='check':
        print(live['id']+' drift: live content differs from desired');sys.exit(2)
    # Existing dashboard is updated in place; concurrent UI edits are rejected by etag.
    update={**canonical(desired),'dashboard_id':live['id'],'etag':live['etag'],
            'comment':'Apply desired configuration from infrastructure/terraform'}
    operation=rpc(SERVICE+'Update',update)
    deadline=time.monotonic()+45
    while not operation.get('done'):
        if time.monotonic()>deadline: raise RuntimeError('Update still pending: '+operation['id'])
        time.sleep(1)
        operation=rpc('yandex.cloud.operation.OperationService/Get',{'operation_id':operation['id']})
    if operation.get('error'): raise RuntimeError(str(operation['error']))
    after=rpc(SERVICE+'Get',{'dashboard_id':desired['id']})
    if digest(after)!=digest(desired): raise RuntimeError('Readback differs from desired content')
    print(live['id']+' updated and verified')

if __name__=='__main__': main()
