#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('backup_install', Path(__file__).with_name('install.py'))
i = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(i)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'srv/pz-backup-spool').mkdir(parents=True)
        self.source = Path(self.tmp.name) / 'source'
        self.source.mkdir()
        for name in i.SCRIPTS + i.AUXILIARY:
            shutil.copyfile(Path(__file__).with_name(name), self.source / name)
        shutil.copytree(Path(__file__).with_name('systemd'), self.source / 'systemd')
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, args, allowed=(0,)):
        self.calls.append(args)
        status = b'disabled\n' if 'is-enabled' in args else b'inactive\n' if 'is-active' in args else b''
        return SimpleNamespace(stdout=status, returncode=0)

    def install(self, settings=None, **kwargs):
        with patch.object(i.os, 'chown'), patch.object(i.os, 'fchown'):
            return i.install(self.source, settings, root=self.root, root_uid=12345, runner=kwargs.get('runner', self.runner),
                             identity_provider=lambda: {'gid': 23456, 'pz-backup-upload': 23457, 'pz-backup-retain': 23458},
                             mount_check=kwargs.get('mount_check', lambda _: True))

    def test_installs_only_allowlisted_code_without_enabling_starting_or_credentials(self):
        (self.source / 'private.credentials.json').write_text('NEVER COPY')
        (self.source / 'test_secret.py').write_text('NEVER COPY')
        result = self.install()
        installed = self.root / 'opt/pz-backup'
        self.assertEqual(set(p.name for p in installed.iterdir()), set(i.SCRIPTS + i.AUXILIARY))
        self.assertFalse(any(command[1] in {'start', 'enable', 'restart', 'enable-now'} for command in self.calls))
        self.assertEqual(self.calls[-1], ['systemctl', 'daemon-reload'])
        self.assertFalse(result['timers_enabled'])
        self.assertFalse(result['credentials_created'])
        self.assertEqual(list((self.root / 'etc/pz-backup').iterdir()), [])

    def test_plaintext_staging_root_is_not_group_readable_and_spool_is_not_writable(self):
        self.install()
        self.assertEqual((self.root / 'var/lib/pz-backup').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'srv/pz-backup-spool').stat().st_mode & 0o777, 0o750)
        self.assertEqual((self.root / 'var/lib/pz-backup-remote/remote.lock').stat().st_mode & 0o777, 0o660)

    def test_active_unit_blocks_before_any_code_or_account_changes(self):
        def active(args, allowed=(0,)):
            return SimpleNamespace(stdout=b'active' if 'is-active' in args else b'disabled', returncode=0)
        with self.assertRaisesRegex(i.Refused, 'active_or_failed_unit'):
            self.install(runner=active)
        self.assertFalse((self.root / 'opt/pz-backup').exists())

    def test_enabled_unit_and_enable_flag_both_block_install(self):
        with self.assertRaisesRegex(i.Refused, 'enabled_or_linked_unit'):
            self.install(runner=lambda args, allowed=(0,): SimpleNamespace(stdout=b'enabled', returncode=0))
        (self.root / 'etc/pz-backup').mkdir(parents=True)
        (self.root / 'etc/pz-backup/enabled').touch()
        with self.assertRaisesRegex(i.Refused, 'enabled_backup_configuration'):
            self.install()

    def test_unmounted_spool_blocks_before_installing(self):
        with self.assertRaisesRegex(i.Refused, 'mounted_spool_required'):
            self.install(mount_check=lambda _: False)
        self.assertFalse((self.root / 'opt').exists())

    def test_symlinked_source_or_destination_refused(self):
        (self.source / 'coordinator.py').unlink()
        (self.source / 'coordinator.py').symlink_to(Path(__file__).with_name('coordinator.py'))
        with self.assertRaisesRegex(i.Refused, 'required_source_missing'):
            self.install()
        (self.source / 'coordinator.py').unlink()
        shutil.copyfile(Path(__file__).with_name('coordinator.py'), self.source / 'coordinator.py')
        (self.root / 'opt').symlink_to(self.tmp.name)
        with self.assertRaisesRegex(i.Refused, 'destination_symlink'):
            self.install()

    def test_role_configs_private_and_changed_configs_require_known_previous_hash(self):
        cfg = {'bucket': 'private-backup', 'prefix': 'pz/production', 'endpoint_url': 'https://storage.yandexcloud.net',
               'lock_path': i.REMOTE_LOCK}
        self.install({'remote': cfg})
        target = self.root / 'etc/pz-backup/remote-upload.json'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        previous = hashlib.sha256(target.read_bytes()).hexdigest()
        changed = {**cfg, 'bucket': 'changed-backup'}
        with self.assertRaisesRegex(i.Refused, 'changed_settings_require_previous_sha256'):
            self.install({'remote': changed})
        self.assertEqual(json.loads(target.read_text())['bucket'], 'private-backup')
        self.install({'remote': changed, 'expected_existing_sha256': {
            'remote-upload.json': previous, 'remote-retain.json': previous, 'cleanup-remote.json': previous}})
        self.assertEqual(json.loads(target.read_text())['bucket'], 'changed-backup')

    def test_lock_inode_persists_across_reinstallation(self):
        self.install()
        lock = self.root / i.REMOTE_LOCK.lstrip('/')
        old = lock.stat().st_ino
        self.install()
        self.assertEqual(lock.stat().st_ino, old)

    def test_credentials_are_never_accepted_in_remote_settings(self):
        with self.assertRaisesRegex(i.Refused, 'credentials_require_separate_delivery'):
            self.install({'remote': {'bucket': 'private-backup', 'prefix': 'pz/production',
                'endpoint_url': 'https://storage.yandexcloud.net', 'lock_path': i.REMOTE_LOCK,
                'secret_access_key': 'not-a-real-secret'}})


if __name__ == '__main__':
    unittest.main()
