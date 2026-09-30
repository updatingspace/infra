import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location('disk_migrate', Path(__file__).with_name('disk-migrate.py'))
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)

IDENTITY = 'fv4123456789012345678'
UUID = '11111111-2222-3333-4444-555555555555'
READ_ONLY_SOURCE = {'fstype': 'ext4', 'options': 'ro,relatime', 'fs-options': 'ro'}


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


class ReadOnlySourceTests(unittest.TestCase):
    def test_requires_ext4_superblock_read_only_not_only_vfs_read_only(self):
        for changes in [{'fs-options': 'rw'}, {'fs-options': ''}, {'fs-options': 'ro,rw'},
                        {'fstype': 'xfs'}, {'options': 'rw'}]:
            with self.subTest(changes=changes), \
                    patch.object(disk, 'source_identity', return_value=READ_ONLY_SOURCE | changes):
                with self.assertRaisesRegex(disk.backup.Refused, 'source_not_read_only'):
                    disk.read_only_source(UUID)
        with patch.object(disk, 'source_identity', return_value=READ_ONLY_SOURCE):
            self.assertEqual(disk.read_only_source(UUID), READ_ONLY_SOURCE)


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
                    patch.object(disk, 'source_identity', return_value=READ_ONLY_SOURCE), \
                    patch.object(disk, 'mount_record'):
                with patch.object(disk.backup, 'file_hash', wraps=disk.backup.file_hash) as hash_file:
                    disk.compare_copy(records, UUID, UUID)
                self.assertEqual([call.args[0] for call in hash_file.call_args_list], [target / 'world.db'])
                before = (target / 'world.db').stat()
                (target / 'world.db').write_bytes(b'corrupt!')
                os.utime(target / 'world.db', ns=(before.st_atime_ns, before.st_mtime_ns))
                with self.assertRaises(disk.backup.Refused):
                    disk.compare_copy(records, UUID, UUID)

    def test_changed_source_metadata_is_not_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source'
            target = Path(temporary) / 'target'
            source.mkdir()
            original = source / 'world.db'
            original.write_bytes(b'original')
            shutil.copytree(source, target, copy_function=shutil.copy2)
            records = disk.backup.inventory(source)
            original.chmod(original.stat().st_mode ^ 0o100)
            with patch.object(disk, 'DATA', source), patch.object(disk, 'NEW', target), \
                    patch.object(disk, 'source_identity', return_value=READ_ONLY_SOURCE), \
                    patch.object(disk, 'mount_record'):
                with self.assertRaisesRegex(disk.backup.Refused, 'source_changed_during_disk_copy'):
                    disk.compare_copy(records, UUID, UUID)

    def test_loss_of_read_only_before_or_during_verification_is_rejected(self):
        writable = READ_ONLY_SOURCE | {'fs-options': 'rw'}
        for states in [[writable], [READ_ONLY_SOURCE, writable]]:
            with self.subTest(states=states), \
                    patch.object(disk, 'source_identity', side_effect=states), \
                    patch.object(disk, 'mount_record'), \
                    patch.object(disk.backup, 'inventory', return_value=[]) as inventory:
                with self.assertRaisesRegex(disk.backup.Refused, 'source_not_read_only'):
                    disk.compare_copy([], UUID, UUID)
                self.assertEqual(inventory.call_count, 0 if len(states) == 1 else 2)


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


