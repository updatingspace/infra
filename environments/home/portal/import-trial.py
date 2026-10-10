#!/usr/bin/env python3
"""Import a committed snapshot into dormant Portal schemas and verify every table."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--replace-trial', action='store_true')
    args = parser.parse_args()
    if not args.replace_trial:
        raise ValueError('Pass --replace-trial only for a dormant, backed-up candidate')
    folder = args.snapshot.parent
    checksum = (folder / 'SHA256SUMS').read_text()
    if not (folder / 'COMMITTED').is_file() or not re.fullmatch(r'[0-9a-f]{64}  snapshot.json\n', checksum):
        raise ValueError('Snapshot is not committed')
    raw = args.snapshot.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checksum.split()[0]:
        raise ValueError('Snapshot archive checksum mismatch')
    snapshot = json.loads(raw)
    if snapshot['format'] != 1:
        raise ValueError('Unsupported snapshot format')
    root = Path('/opt/updspace-portal-migration')
    source = json.loads((root / 'source-runtime.json').read_text())
    command = ['k3s', 'kubectl', '-n', 'updspace-portal']
    deployments = json.loads(subprocess.check_output(command + ['get', 'deployments', '-o', 'json'], text=True))['items']
    deployments = [x for x in deployments if x['metadata']['name'] != 'frontend']
    if {x['metadata']['name'] for x in deployments} != set(source['services']) or any(x['spec']['replicas'] or x.get('status', {}).get('replicas', 0) for x in deployments):
        raise ValueError('Every Portal application must be scaled to zero before replacement')
    coverage = {table: [] for table in snapshot['tables']}
    requests = {}
    for service in source['services']:
        metadata = json.loads((root / (service + '-models.json')).read_text())
        owned = {model['table'] for model in metadata['models']}
        requests[service] = {table: value for table, value in snapshot['tables'].items() if table in owned}
        for table in requests[service]:
            coverage[table].append(service)
    allowed_shared = {'auth_group', 'auth_permission', 'auth_user', 'django_content_type', 'django_session'}
    for table, owners in coverage.items():
        if not owners or (len(owners) > 1 and (table not in allowed_shared or snapshot['tables'][table]['rows'])):
            raise ValueError('Unmapped or nonempty shared source table requires explicit review: ' + table)
    tools = Path(__file__).parent
    codec = (tools / 'portal_ydb_codec.py').read_text()
    importer = (tools / 'import-ydb-service.py').read_text()
    program = "import types, sys\nm=types.ModuleType('portal_ydb_codec')\nexec(" + repr(codec) + ",m.__dict__)\nsys.modules[m.__name__]=m\n" + importer
    def apply(service):
        payload = {'service': service, 'tables': requests[service], 'replace_trial': True}
        result = subprocess.run(command + ['exec', '-i', 'inspect-' + service, '--', 'python', '-c', program],
            input=json.dumps(payload), capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise RuntimeError('Import failed for ' + service + '; inspect candidate before retry, raw output suppressed')
        proof = json.loads(result.stdout)
        (folder / (service + '-import-proof.json')).write_text(json.dumps(proof, indent=2))
        print(service + ': verified ' + str(len(proof['tables'])) + ' tables, ' + str(sum(t['rows'] for t in proof['tables'])) + ' rows', flush=True)
        return proof
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        proofs = list(pool.map(apply, source['services']))
    (folder / 'IMPORT-VERIFIED.json').write_text(json.dumps({'services': len(proofs), 'source_tables': len(coverage),
        'source_rows': sum(len(table['rows']) for table in snapshot['tables'].values()), 'all_matched': True}, indent=2))
    print('All source tables verified; applications and CronJobs remain dormant')


if __name__ == '__main__':
    main()
