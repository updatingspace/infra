#!/usr/bin/env python3
"""Restore decrypted Portal backup inputs into an isolated pod and verify source rows."""
import argparse
import datetime
import json
from pathlib import Path
import re
import subprocess
import uuid

from portal_ydb_codec import digest, normalize

KUBE = ['k3s', 'kubectl', '-n', 'updspace-data']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', type=Path)
    parser.add_argument('source_snapshot', type=Path)
    args = parser.parse_args()
    source = json.loads(args.source_snapshot.read_text())
    root = Path('/opt/updspace-portal-migration')
    metadata = {p.name.removesuffix('-models.json'): json.loads(p.read_text()) for p in root.glob('*-models.json')}
    image = subprocess.check_output(KUBE + ['get', 'statefulset', 'postgres', '-o', 'jsonpath={.spec.template.spec.containers[0].image}'], text=True)
    pod = 'portal-restore-' + uuid.uuid4().hex[:8]
    manifest = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': pod, 'namespace': 'updspace-data',
        'labels': {'app.kubernetes.io/name': 'portal-restore-check'}}, 'spec': {
        'automountServiceAccountToken': False, 'restartPolicy': 'Never',
        'securityContext': {'runAsUser': 999, 'runAsGroup': 999, 'fsGroup': 999, 'runAsNonRoot': True,
                            'seccompProfile': {'type': 'RuntimeDefault'}},
        'containers': [{'name': 'postgres', 'image': image, 'args': ['-c', 'listen_addresses=127.0.0.1'],
            'env': [{'name': 'POSTGRES_DB', 'value': 'updspace'}, {'name': 'POSTGRES_HOST_AUTH_METHOD', 'value': 'trust'}],
            'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
            'readinessProbe': {'exec': {'command': ['pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'updspace']}, 'periodSeconds': 2},
            'volumeMounts': [{'name': 'data', 'mountPath': '/var/lib/postgresql'}, {'name': 'runtime', 'mountPath': '/var/run/postgresql'}]}],
        'volumes': [{'name': 'data', 'emptyDir': {'sizeLimit': '512Mi'}}, {'name': 'runtime', 'emptyDir': {}}]}}
    subprocess.run(KUBE + ['create', '-f', '-'], input=json.dumps(manifest), text=True, check=True)
    def sql(statement):
        result = subprocess.run(KUBE + ['exec', '-i', pod, '--', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'updspace'],
            input=statement, capture_output=True, text=True, timeout=120)
        if result.returncode: raise RuntimeError('Restore verification SQL failed; data output suppressed')
        return result.stdout.strip()
    try:
        subprocess.run(KUBE + ['wait', '--for=condition=Ready', 'pod/' + pod, '--timeout=60s'], check=True, timeout=65)
        roles = '\n'.join(line for line in (args.inputs / 'roles.sql').read_text().splitlines() if line != 'CREATE ROLE postgres;')
        sql(roles)
        with (args.inputs / 'updspace.dump').open('rb') as data:
            result = subprocess.run(KUBE + ['exec', '-i', pod, '--', 'pg_restore', '--exit-on-error', '-U', 'postgres', '-d', 'updspace'],
                stdin=data, capture_output=True, timeout=120)
        if result.returncode: raise RuntimeError('Restore failed; data output suppressed')
        coverage = set()
        sequence_count = 0
        for service, info in metadata.items():
            schema = info['schema']
            selects = []
            tables = []
            sequences = []
            for model in info['models']:
                table = model['table']
                if not re.fullmatch('[a-z0-9_]+', table + schema): raise ValueError('Unexpected identifier')
                qualified = '"' + schema + '"."' + table + '"'
                if table in source['tables']:
                    selects.append("'" + table + "', COALESCE((SELECT jsonb_agg(row_to_json(t)) FROM (SELECT * FROM " + qualified + ") t), '[]'::jsonb)")
                    tables.append(table)
                for field in model['fields']:
                    if field['type'] in ('AutoField', 'BigAutoField', 'SmallAutoField'):
                        column = field['column']
                        sequences.append("jsonb_build_object('table','" + table + "','next',nextval(pg_get_serial_sequence('" + qualified + "','" + column + "')),'maximum',(SELECT max(\"" + column + "\") FROM " + qualified + '))')
            actual = json.loads(sql('SELECT jsonb_build_object(' + ','.join(selects) + ');'))
            for table in tables:
                expected = source['tables'][table]
                rows = []
                for row in actual[table]:
                    normalized = {}
                    for column in expected['columns']:
                        value = row[column['name']]
                        kind = column['type'].removesuffix('?')
                        if value is not None and kind in ('Datetime', 'Timestamp'): value = datetime.datetime.fromisoformat(value)
                        if value is not None and kind == 'Date': value = datetime.date.fromisoformat(value)
                        normalized[column['name']] = normalize(value, column['type'])
                    rows.append(normalized)
                if len(rows) != len(expected['rows']) or digest(rows) != expected['sha256']:
                    raise ValueError('Restored source table differs: ' + service + '/' + table)
                coverage.add(table)
            if sequences:
                values = json.loads(sql('SELECT jsonb_build_array(' + ','.join(sequences) + ');'))
                if any(item['next'] is None or item['next'] <= (item['maximum'] if item['maximum'] is not None else 0) for item in values):
                    raise ValueError('Restored sequence would reuse an existing ID')
                sequence_count += len(values)
        if coverage != set(source['tables']): raise ValueError('Incomplete restored table coverage')
        owners = sql("SELECT count(*) FROM pg_namespace WHERE (nspname='id' OR starts_with(nspname,'portal_')) AND nspname=pg_get_userbyid(nspowner);")
        if owners != '9': raise ValueError('Unexpected schema owners')
        result = {'restored_source_tables': len(coverage), 'restored_source_rows': sum(len(t['rows']) for t in source['tables'].values()),
                  'schema_owners': 9, 'sequences_verified': sequence_count, 'sha256_all_match': True, 'contains_portal_business_data': True}
        (args.inputs / 'verification.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    finally:
        subprocess.run(KUBE + ['delete', 'pod', pod, '--wait=true', '--timeout=60s'], check=True, timeout=65)


if __name__ == '__main__': main()