class ResumeTests(unittest.TestCase):
    def arguments(self, **changes):
        values = dict(migration_timeout_seconds=1800, resume_after_abort=True,
                      resume_journal_sha256='a'*64, resume_boot_guard_sha256='absent',
                      resume_unit='pz-disk-migration.service', disk_id=IDENTITY, target_uuid=UUID)
        return SimpleNamespace(**(values | changes))

    def test_migration_budget_and_explicit_resume_evidence(self):
        for value in (1800, 3600):
            self.assertEqual(disk.migration_options(self.arguments(migration_timeout_seconds=value)), value)
        for value in (0, 3601, True, '3600'):
            with self.subTest(value=value), self.assertRaisesRegex(disk.backup.Refused, 'migration_timeout'):
                disk.migration_options(self.arguments(migration_timeout_seconds=value))
        for changed in ({'resume_after_abort': False}, {'resume_unit': None},
                        {'resume_journal_sha256': None}, {'resume_boot_guard_sha256': None},
                        {'resume_unit': 'unrelated.service'}, {'resume_journal_sha256': 'not-a-hash'}):
            with self.subTest(changed=changed), self.assertRaises(disk.backup.Refused):
                disk.migration_options(self.arguments(**changed))
        self.assertEqual(disk.migration_options(self.arguments(resume_after_abort=False,
            resume_unit=None, resume_journal_sha256=None, resume_boot_guard_sha256=None)), 1800)

    def test_regular_backup_budget_still_refuses_over_1800(self):
        cfg = {'data_root': str(disk.backup.DATA), 'spool': str(disk.backup.SPOOL),
               'data_uuid': UUID, 'spool_uuid': '22222222-2222-3333-4444-555555555555',
               'age_recipient': 'age1'+'a'*58, 'infra_revision': 'a'*40, 'server_name': 'test',
               'min_free_bytes': 1, 'min_free_inodes': 1, 'max_snapshot_bytes': 1,
               'max_entries': 1, 'uploader_gid': 1, 'stop_timeout_seconds': 300,
               'staging_timeout_seconds': 3600}
        with patch.object(disk.backup, 'trusted'), patch.object(disk.backup, 'read_json', return_value=cfg):
            with self.assertRaisesRegex(disk.backup.Refused, 'staging_timeout_must_fit'):
                disk.backup.configuration(Path('/config'))

    def test_exact_abort_evidence_and_boot_identity_required(self):
        previous = {'format': 'pz-disk-migration-v1', 'phase': 'aborted_before_cutover',
                    'operator_inspection_required': True, 'old_uuid': 'old', 'new_uuid': UUID,
                    'disk_id': IDENTITY, 'source_loop': '/dev/loop2',
                    'restore_snapshot_id': 'snapshot', 'restore_commit_sha256': 'commit'}
        attestation = {'snapshot_id': 'snapshot', 'commit_sha256': 'commit'}
        cases = [({}, {}, 'fstab', None), ({'phase': 'copying'}, {}, 'fstab', None),
                 ({'phase': 'cutover_verified'}, {}, 'fstab', None),
                 ({'operator_inspection_required': False}, {}, 'fstab', None),
                 ({'new_uuid': 'other'}, {}, 'fstab', None),
                 ({'disk_id': 'other'}, {}, 'fstab', None),
                 ({'source_loop': '/dev/loop9'}, {}, 'fstab', None),
                 ({'restore_commit_sha256': 'other'}, {}, 'fstab', None),
                 ({'boot_guard_before_sha256': 'a'*64}, {}, 'fstab', None),
                 ({}, {'resume_journal_sha256': '0'*64}, 'fstab', None),
                 ({}, {}, 'changed-fstab', None), ({}, {}, 'fstab', b'changed-guard')]
        for index, (mutation, arguments, fstab, guard) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp); raw = json.dumps(previous | mutation).encode()
                (state/'journal.json').write_bytes(raw); (state/'fstab.before').write_text('fstab')
                (state/'current.fstab').write_text(fstab)
                args = self.arguments(resume_journal_sha256=hashlib.sha256(raw).hexdigest())
                for key, value in arguments.items(): setattr(args, key, value)
                with patch.object(disk, 'STATE', state), patch.object(disk, 'FSTAB', state/'current.fstab'), \
                        patch.object(disk.backup, 'trusted'), patch.object(disk, 'source_identity', return_value={
                            'source': '/dev/loop2', 'options': 'rw', 'fs-options': 'rw'}), \
                        patch.object(disk, 'resume_unit_stopped') as stopped, \
                        patch.object(disk, 'wait_mount_release') as release:
                    if index == 0:
                        self.assertEqual(disk.resume_evidence(args, {'data_uuid': 'old'}, attestation, 'fstab', guard), raw)
                        stopped.assert_called_once_with(args.resume_unit)
                        release.assert_called_once_with(path=disk.NEW)
                    else:
                        with self.assertRaises(disk.backup.Refused):
                            disk.resume_evidence(args, {'data_uuid': 'old'}, attestation, 'fstab', guard)
                        release.assert_not_called()
                self.assertEqual((state/'journal.json').read_bytes(), raw)

    def test_abort_journal_preserved_byte_for_byte_without_rewriting_original(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(disk, 'STATE', Path(tmp)), \
                patch.object(disk.backup, 'trusted'):
            raw = b'{ "phase": "aborted_before_cutover" }\n'
            current = Path(tmp)/'journal.json'; current.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            archived = disk.preserve_abort(raw, digest)
            self.assertEqual(current.read_bytes(), raw)
            self.assertEqual(archived.read_bytes(), raw)
            self.assertEqual(archived.stat().st_mode & 0o777, 0o600)
            self.assertEqual(disk.preserve_abort(raw, digest), archived)
            archived.write_bytes(b'changed')
            with self.assertRaisesRegex(disk.backup.Refused, 'archived_abort_evidence_changed'):
                disk.preserve_abort(raw, digest)

    def test_previous_unit_cgroup_and_lingering_rsync_are_checked(self):
        base = {'LoadState': 'loaded', 'ActiveState': 'failed', 'MainPID': '0', 'ControlPID': '0',
                'ControlGroup': '/old-unit'}
        for change, populated, rsync in [({}, '0', False), ({'ActiveState': 'active'}, '0', False),
                ({'MainPID': '42'}, '0', False), ({'LoadState': 'not-found'}, '0', False),
                ({}, '1', False), ({}, '0', True)]:
            with self.subTest(change=change, populated=populated, rsync=rsync), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); proc = root/'proc'; proc.mkdir(); group = root/'old-unit'; group.mkdir()
                (group/'cgroup.events').write_text('populated '+populated+'\n')
                if rsync:
                    (proc/'123').mkdir(); (proc/'123/exe').symlink_to('/usr/bin/rsync')
                output = '\n'.join(k+'='+v for k,v in (base | change).items()).encode()
                with patch.object(disk, 'command', return_value=output):
                    if not change and populated == '0' and not rsync:
                        disk.resume_unit_stopped('pz-disk-migration.service', proc, root)
                    else:
                        with self.assertRaises(disk.backup.Refused):
                            disk.resume_unit_stopped('pz-disk-migration.service', proc, root)

    def test_cache_budget_reserves_all_potential_writes_without_stale_credit(self):
        records = [{'path': 'same', 'type': 'file', 'size': 100, 'mtime_ns': 1},
                   {'path': 'changed', 'type': 'file', 'size': 200, 'mtime_ns': 2},
                   {'path': 'missing', 'type': 'file', 'size': 300, 'mtime_ns': 3}]
        target = [records[0], records[1] | {'mtime_ns': 1},
                  {'path': 'stale', 'type': 'file', 'size': 999999, 'mtime_ns': 1}]
        self.assertEqual(disk.resume_copy_budget(records, target), 500+3*8192)

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required for real partial-cache reconciliation fixture')
    def test_real_resume_reconciles_partial_and_stale_files_then_verifies_exact_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp)/'source', Path(tmp)/'target'
            source.mkdir(); target.mkdir(); (target/'lost+found').mkdir()
            (source/'world').write_bytes(b'fresh world')
            (source/'cached').write_bytes(b'cached bytes')
            shutil.copy2(source/'cached', target/'cached')
            (target/'world').write_bytes(b'partial')
            (target/'stale').write_bytes(b'old source generation')
            (target/'.world.old-rsync-temp').write_bytes(b'partial temp')
            records = disk.backup.inventory(source)
            with patch.object(disk, 'DATA', source), patch.object(disk, 'NEW', target), \
                    patch.object(disk, 'source_identity', return_value=READ_ONLY_SOURCE), \
                    patch.object(disk, 'mount_record'):
                disk.command(disk.rsync_arguments(True))
                self.assertTrue((target/'lost+found').is_dir())
                self.assertFalse((target/'stale').exists())
                self.assertFalse((target/'.world.old-rsync-temp').exists())
                disk.compare_copy(records, UUID, UUID)
                before = (target/'cached').stat()
                (target/'cached').write_bytes(b'CORRUPTbytes')
                os.utime(target/'cached', ns=(before.st_atime_ns, before.st_mtime_ns))
                disk.command(disk.rsync_arguments(True))
                with self.assertRaisesRegex(disk.backup.Refused, 'disk_copy_hash_or_metadata_mismatch'):
                    disk.compare_copy(records, UUID, UUID)
            self.assertEqual((source/'cached').read_bytes(), b'cached bytes')


if __name__ == '__main__':
    unittest.main()
