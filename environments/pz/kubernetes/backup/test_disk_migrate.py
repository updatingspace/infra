import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location('disk_migrate', Path(__file__).with_name('disk-migrate.py'))
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)

IDENTITY = 'fv4123456789012345678'
UUID = '11111111-2222-3333-4444-555555555555'


class FstabTests(unittest.TestCase):
    def original(self):
        return ('# untouched host\nUUID=root / ext4 defaults 0 1\n' + disk.BEGIN + '\n'
                '/var/lib/pz-volumes/edge.ext4 /srv/pz-storage/edge ext4 loop,nodev,nosuid,noatime 0 0\n'
                '/var/lib/pz-volumes/zomboid.ext4 /srv/pz-storage/zomboid ext4 loop,nodev,nosuid,noatime 0 0\n'
                + disk.END + '\n# end host\n')

    def test_changes_only_exact_world_entry(self):
        old = self.original()
        new = disk.replacement_fstab(old, UUID)
        self.assertIn(f'UUID={UUID} /srv/pz-storage/zomboid ext4 nodev,nosuid,noatime 0 2', new)
        self.assertEqual([x for x in old.splitlines() if '/zomboid' not in x],
                         [x for x in new.splitlines() if '/zomboid' not in x])
        self.assertNotIn('nofail', new)

    def test_rejects_unmanaged_or_duplicate_world_entry(self):
        for old in [self.original() + '/dev/vdb /srv/pz-storage/zomboid ext4 defaults 0 2\n',
                    self.original().replace(disk.END, self.original()),
                    self.original().replace('/var/lib/pz-volumes/zomboid.ext4', 'UUID=existing')]:
            with self.subTest(old=old), self.assertRaises(disk.backup.Refused):
                disk.replacement_fstab(old, UUID)

    def test_rejects_fstab_injection(self):
        with self.assertRaises(disk.backup.Refused):
            disk.replacement_fstab(self.original(), UUID + '\n/dev/vda /bad ext4 defaults')


class InitializeTests(unittest.TestCase):
    def test_requires_exact_repeated_disk_id_before_any_commands(self):
        with patch.object(disk, 'block_identity') as identity:
            with self.assertRaises(disk.backup.Refused):
                disk.initialize(IDENTITY, 40, 'pz-data', 'another-disk')
            identity.assert_not_called()

    def test_refuses_existing_filesystem_or_mount(self):
        for row in [{'fstype': 'ext4', 'mountpoints': []}, {'fstype': None, 'mountpoints': ['/existing']}]:
            with self.subTest(row=row), patch.object(disk, 'block_identity', return_value=(Path('/dev/fake'), row)), \
                    patch.object(disk, 'command') as run:
                with self.assertRaises(disk.backup.Refused):
                    disk.initialize(IDENTITY, 40, 'pz-data', IDENTITY)
                run.assert_not_called()

    def test_refuses_signature_without_mkfs(self):
        with patch.object(disk, 'block_identity', return_value=(Path('/dev/fake'), {'fstype': None, 'mountpoints': []})), \
                patch.object(disk, 'command', return_value=b'{"signatures":[{"type":"gpt"}]}') as run:
            with self.assertRaises(disk.backup.Refused):
                disk.initialize(IDENTITY, 40, 'pz-data', IDENTITY)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], 'wipefs')
            self.assertIn('--no-act', run.call_args.args[0])

    def test_formats_only_blank_explicit_disk_without_force(self):
        with patch.object(disk, 'block_identity', return_value=(Path('/dev/fake'), {'fstype': None, 'mountpoints': []})), \
                patch.object(disk, 'command', side_effect=[b'{"signatures":[]}', b'',
                    f'TYPE=ext4\nLABEL=pz-data\nUUID={UUID}\n'.encode()]) as run:
            result = disk.initialize(IDENTITY, 40, 'pz-data', IDENTITY)
            self.assertEqual(result['uuid'], UUID)
            call = run.call_args_list[1].args[0]
            self.assertEqual(call[-1], '/dev/fake')
            self.assertNotIn('-F', call)
            self.assertEqual(call[0], 'mkfs.ext4')


class SpoolTests(unittest.TestCase):
    def test_fstab_is_persistent_idempotent_and_preserves_host_entries(self):
        original = 'UUID=root / ext4 defaults 0 1\n'
        updated = disk.spool_fstab(original, UUID)
        self.assertTrue(updated.startswith(original))
        self.assertIn(f'UUID={UUID} {disk.SPOOL} ext4 nodev,nosuid,noatime 0 2', updated)
        self.assertNotIn('nofail', updated)
        self.assertEqual(disk.spool_fstab(updated, UUID), updated)

    def test_mismatched_existing_spool_entry_is_never_replaced(self):
        for original in [f'/dev/vdb {disk.SPOOL} ext4 defaults 0 2\n',
                         disk.spool_fstab('', UUID) * 2,
                         disk.SPOOL_BEGIN + '\n' + disk.SPOOL_END]:
            with self.subTest(original=original), self.assertRaises(disk.backup.Refused):
                disk.spool_fstab(original, UUID)

    def test_mount_refuses_uninitialized_or_elsewhere_without_mutation(self):
        for row in [{'fstype': None, 'mountpoints': []}, {'fstype': 'ext4', 'mountpoints': ['/other']}]:
            with self.subTest(row=row), patch.object(disk, 'block_identity', return_value=(Path('/dev/fake'), row)), \
                    patch.object(disk, 'command') as run, patch.object(disk, 'atomic_text') as write:
                with self.assertRaises(disk.backup.Refused):
                    disk.mount_spool(IDENTITY, 64, UUID)
                run.assert_not_called()
                write.assert_not_called()

    def test_mount_refuses_wrong_uuid_without_fstab_change(self):
        with patch.object(disk, 'block_identity', return_value=(Path('/dev/fake'), {'fstype': 'ext4', 'mountpoints': []})), \
                patch.object(disk, 'command', return_value=b'TYPE=ext4\nLABEL=pz-backup-spool\nUUID=wrong\n') as run, \
                patch.object(disk, 'atomic_text') as write:
            with self.assertRaises(disk.backup.Refused):
                disk.mount_spool(IDENTITY, 64, UUID)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], 'blkid')
            write.assert_not_called()


