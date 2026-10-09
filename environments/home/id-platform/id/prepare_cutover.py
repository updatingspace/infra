#!/usr/bin/env python3
"""Prepare an ID-only source freeze/rollback bundle; never change cloud resources."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

import yaml

GATEWAY = 'd5d6almt4c5i2ao9e4ha'
DATABASE = 'etnq1cp5vubgd8ono4t4'
RUNTIME_ACCOUNT = 'aje5cbnkoueu47koscvk'
REVISIONS = {
    'bbak8734de8cabdaorc7': 'bbas4oqu5d6vbrg8cjr5',
    'bba3ev9oenabp55se69m': 'bbamoocb3r53ga9atqkv',
    'bbamj363kmhlj2nof0mo': 'bbado17i6fh25jeuplho',
    'bbai4bjc8f21qg5fvjht': 'bbano93nimdijpannom5',
    'bba9v82d1op2qbh2kqtt': 'bba7ele9ti9d4m324m43',
}
TRIGGERS = {
    'a1smb0u230llgnf30kae', 'a1spdo2pukcpeeoogmkv', 'a1s6opgsogj40a445kck',
    'a1s2ok45pgnc4vmtulnm', 'a1sqbhciude97njt20lc',
}
METHODS = {'get', 'head', 'post', 'put', 'patch', 'delete', 'options', 'x-yc-apigateway-any-method'}
INTEGRATION = 'x-yc-apigateway-integration'
DB_BINDING = {'role_id': 'ydb.editor', 'subject': {'id': RUNTIME_ACCOUNT, 'type': 'serviceAccount'}}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def maintenance_spec(source):
    require(set(source) == {'openapi', 'info', 'servers', 'paths'}, 'Unexpected gateway root fields')
    result = copy.deepcopy(source)
    count = 0
    for operations in result['paths'].values():
        require(set(operations) <= METHODS | {'parameters'}, 'Unexpected gateway operation')
        for method, operation in list(operations.items()):
            if method not in METHODS:
                continue
            integration = operation.get(INTEGRATION, {})
            require(integration.get('type') == 'serverless_containers'
                    and integration.get('container_id') in REVISIONS, 'Unexpected gateway backend')
            # Preserve path parameters while removing every backend/authorizer hook.
            operations[method] = {
                **({'parameters': operation['parameters']} if 'parameters' in operation else {}),
                'responses': {'503': {'description': 'ID migration in progress'}},
                INTEGRATION: {'type': 'dummy', 'http_code': 503,
                    'http_headers': {'Content-Type': 'text/plain; charset=utf-8',
                                     'Cache-Control': 'no-store', 'Retry-After': '120'},
                    'content': {'*': 'ID is temporarily unavailable during migration. Please retry later.'}},
            }
            count += 1
    require(count == 131 and len(result['paths']) == 81, 'Gateway route inventory changed')
    return result


def prepare(inventory, db_bindings, functions, gateway):
    require(inventory['gateway']['id'] == GATEWAY, 'Wrong gateway')
    require({x['id'] for x in inventory['containers']} == set(REVISIONS), 'Container inventory changed')
    require({x['id'] for x in inventory['triggers']} == TRIGGERS, 'Trigger inventory changed')
    require(not functions, 'Unexpected legacy ID functions')
    require(db_bindings == [DB_BINDING], 'Database permissions changed')
    # A parent role cannot be revoked by removing a binding on the database.
    for binding in inventory['folder_bindings']:
        if binding['subject']['id'] == RUNTIME_ACCOUNT:
            require(binding['role_id'] in {'container-registry.images.puller', 'logging.writer'},
                    'Runtime account has unexpected inherited permissions')
    pause, resume, remove, restore, timeouts = [], [], [], [], []
    for item in inventory['containers']:
        active = [x for x in item['revisions'] if x.get('status') == 'ACTIVE']
        require(len(active) == 1 and active[0]['id'] == REVISIONS[item['id']], 'Active revision changed')
        timeout = active[0]['execution_timeout']
        require(timeout.endswith('s') and timeout[:-1].isdigit(), 'Unrecognized timeout')
        timeouts.append(int(timeout[:-1]))
        for binding in item['bindings']:
            require(binding['role_id'] == 'serverless.containers.invoker'
                    and binding['subject']['type'] == 'serviceAccount', 'Unexpected container binding')
            args = ['--id', item['id'], '--role', binding['role_id'],
                    '--subject', 'serviceAccount:' + binding['subject']['id']]
            remove.append(['yc', 'serverless', 'container', 'remove-access-binding', *args])
            restore.append(['yc', 'serverless', 'container', 'add-access-binding', *args])
    for trigger in inventory['triggers']:
        require(trigger['status'] in {'ACTIVE', 'PAUSED'}, 'Unexpected trigger status')
        rule = trigger['rule']
        require(set(rule) == {'timer'} and rule['timer']['invoke_container_with_retry']['container_id']
                == 'bba9v82d1op2qbh2kqtt', 'Trigger target changed')
        if trigger['status'] == 'ACTIVE':
            pause.append(['yc', 'serverless', 'trigger', 'pause', '--id', trigger['id']])
            resume.append(['yc', 'serverless', 'trigger', 'resume', '--id', trigger['id']])
    db_args = ['--id', DATABASE, '--role', 'ydb.editor', '--service-account-id', RUNTIME_ACCOUNT]
    return maintenance_spec(gateway), {
        'source_observed_at': inventory['stamp'], 'applied': False,
        'freeze': {
            'pause_previously_active_triggers': pause,
            'gateway_maintenance': ['yc', 'serverless', 'api-gateway', 'update', '--id', GATEWAY,
                                    '--spec', 'gateway-maintenance.yaml'],
            'remove_direct_invocation_bindings': remove,
            'drain_seconds_after_verified_invocation_fence': max(timeouts),
            'fence_database_runtime': ['yc', 'ydb', 'database', 'remove-access-binding', *db_args],
        },
        'rollback_before_target_accepts_writes': {
            'restore_database_runtime': ['yc', 'ydb', 'database', 'add-access-binding', *db_args],
            'restore_direct_invocation_bindings': restore,
            'restore_gateway': ['yc', 'serverless', 'api-gateway', 'update', '--id', GATEWAY,
                                '--spec', 'gateway-before.yaml'],
            'resume_previously_active_triggers': resume,
        },
        'required_checks': [
            'Refresh this snapshot and compare revisions immediately before applying.',
            'Coordinate source CI/deploy writers; parent admin roles remain unchanged.',
            'Verify all triggers paused, maintenance 503, bindings removed, and runtime YDB access denied.',
            'After the last successful source invocation, drain for the full maximum execution timeout.',
            'IAM propagation is asynchronous: verify denial before the final database/media snapshot.',
            'After target writes begin, DNS-only rollback is unsafe; stop both writers and reconcile data.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path, help='Private directory produced by the live source inventory')
    parser.add_argument('output', type=Path, help='New private directory for the reviewed cutover bundle')
    args = parser.parse_args()
    os.umask(0o077)
    read = lambda name: json.loads((args.snapshot / name).read_text())
    before = (args.snapshot/'gateway.yaml').read_bytes()
    maintenance, plan = prepare(read('inventory.json'), read('database-bindings.json'),
                                read('functions.json'), yaml.safe_load(before))
    args.output.mkdir(mode=0o700)
    (args.output/'gateway-before.yaml').write_bytes(before)
    (args.output/'gateway-maintenance.yaml').write_text(yaml.safe_dump(maintenance, sort_keys=False))
    (args.output/'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(args.output.iterdir())}
    (args.output/'SHA256.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'prepared': str(args.output), 'operations': 131, 'cloud_mutations': 0,
                      'maximum_drain_seconds': plan['freeze']['drain_seconds_after_verified_invocation_fence']}))


if __name__ == '__main__':
    main()
