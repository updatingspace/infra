import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
from unittest.mock import patch
import yaml
import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_failed_dump_resumes_the_app_and_never_commits_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / 'k3s'
            command.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
p=Path(os.environ['TEST_STATE'])
args=sys.argv[1:]
if 'get' in args: print(p.read_text())
elif 'scale' in args:
    requested=next(x.split('=',1)[1] for x in args if x.startswith('--replicas='))
    p.write_text(requested)
elif 'pg_dump' in args: sys.exit(42)
''')
            command.chmod(0o700)
            state = root / 'replicas'
            state.write_text('1')
            script = Path(__file__).with_name('backup.sh').read_text().replace(
                '/run/lock/glitchtip-backup.lock', str(root / 'lock')).replace(
                '/srv/updspace/backups/glitchtip', str(root / 'backups'))
            result = subprocess.run(['bash'], input=script, text=True, capture_output=True,
                env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], TEST_STATE=str(state)))
            self.assertEqual(result.returncode, 42)
            self.assertEqual(state.read_text(), '1')
            self.assertFalse(list((root / 'backups').rglob('COMMITTED')))

    def test_credentials_preserved_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'credentials.json'
            current = {'role': None, 'owner': None}
            first = bootstrap.credentials(current, path)
            self.assertEqual(bootstrap.credentials(current, path), first)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                bootstrap.credentials(current, path)

    def test_existing_database_without_keys_never_rotates_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.json'
            with self.assertRaises(ValueError):
                bootstrap.credentials({'role': {'login': True}, 'owner': 'glitchtip'}, path)
            self.assertFalse(path.exists())

    def test_foreign_owner_and_extra_privileges_are_rejected(self):
        with self.assertRaises(ValueError):
            bootstrap.validate_state({'owner': 'postgres', 'role': None})
        with self.assertRaises(ValueError):
            bootstrap.validate_state({'owner': 'glitchtip', 'role': {'super': True}})

    def test_apply_existing_database_does_not_change_password(self):
        current = {'owner': 'glitchtip', 'role': dict(super=False, createdb=False, createrole=False,
            replication=False, bypassrls=False, login=True, connections=12)}
        with patch.object(bootstrap, 'state', return_value=current), \
             patch.object(bootstrap, 'credentials', return_value={'database_password':'fake-test-value', 'secret_key':'fake-test-value'}), \
             patch.object(bootstrap, 'sql') as sql, patch.object(bootstrap.subprocess, 'run'):
            bootstrap.database()
            self.assertFalse(any('PASSWORD' in call.args[0] for call in sql.call_args_list))

    def test_oidc_keeps_signup_and_user_privileges_closed(self):
        config=json.loads(Path(__file__).with_name('oidc.json').read_text())
        self.assertEqual(config['client_id'],'observability')
        self.assertEqual(config['organization'],'updspace')
        self.assertTrue(config['settings']['fetch_userinfo'])
        self.assertTrue(config['settings']['oauth_pkce_enabled'])
        objects=list(yaml.safe_load_all(Path(__file__).with_name('resources.yaml').read_text()))
        cm=next(x for x in objects if x['kind']=='ConfigMap')
        self.assertEqual(cm['data']['ENABLE_USER_REGISTRATION'],'False')
        self.assertEqual(cm['data']['ENABLE_SOCIAL_APPS_USER_REGISTRATION'],'False')
        deploy=next(x for x in objects if x['kind']=='Deployment')
        self.assertEqual(deploy['spec']['template']['spec']['hostAliases'],
            [{'ip':'192.168.1.176','hostnames':['id.updspace.com']}])

    def test_manifest_keeps_small_persistent_isolated_runtime(self):
        objects = list(yaml.safe_load_all(Path(__file__).with_name('resources.yaml').read_text()))
        self.assertFalse(any(o['kind'] == 'Secret' for o in objects))
        deploy = next(o for o in objects if o['kind'] == 'Deployment')
        spec = deploy['spec']['template']['spec']
        self.assertEqual(deploy['spec']['strategy']['type'], 'Recreate')
        self.assertFalse(spec['automountServiceAccountToken'])
        container = spec['containers'][0]
        self.assertIn('@sha256:', container['image'])
        self.assertEqual(container['resources']['limits']['memory'], '1Gi')
        pv = next(o for o in objects if o['kind'] == 'PersistentVolume')
        self.assertEqual(pv['spec']['persistentVolumeReclaimPolicy'], 'Retain')
        self.assertEqual(next(o for o in objects if o['kind'] == 'Service')['spec'].get('type', 'ClusterIP'), 'ClusterIP')


if __name__ == '__main__':
    unittest.main()