class CopyTests(unittest.TestCase):
    def test_corrupted_destination_is_not_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source'
            target = Path(temporary) / 'target'
            source.mkdir()
            (source / 'world.db').write_bytes(b'original')
            shutil.copytree(source, target, copy_function=shutil.copy2)
            records = disk.backup.inventory(source)
            with patch.object(disk, 'DATA', source), patch.object(disk, 'NEW', target), \
                    patch.object(disk, 'source_identity'), patch.object(disk, 'mount_record'):
                disk.compare_copy(records, UUID, UUID)
                before = (target / 'world.db').stat()
                (target / 'world.db').write_bytes(b'corrupt!')
                os.utime(target / 'world.db', ns=(before.st_atime_ns, before.st_mtime_ns))
                with self.assertRaises(disk.backup.Refused):
                    disk.compare_copy(records, UUID, UUID)

    def test_changed_source_is_not_verified(self):
        with patch.object(disk, 'source_identity'), patch.object(disk, 'mount_record'), \
                patch.object(disk.backup, 'inventory', return_value=[{'changed': True}]):
            with self.assertRaises(disk.backup.Refused):
                disk.compare_copy([{'original': True}], UUID, UUID)


class WriterTests(unittest.TestCase):
    def test_rejects_terminating_pod_and_active_updater(self):
        api = Mock()
        api.get.return_value = {'spec': {'replicas': 0, 'suspend': True}}
        api.pods.return_value = [{'metadata': {'deletionTimestamp': 'now'}}]
        with self.assertRaises(disk.backup.Refused):
            disk.writers_stopped(api)
        api.pods.return_value = []
        api.updater_idle.return_value = False
        with self.assertRaises(disk.backup.Refused):
            disk.writers_stopped(api)

    def test_stopped_writers_still_check_unknown_writers_and_journal(self):
        api = Mock()
        api.get.return_value = {'spec': {'replicas': 0, 'suspend': True}}
        api.pods.return_value = []
        api.updater_idle.return_value = True
        with patch.object(disk.backup, 'updater_journal') as journal:
            disk.writers_stopped(api)
            api.no_unknown_writers.assert_called_once()
            journal.assert_called_once()

    def test_collector_replicas_and_pods_must_be_zero(self):
        api = Mock()
        def get(kind, name, **kwargs):
            return {'spec': {'replicas': 1 if name == 'otel-collector' else 0, 'suspend': True}}
        api.get.side_effect = get
        api.pods.return_value = []
        api.updater_idle.return_value = True
        with self.assertRaisesRegex(disk.backup.Refused, 'collector_still_holds_data_mounts'):
            disk.writers_stopped(api)


class MountNamespaceTests(unittest.TestCase):
    def row(self, namespace='host', target=None, root='/', propagation=None):
        return {'namespace': namespace, 'pid': 1, 'root': root, 'target': str(disk.DATA) if target is None else target,
                'propagation': ['shared:71'] if propagation is None else propagation}

    def test_shared_host_mount_and_following_systemd_slaves_are_detachable(self):
        rows = [self.row(), self.row('systemd-resolved', propagation=['shared:79', 'master:71']),
                self.row('polkit', propagation=['master:71'])]
        self.assertTrue(disk.detachable_references(rows, 'host'))

    def test_collector_private_root_clone_and_log_bind_are_not_detachable(self):
        for row in [self.row('collector', '/hostfs/srv/pz-storage/zomboid', propagation=[]),
                    self.row('collector', '/var/log/pz', '/zomboid', []),
                    self.row('unknown', propagation=[])]:
            with self.subTest(row=row):
                self.assertFalse(disk.detachable_references([self.row(), row], 'host'))

    def test_kubelet_bind_must_be_released_after_pod_disappearance(self):
        rows = [self.row(), self.row(target='/var/lib/kubelet/pods/uid/volumes/pz-zomboid', root='/zomboid')]
        self.assertFalse(disk.detachable_references(rows, 'host'))

    def test_migration_never_unmounts_inside_a_private_service_namespace(self):
        with patch.object(disk.os, 'readlink', side_effect=['host', 'private']), patch.object(disk, 'command') as command:
            with self.assertRaisesRegex(disk.backup.Refused, 'host_mount_namespace'):
                disk.wait_mount_release()
        command.assert_not_called()

    def test_process_mountinfo_is_parsed_without_reporting_unrelated_mounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / '123/ns').mkdir(parents=True)
            (proc / '123/ns/mnt').symlink_to('mnt:[123]')
            (proc / '123/mountinfo').write_text(
                '82 29 7:2 / /srv/pz-storage/zomboid rw shared:71 - ext4 /dev/loop2 rw\n'
                '83 29 8:1 / / rw - ext4 /dev/vda1 rw\n')
            rows = disk.source_mount_references(os.makedev(7, 2), proc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['target'], str(disk.DATA))
        self.assertEqual(rows[0]['namespace'], 'mnt:[123]')


if __name__ == '__main__':
    unittest.main()
