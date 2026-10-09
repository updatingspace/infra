#!/usr/bin/env python3
"""Audit Garage ownership; optionally reconcile only existing bucket CORS/lifecycle."""
import argparse
import json
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parent
KUBE = ['k3s', 'kubectl', '-n', 'updspace-data']


def api(operation, params=None):
    result = subprocess.run(KUBE + ['exec', 'garage-0', '--', '/garage', 'json-api',
                            operation, json.dumps(params)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'Garage {operation} failed; no automatic retry')
    return json.loads(result.stdout)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def audit(desired, call=api):
    layout = call('GetClusterLayout')
    expected = desired['layout']
    require(layout['version'] == expected['version'] and not layout['stagedRoleChanges']
            and layout.get('stagedParameters') is None, 'Unexpected layout version or staged changes')
    require(len(layout['roles']) == 1, 'Unexpected Garage nodes; refusing any apply')
    node = layout['roles'][0]
    require(node['id'].startswith(expected['node_id_prefix']) and node['zone'] == expected['zone']
            and round(node['capacity'] / 1024**3, 1) == expected['capacity_gib'], 'Unexpected layout role')
    buckets = {}
    for name, principal in desired['principals'].items():
        key = call('GetKeyInfo', {'search': name, 'showSecretKey': False})
        require('secretAccessKey' not in key and key['name'] == name and not key['expired']
                and key['permissions'] == {'createBucket': False}, 'Unexpected runtime key state')
        rights = {flag: flag in principal['permissions'] for flag in ('read', 'write')}
        rights['owner'] = principal['owner']
        actual = sorted(alias for bucket in key['buckets'] for alias in bucket['globalAliases'])
        require(actual == sorted(principal['buckets']) and len(key['buckets']) == len(actual)
                and all(not b['localAliases'] and b['permissions'] == rights for b in key['buckets']),
                'Unexpected key permissions; refusing any apply')
        for alias in principal['buckets']:
            bucket = call('GetBucketInfo', {'globalAlias': alias})
            require(bucket['globalAliases'] == [alias] and not bucket['websiteAccess']
                    and len(bucket['keys']) == 1 and bucket['keys'][0]['accessKeyId'] == key['accessKeyId']
                    and bucket['keys'][0]['permissions'] == rights, 'Unexpected bucket ownership')
            buckets[alias] = bucket
    return buckets


def settings_body(kind, value):
    if kind == 'lifecycle':
        require(set(value) == {'Rules'} and bool(value['Rules']), 'Invalid lifecycle configuration')
        return {'lifecycleRules': value['Rules']}
    require(kind == 'cors' and set(value) == {'CORSRules'}, 'Unknown bucket setting')
    names = {'AllowedMethods': 'AllowedMethod', 'AllowedHeaders': 'AllowedHeader',
             'AllowedOrigins': 'AllowedOrigin', 'ExposeHeaders': 'ExposeHeader', 'MaxAgeSeconds': 'MaxAgeSeconds'}
    rules = []
    for rule in value['CORSRules']:
        require(set(rule) <= names.keys() and rule.get('AllowedMethods') and rule.get('AllowedOrigins'),
                'Unsupported CORS fields; refusing lossy conversion')
        rules.append({names[k]: v for k, v in rule.items()})
    require(bool(rules), 'Empty CORS configuration is not supported by this reconciler')
    return {'corsRules': rules}


def reconcile(desired, settings, apply=False, call=api):
    buckets = audit(desired, call)
    changes = []
    for principal, alias, kind, value in settings:
        require(alias in desired['principals']['updspace-' + principal]['buckets'], 'Setting targets wrong principal')
        body = settings_body(kind, value)
        field = next(iter(body))
        bucket = buckets[alias]
        if bucket.get(field) != body[field]:
            changes.append({'bucket': alias, 'setting': kind})
            if apply:
                current = call('GetBucketInfo', {'globalAlias': alias})
                require(current['id'] == bucket['id'] and current['keys'] == bucket['keys']
                        and current.get(field) == bucket.get(field), 'Concurrent Garage change; inspect before retry')
                call('UpdateBucket', {'id': bucket['id'], 'body': body})
                verified = call('GetBucketInfo', {'globalAlias': alias})
                require(verified.get(field) == body[field], 'Bucket settings readback mismatch')
                buckets[alias] = verified
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply CORS/lifecycle drift only; never alter keys/layout/data')
    args = parser.parse_args()
    desired = json.loads((ROOT/'desired-state.json').read_text())
    configmap = json.loads(subprocess.check_output(KUBE + ['get', 'configmap/garage-config', '-o', 'json']))
    config = tomllib.loads(configmap['data']['garage.toml'])
    require(config['replication_factor'] == desired['replication_factor']
            and config['s3_api']['s3_region'] == desired['region'], 'Unexpected Garage replication/region')
    changes = reconcile(desired, json.loads((ROOT/'bucket-settings.json').read_text()), args.apply)
    print(json.dumps({'mode': 'apply' if args.apply else 'check', 'layout_and_permissions_match': True,
                      'changes': changes, 'matches': not changes or args.apply}))
    return 0 if not changes or args.apply else 2


if __name__ == '__main__':
    raise SystemExit(main())
