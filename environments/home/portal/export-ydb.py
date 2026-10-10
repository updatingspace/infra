#!/usr/bin/env python3
"""Read a YDB snapshot and stream it over SSH to protected storage on the VM."""
import argparse
import datetime
import json
import os
import subprocess



from portal_ydb_codec import normalize, digest


def main():
    import ydb
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--endpoint', default='grpcs://ydb.serverless.yandexcloud.net:2135')
    args = parser.parse_args()
    os.environ['YC_CLI_INITIALIZATION_SILENCE'] = 'true'
    result = subprocess.run(['yc', 'iam', 'create-token'], capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('Unable to obtain temporary read token')
    settings = (ydb.TableClientSettings().with_native_datetime_in_result_sets(True)
                .with_native_timestamp_in_result_sets(True).with_native_date_in_result_sets(True)
                .with_native_json_in_result_sets(True))
    tables = {}
    config = ydb.DriverConfig(endpoint=args.endpoint, database=args.database,
        credentials=ydb.AccessTokenCredentials(result.stdout.strip()), table_client_settings=settings)
    with ydb.Driver(config) as driver:
        driver.wait(timeout=25, fail_fast=True)
        with ydb.SessionPool(driver) as pool:
            entries = driver.scheme_client.list_directory(args.database).children
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name == '.sys':
                    continue
                if not entry.is_table():
                    raise ValueError('Unexpected non-table entry; inspect the source')
                path = args.database.rstrip('/') + '/' + entry.name
                def read(session):
                    description = session.describe_table(path)
                    columns = [{'name': c.name, 'type': str(c.type)} for c in description.columns]
                    rows = []
                    for chunk in session.read_table(path, ordered=True, use_snapshot=True):
                        rows.extend({c['name']: normalize(row[c['name']], c['type']) for c in columns} for row in chunk.rows)
                    return {'columns': columns, 'primary_key': list(description.primary_key), 'rows': rows, 'sha256': digest(rows)}
                tables[entry.name] = pool.retry_operation_sync(read)
    snapshot = {'format': 1, 'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'consistency': 'per-table snapshot; writer freeze must be verified separately', 'tables': tables}
    receiver = """import datetime,hashlib,json,os,pathlib,sys,uuid
os.umask(0o077)
value=json.load(sys.stdin)
assert value['format']==1 and value['tables']
root=pathlib.Path('/opt/updspace-portal-migration/exports')
root.mkdir(mode=0o700,parents=True,exist_ok=True)
name=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
folder=root/name
folder.mkdir(mode=0o700)
payload=json.dumps(value,sort_keys=True,ensure_ascii=False).encode()
(folder/'snapshot.json').write_bytes(payload)
(folder/'SHA256SUMS').write_text(hashlib.sha256(payload).hexdigest()+'  snapshot.json\\n')
(folder/'COMMITTED').touch()
print('Snapshot '+name+': '+str(len(value['tables']))+' tables, '+str(sum(len(t['rows']) for t in value['tables'].values()))+' rows; root-only on VM')
"""
    import shlex
    result = subprocess.run(['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        'updspace_m4tveevm@192.168.1.176', 'sudo -n python3 -c ' + shlex.quote(receiver)],
        input=json.dumps(snapshot, ensure_ascii=False, allow_nan=False), capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Snapshot transfer failed; inspect VM before retrying')
    print(result.stdout.strip())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('YDB export failed (' + type(error).__name__ + '); rows and credentials suppressed') from None
