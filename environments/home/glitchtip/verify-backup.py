#!/usr/bin/env python3
"""Restore an encrypted off-host backup into a disposable, loopback-only Postgres."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import uuid

SSH = ['ssh', '-o', 'BatchMode=yes', 'updspace_m4tveevm@192.168.1.176']
IMAGE = 'docker.io/library/postgres:18.6-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    archive = args.backup / 'postgres.tar.gpg'
    expected = (args.backup / 'postgres.tar.gpg.sha256').read_text().split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
    decrypted = subprocess.run(['gpg', '--batch', '--no-options', '--homedir',
        str(Path.home() / '.local/share/updspace-backups/keys'), '--decrypt', str(archive)],
        capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(decrypted)) as tar:
        dump = tar.extractfile('glitchtip.dump').read()
        contents = tar.extractfile('contents.txt').read()
        sums = dict(line.split('  ', 1)[::-1] for line in tar.extractfile('SHA256SUMS').read().decode().splitlines())
        for filename, body in [('glitchtip.dump', dump), ('contents.txt', contents)]:
            assert hashlib.sha256(body).hexdigest() == sums[filename]
        credentials = json.load(tar.extractfile('glitchtip-credentials.json'))
        assert {'database_password', 'secret_key', 'admin_email', 'admin_password', 'dsn'} <= credentials.keys()
        uploads = [m for m in tar.getmembers() if m.isfile() and m.name.startswith('uploads/')]
        assert all('..' not in Path(m.name).parts for m in uploads)
        upload_hashes = {m.name: hashlib.sha256(tar.extractfile(m).read()).hexdigest() for m in uploads}
    name = 'glitchtip-restore-' + uuid.uuid4().hex[:8]
    command = ['sudo', '-n', 'k3s', 'kubectl', '-n', 'glitchtip']
    def kube(arguments, data=None):
        # Arguments are generated locally, not read from the archive.
        import shlex
        return subprocess.run(SSH + [shlex.join(command + arguments)], input=data, capture_output=True, check=True).stdout
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': 'glitchtip'},
        'spec': {'restartPolicy': 'Never', 'activeDeadlineSeconds': 600, 'automountServiceAccountToken': False,
            'securityContext': {'runAsUser': 999, 'runAsGroup': 999, 'fsGroup': 999, 'runAsNonRoot': True},
            'containers': [{'name': 'postgres', 'image': IMAGE, 'args': ['-c', 'listen_addresses=127.0.0.1'],
                'env': [{'name': 'POSTGRES_HOST_AUTH_METHOD', 'value': 'trust'}, {'name': 'POSTGRES_DB', 'value': 'glitchtip'}],
                'resources': {'requests': {'cpu': '125m', 'memory': '256Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}},
                'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                'readinessProbe': {'exec': {'command': ['pg_isready', '-h', '127.0.0.1', '-U', 'postgres']}, 'periodSeconds': 2},
                'volumeMounts': [{'name': 'data', 'mountPath': '/var/lib/postgresql'}]}],
            'volumes': [{'name': 'data', 'emptyDir': {'sizeLimit': '1Gi'}}]}}
    kube(['create', '-f', '-'], json.dumps(pod).encode())
    try:
        kube(['wait', '--for=condition=Ready', 'pod/' + name, '--timeout=60s'])
        sql = ['exec', '-i', name, '--', 'psql', '-XqAt', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'glitchtip']
        kube(sql, b'CREATE ROLE glitchtip LOGIN; ALTER DATABASE glitchtip OWNER TO glitchtip;')
        kube(['exec', '-i', name, '--', 'pg_restore', '--exit-on-error', '-U', 'postgres', '-d', 'glitchtip'], dump)
        result = json.loads(kube(sql, b"""SELECT json_build_object(
          'users',(SELECT count(*) FROM users_user),
          'projects',(SELECT count(*) FROM projects_project),
          'errors',(SELECT count(*) FROM issue_events_issueevent),
          'transactions',(SELECT count(*) FROM performance_transactiongroup),
          'spans',(SELECT count(*) FROM performance_spanstaging));"""))
        assert result['users'] >= 1 and result['projects'] >= 1 and result['errors'] >= 1 and result['transactions'] >= 1
        # Spans may already have moved into the included Parquet files.
        assert result['spans'] >= 1 or upload_hashes
        print(json.dumps({'backup': args.backup.name, 'encryptedSha256': expected,
            'isolatedDatabaseRestore': result, 'uploadFiles': len(upload_hashes), 'privateCredentialsIncluded': True}))
    finally:
        kube(['delete', 'pod', name, '--wait=true', '--timeout=45s'])


if __name__ == '__main__':
    main()
