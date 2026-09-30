#!/usr/bin/env python3
"""Failure-boundary tests; no cluster, host mutation, credentials or network."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('coordinator', Path(__file__).with_name('coordinator.py'))
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)


def original(game=1, panel=1, suspended=False):
    return {'zomboid': {'kind': 'statefulset', 'uid': 'game-uid', 'spec': {'replicas': game}},
            'panel': {'kind': 'deployment', 'uid': 'panel-uid', 'spec': {'replicas': panel}},
            'panel-auto-update': {'kind': 'cronjob', 'uid': 'updater-uid', 'spec': {'suspend': suspended}}}


class FakeKubernetes:
    def __init__(self, game=0, panel=0, suspended=True):
        self.current = original(game, panel, suspended)
        self.patches = []

    def get(self, kind, name, namespace='zomboid'):
        row = self.current[name]
        return {'metadata': {'uid': row['uid']}, 'spec': deepcopy(row['spec'])}

    def patch(self, kind, name, uid, field, previous, value, namespace='zomboid'):
        assert self.current[name]['uid'] == uid
        assert self.current[name]['spec'][field.split('/')[-1]] == previous
        self.current[name]['spec'][field.split('/')[-1]] = value
        self.patches.append((name, value))

    def updater_idle(self):
        return True


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hashes_hardlinks_modes_and_binary_xattrs(self):
        first = self.root / 'a'
        first.write_bytes(b'world database\0private')
        first.chmod(0o640)
        os.link(first, self.root / 'b')
        os.setxattr(first, 'user.test', b'\0\xffbinary')
        (self.root / 'link').symlink_to('a')
        records = {r['path']: r for r in c.inventory(self.root)}
        self.assertEqual(records['a']['sha256'], hashlib.sha256(first.read_bytes()).hexdigest())
        self.assertEqual(records['a']['mode'], 0o640)
        self.assertEqual(records['b']['hardlink'], 'a')
        self.assertEqual(records['link']['target'], 'a')
        self.assertIn('user.test', records['a']['xattrs'])

    def test_relative_escape_absolute_and_dangling_links_refused(self):
        for destination in ('../missing', '/etc/passwd', 'missing'):
            with self.subTest(destination=destination):
                link = self.root / 'bad'
                link.symlink_to(destination)
                with self.assertRaises(c.Refused):
                    c.inventory(self.root)
                link.unlink()

    def test_special_file_refused(self):
        os.mkfifo(self.root / 'pipe')
        with self.assertRaisesRegex(c.Refused, 'special_file'):
            c.inventory(self.root)

    def test_inventory_and_file_hash_stop_on_elapsed_staging_deadline(self):
        file = self.root / 'large'
        file.write_bytes(b'x' * (2 * 1024 * 1024))
        with patch.object(c.time, 'monotonic', return_value=200):
            with self.assertRaisesRegex(c.Refused, 'staging_deadline_exceeded'):
                c.inventory(self.root, deadline=100)
            with self.assertRaisesRegex(c.Refused, 'staging_deadline_exceeded'):
                c.file_hash(file, deadline=100)

    def test_manifest_serialization_honors_staging_deadline(self):
        output = self.root / 'manifest.json'
        with patch.object(c.time, 'monotonic', return_value=200):
            with self.assertRaisesRegex(c.Refused, 'staging_deadline_exceeded'):
                c.atomic_json(output, {'files': ['a']}, deadline=100)
        self.assertFalse(output.exists())

    def test_narrow_explicit_exclusion_and_no_automatic_exclusions(self):
        (self.root / 'backups').mkdir()
        (self.root / 'backups/old.zip').write_bytes(b'previous')
        self.assertIn('backups/old.zip', [r['path'] for r in c.inventory(self.root)])
        self.assertEqual([r['path'] for r in c.inventory(self.root, excluded=['backups'])], ['.'])

    def test_budget_covers_staging_plus_incompressible_archive(self):
        size = 10 * 1024 ** 3
        logical, budget = c.estimate([{'path': 'large', 'size': size}])
        self.assertEqual(logical, size)
        self.assertGreater(budget, size * 2)

    def test_free_blocks_and_inodes_are_both_required(self):
        cfg = {'min_free_bytes': 100, 'min_free_inodes': 10}
        for free_bytes, free_inodes in ((150, 100), (1000, 15)):
            with self.subTest(free_bytes=free_bytes, free_inodes=free_inodes):
                with patch.object(c.os, 'statvfs', return_value=SimpleNamespace(f_bavail=free_bytes, f_frsize=1,
                                                                             f_favail=free_inodes)):
                    with self.assertRaises(c.Refused):
                        c.free_space(self.root, 100, 10, cfg)

    def test_atomic_json_reads_back_bytes_and_is_private(self):
        output = self.root / 'journal.json'
        c.atomic_json(output, {'phase': 'staging_verified', 'data': ['a', 'b']})
        self.assertEqual(c.read_json(output)['phase'], 'staging_verified')
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_mount_mismatch_refuses_before_data_changes(self):
        mount = {'filesystems': [{'target': str(self.root), 'uuid': 'wrong', 'fstype': 'ext4'}]}
        with patch.object(c, 'command', return_value=json.dumps(mount).encode()):
            with self.assertRaisesRegex(c.Refused, 'mount_identity_mismatch'):
                c.check_mount(self.root, 'expected')

    @unittest.skipUnless(shutil.which('tar'), 'GNU tar required')
    def test_real_tar_full_content_metadata_hardlink_and_xattr_verification(self):
        (self.root / 'data').mkdir()
        (self.root / 'recovery').mkdir()
        first = self.root / 'data/a'
        first.write_bytes(b'important persistent bytes')
        os.setxattr(first, 'user.binary', b'\0\xffvalue')
        os.link(first, self.root / 'data/b')
        (self.root / 'data/link').symlink_to('a')
        records = []
        for prefix in ('data', 'recovery'):
            for row in c.inventory(self.root / prefix):
                row['path'] = prefix if row['path'] == '.' else prefix + '/' + row['path']
                if 'hardlink' in row:
                    row['hardlink'] = prefix + '/' + row['hardlink']
                records.append(row)
        manifest = json.dumps({'files': records}).encode()
        (self.root / 'manifest.json').write_bytes(manifest)
        archive = subprocess.check_output(['tar', '--format=pax', '--sort=name', '--acls', '--xattrs',
                                           '--xattrs-include=*', '--pax-option=delete=atime,delete=ctime',
                                           '--numeric-owner', '-C', str(self.root), '-cf', '-', 'data', 'recovery', 'manifest.json'])
        c.verify_tar(io.BytesIO(archive), records, manifest)
        corrupted = archive.replace(b'important persistent bytes', b'corrupted persistent bytes', 1)
        with self.assertRaisesRegex(c.Refused, 'archive_sha256_mismatch'):
            c.verify_tar(io.BytesIO(corrupted), records, manifest)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {'data_uuid': 'data', 'spool_uuid': 'spool'}

    def tearDown(self):
        self.tmp.cleanup()

    def coordinator(self, api=None):
        value = c.Coordinator(self.cfg, api or FakeKubernetes())
        value.journal_path = self.root / 'journal.json'
        value.journal = {'snapshot_id': '20260930T010203Z-' + 'a' * 32, 'original': original()}
        return value

    def test_intentionally_stopped_game_and_suspended_updater_remain_stopped(self):
        api = FakeKubernetes()
        coordinator = self.coordinator(api)
        coordinator.journal['original'] = original(game=0, panel=1, suspended=True)
        with patch.object(c, 'updater_journal'):
            coordinator.restore_apps()
        self.assertEqual(api.patches, [('panel', 1)])
        self.assertEqual(coordinator.journal['phase'], 'apps_restored')

    def test_collector_original_replicas_and_uid_preserved_during_restore(self):
        api = FakeKubernetes()
        api.current['otel-collector'] = {'kind': 'deployment', 'namespace': 'observability',
                                         'uid': 'collector-uid', 'spec': {'replicas': 0}}
        coordinator = self.coordinator(api)
        coordinator.journal['original']['otel-collector'] = deepcopy(api.current['otel-collector'])
        coordinator.journal['original']['otel-collector']['spec']['replicas'] = 1
        with patch.object(c, 'updater_journal'):
            coordinator.restore_apps()
        self.assertEqual(api.patches, [('zomboid', 1), ('panel', 1), ('otel-collector', 1), ('panel-auto-update', False)])

    def test_preparation_failure_restores_collector_without_touching_game(self):
        api = FakeKubernetes(game=1, panel=1)
        api.current['otel-collector'] = {'kind': 'deployment', 'namespace': 'observability',
                                         'uid': 'collector-uid', 'spec': {'replicas': 0}}
        coordinator = self.coordinator(api)
        coordinator.journal['original']['otel-collector'] = {**api.current['otel-collector'], 'spec': {'replicas': 1}}
        with patch.object(c, 'updater_journal'):
            coordinator.restore_preparation()
        self.assertEqual(api.patches, [('otel-collector', 1), ('panel-auto-update', False)])

    def test_restore_replicas_before_reenabling_schedule(self):
        api = FakeKubernetes()
        coordinator = self.coordinator(api)
        with patch.object(c, 'updater_journal'):
            coordinator.restore_apps()
        self.assertEqual(api.patches, [('zomboid', 1), ('panel', 1), ('panel-auto-update', False)])

    def test_ambiguous_reboot_never_starts_game_or_mutates_cluster(self):
        for phase in ('updater_suspending', 'writers_stopping'):
            with self.subTest(phase=phase):
                api = FakeKubernetes()
                coordinator = self.coordinator(api)
                c.atomic_json(coordinator.journal_path, {**coordinator.journal, 'phase': phase})
                with self.assertRaisesRegex(c.Refused, 'operator_inspection'):
                    coordinator.recover()
                self.assertEqual(api.patches, [])

    def test_preparation_failure_restores_only_updater_schedule(self):
        api = FakeKubernetes(game=1, panel=1, suspended=True)
        coordinator = self.coordinator(api)
        c.atomic_json(coordinator.journal_path, {**coordinator.journal, 'phase': 'preparing_recovery'})
        with patch.object(c, 'check_mount'), patch.object(c, 'updater_journal'):
            coordinator.recover()
        self.assertEqual(api.patches, [('panel-auto-update', False)])
        self.assertEqual(coordinator.journal['phase'], 'preparation_failed')

    def test_copy_failure_with_clean_exit_restores_apps_without_marking_snapshot_consistent(self):
        coordinator = self.coordinator()
        c.atomic_json(coordinator.journal_path, {**coordinator.journal, 'phase': 'staging',
            'exit_evidence': {'exit_code': 0, 'runtime_child_exit': 0, 'cgroup_empty': True}})
        with patch.object(c, 'check_mount'), patch.object(c, 'updater_journal'), patch.object(coordinator, 'archive') as archive:
            coordinator.recover()
        archive.assert_not_called()
        self.assertEqual(coordinator.journal['phase'], 'capture_failed')
        self.assertEqual(coordinator.api.patches, [('zomboid', 1), ('panel', 1), ('panel-auto-update', False)])

    def test_copy_failure_without_clean_exit_proof_never_restarts_game(self):
        coordinator = self.coordinator()
        c.atomic_json(coordinator.journal_path, {**coordinator.journal, 'phase': 'staging'})
        with patch.object(c, 'check_mount'):
            with self.assertRaisesRegex(c.Refused, 'no_clean_exit_proof'):
                coordinator.recover()
        self.assertEqual(coordinator.api.patches, [])

    def test_incomplete_disk_migration_blocks_coordinator_commands(self):
        migration = self.root / 'disk-migration'
        migration.mkdir()
        with patch.object(c, 'STATE', self.root):
            c.disk_migration_ready()
            c.atomic_json(migration / 'journal.json', {'phase': 'fstab_switching'})
            with self.assertRaisesRegex(c.Refused, 'disk_migration_incomplete'):
                c.disk_migration_ready()
            c.atomic_json(migration / 'journal.json', {'phase': 'complete'})
            c.disk_migration_ready()

    def test_failed_preflight_does_not_touch_cluster_or_create_journal(self):
        api = FakeKubernetes()
        coordinator = self.coordinator(api)
        with patch.object(coordinator, 'preflight', side_effect=c.Refused('insufficient_free_bytes')):
            with self.assertRaisesRegex(c.Refused, 'insufficient_free_bytes'):
                coordinator.run()
        self.assertEqual(api.patches, [])
        self.assertFalse(coordinator.journal_path.exists())

    def test_previous_incomplete_run_is_never_replaced(self):
        coordinator = self.coordinator()
        c.atomic_json(coordinator.journal_path, {**coordinator.journal, 'phase': 'archiving'})
        previous = coordinator.journal_path.read_bytes()
        with self.assertRaisesRegex(c.Refused, 'unfinished_backup'):
            coordinator.run()
        self.assertEqual(coordinator.journal_path.read_bytes(), previous)

    def test_archive_retry_never_creates_third_copy_beside_completed_payload(self):
        coordinator = self.coordinator()
        partial = self.root / 'partial'
        partial.mkdir()
        (partial / 'payload.enc').write_bytes(b'existing encrypted payload')
        with patch.object(c, 'encrypted_archive') as archive:
            with self.assertRaisesRegex(c.Refused, 'existing_ciphertext_requires_operator'):
                coordinator.archive(partial)
        archive.assert_not_called()
        self.assertEqual((partial / 'payload.enc').read_bytes(), b'existing encrypted payload')

    def test_phase_timings_do_not_reset_on_retry(self):
        coordinator = self.coordinator()
        coordinator.phase('staging')
        first = coordinator.journal['staging_started_at']
        coordinator.phase('staging')
        self.assertEqual(coordinator.journal['staging_started_at'], first)

    def test_changed_workload_uid_refuses_restart(self):
        api = FakeKubernetes()
        api.current['zomboid']['uid'] = 'recreated'
        coordinator = self.coordinator(api)
        with self.assertRaisesRegex(c.Refused, 'identity_changed'):
            coordinator.restore_apps()
        self.assertEqual(api.patches, [])

    def test_recovery_restores_verified_snapshot_even_when_no_new_backup_budget(self):
        coordinator = self.coordinator()
        c.atomic_json(coordinator.journal_path, {**coordinator.journal, 'phase': 'staging_verified'})
        partial = self.root / (coordinator.journal['snapshot_id'] + '.partial')
        partial.mkdir()
        with patch.object(c, 'SPOOL', self.root), patch.object(c, 'check_mount'), \
                patch.object(c, 'updater_journal'), patch.object(coordinator, 'archive') as archive, \
                patch.object(coordinator, 'preflight', side_effect=AssertionError('must not need another full budget')):
            coordinator.recover()
        archive.assert_called_once_with(partial)

    def test_failed_attempt_count_is_not_terminal_job(self):
        api = c.Kubernetes()
        api.get = Mock(return_value={'items': [{'metadata': {}, 'status': {'failed': 1, 'active': 1}}]})
        api.pods = Mock(return_value=[])
        self.assertFalse(api.updater_idle())
        api.get.return_value = {'items': [{'metadata': {}, 'status': {'failed': 1, 'conditions': [
            {'type': 'Failed', 'status': 'True'}]}}]}
        self.assertTrue(api.updater_idle())
        api.pods.return_value = [{'metadata': {'deletionTimestamp': 'now'}, 'status': {'phase': 'Failed'}}]
        self.assertFalse(api.updater_idle())

    def test_all_namespace_flag_belongs_to_kubectl_get_subcommand(self):
        api = c.Kubernetes()
        with patch.object(c, 'command', return_value=b'{"items": []}') as command:
            self.assertEqual(api.get('pvc', all_namespaces=True), {'items': []})
        command.assert_called_once_with([c.K3S, 'kubectl', 'get', 'pvc', '-A', '-o', 'json'])
        args = command.call_args.args[0]
        self.assertGreater(args.index('-A'), args.index('get'))

    def test_explicit_observability_namespace_keeps_workload_name(self):
        api = c.Kubernetes()
        with patch.object(c, 'command', return_value=b'{"spec": {"replicas": 1}}') as command:
            api.get('deployment', 'otel-collector', namespace='observability')
        command.assert_called_once_with([c.K3S, 'kubectl', 'get', 'deployment', '-n', 'observability',
                                         'otel-collector', '-o', 'json'])

    def exit_evidence(self, *, status=0, logs=None):
        evidence = object.__new__(c.ExitEvidence)
        evidence.container = 'a' * 64
        evidence.pid, evidence.pod_uid = 2423196, 'pod-uid'
        event = {'container_id': evidence.container, 'id': evidence.container,
                 'pid': evidence.pid, 'exited_at': {'seconds': 1790793846, 'nanos': 425329585}}
        if status != 'omitted':
            event['exit_status'] = status
        evidence.events = [event]
        evidence.invalid_matching_event = False
        evidence.log = io.BytesIO(logs if logs is not None else
            b'Shutdown requested: saving world and requesting graceful quit\nGame process exited with code 0')
        evidence.cgroup = self.root / 'absent-cgroup'
        return evidence

    def test_protobuf_omitted_exit_zero_requires_exact_typed_task_exit(self):
        evidence = self.exit_evidence(status='omitted')
        raw = evidence.events[0]
        evidence.events = []
        evidence.process = SimpleNamespace(stdout=[
            'timestamp k8s.io /tasks/exit ' + json.dumps(raw),
            'timestamp wrong /tasks/exit ' + json.dumps(raw),
            'timestamp k8s.io /tasks/exit-wrong ' + json.dumps(raw)])
        evidence.collect()
        self.assertEqual(evidence.events, [raw])
        with patch.object(c.time, 'sleep'), patch.object(c, 'STATE', self.root):
            self.assertEqual(evidence.verify()['exit_code'], 0)
        proof = c.read_json(self.root / ('container-exit-' + evidence.container + '.json'))
        self.assertEqual(proof['events'], [raw])
        self.assertTrue(proof['runtime_shutdown_requested'])
        self.assertTrue(proof['runtime_child_exit_zero'])
        self.assertTrue(proof['cgroup_empty'])
        self.assertNotIn('Shutdown requested', json.dumps(proof))

    def test_malformed_matching_exit_event_never_uses_zero_default_or_cri_fallback(self):
        for changes in ({'exit_status': None}, {'exit_status': '0'}, {'exit_status': False},
                        {'pid': '2423196'}, {'pid': 1}, {'exited_at': None},
                        {'exited_at': {'seconds': '1790793846'}},
                        {'exited_at': {'seconds': 1790793846, 'nanos': 1000000000}}):
            with self.subTest(changes=changes):
                evidence = self.exit_evidence(status='omitted')
                event = {**evidence.events[0], **changes}
                evidence.events = []
                evidence.process = SimpleNamespace(stdout=['timestamp k8s.io /tasks/exit ' + json.dumps(event)])
                evidence.collect()
                with patch.object(c.time, 'sleep'), patch.object(c, 'STATE', self.root), patch.object(c, 'command') as command:
                    with self.assertRaisesRegex(c.Refused, 'clean_container_exit_not_observed'):
                        evidence.verify()
                    command.assert_not_called()
                proof = c.read_json(self.root / ('container-exit-' + evidence.container + '.json'))
                self.assertTrue(proof['invalid_matching_event'])
                self.assertTrue(proof['runtime_child_exit_zero'])

    def test_stopped_container_without_runtime_exit_log_is_not_consistent(self):
        evidence = self.exit_evidence(logs=b'pod deleted but game did not confirm exit')
        with patch.object(c.time, 'sleep'), patch.object(c, 'STATE', self.root):
            with self.assertRaisesRegex(c.Refused, 'clean_game_process_exit_not_observed'):
                evidence.verify()
        proof = c.read_json(self.root / ('container-exit-' + evidence.container + '.json'))
        self.assertFalse(proof['runtime_child_exit_zero'])
        self.assertEqual(proof['events'][0]['exit_status'], 0)

    def test_exit_137_never_passes_consistency_gate(self):
        evidence = self.exit_evidence(status=137)
        with patch.object(c.time, 'sleep'), patch.object(c, 'STATE', self.root), patch.object(c, 'command') as command:
            with self.assertRaisesRegex(c.Refused, 'clean_container_exit_not_observed'):
                evidence.verify()
            command.assert_not_called()
        proof = c.read_json(self.root / ('container-exit-' + evidence.container + '.json'))
        self.assertEqual(proof['events'][0]['exit_status'], 137)
        self.assertTrue(proof['runtime_child_exit_zero'])

    def test_game_descendant_process_remaining_refuses_consistency(self):
        evidence = self.exit_evidence()
        evidence.cgroup = self.root / 'cgroup'
        (evidence.cgroup / 'nested').mkdir(parents=True)
        (evidence.cgroup / 'cgroup.procs').write_text('')
        (evidence.cgroup / 'nested/cgroup.procs').write_text('12345\n')
        with patch.object(c.time, 'sleep'), patch.object(c, 'STATE', self.root):
            with self.assertRaisesRegex(c.Refused, 'game_process_still_exists'):
                evidence.verify()
        proof = c.read_json(self.root / ('container-exit-' + evidence.container + '.json'))
        self.assertFalse(proof['cgroup_empty'])

    def test_stage_stopped_preserves_data_hashes_and_explicit_deadline(self):
        data, partial = self.root / 'data', self.root / 'snapshot.partial'
        data.mkdir()
        (data / 'world').write_bytes(b'world state')
        os.link(data / 'world', data / 'world-link')
        (partial / 'recovery').mkdir(parents=True)
        (partial / 'recovery/index.json').write_text('{}')
        coordinator = self.coordinator()
        coordinator.cfg.update(max_entries=100, max_snapshot_bytes=100000,
                               server_name='survival42', infra_revision='b' * 40)
        proof = {'exit_code': 0, 'runtime_child_exit': 0, 'cgroup_empty': True}
        calls = []
        def command(args, timeout=60):
            calls.append((args, timeout))
            if args[0] == 'rsync':
                subprocess.check_call(['cp', '-a', str(data), str(partial / 'data')])
            return b''
        with patch.object(c, 'DATA', data), patch.object(c, 'free_space'), patch.object(c, 'command', side_effect=command), patch.object(c.time, 'monotonic', return_value=100):
            coordinator.stage_stopped(partial, proof, deadline=250)
        self.assertEqual(coordinator.journal['phase'], 'staging_verified')
        manifest = c.read_json(partial / 'manifest.json')
        records = {row['path']: row for row in manifest['files']}
        self.assertEqual(records['data/world']['sha256'], hashlib.sha256(b'world state').hexdigest())
        self.assertEqual(records['data/world-link']['hardlink'], 'data/world')
        self.assertEqual(manifest['exit_evidence'], proof)
        self.assertEqual(calls[0][1], 150)
        self.assertEqual(calls[1][1], 150)

    def test_stage_stopped_elapsed_operator_deadline_cannot_reset_window(self):
        coordinator = self.coordinator()
        with patch.object(c.time, 'monotonic', return_value=200), patch.object(c, 'command') as command:
            with self.assertRaisesRegex(c.Refused, 'staging_deadline_exceeded'):
                coordinator.stage_stopped(self.root, {'already_stopped': True}, deadline=100)
            command.assert_not_called()
        self.assertEqual(coordinator.journal['phase'], 'writers_stopped')

    def test_other_namespace_hostpath_writer_is_rejected(self):
        api = c.Kubernetes()
        api.get = Mock(side_effect=[{'items': []}, {'items': [{
            'metadata': {'namespace': 'other', 'labels': {'app.kubernetes.io/name': 'zomboid'}},
            'status': {'phase': 'Running'}, 'spec': {'volumes': [{'name': 'data', 'hostPath': {'path': '/srv'}}],
                'containers': [{'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}]}}]}])
        with self.assertRaisesRegex(c.Refused, 'unknown_data_writer'):
            api.no_unknown_writers()

    def test_terminal_pod_still_terminating_is_checked_as_writer(self):
        api = c.Kubernetes()
        api.get = Mock(side_effect=[{'items': []}, {'items': [{
            'metadata': {'namespace': 'other', 'deletionTimestamp': 'now'},
            'status': {'phase': 'Succeeded'}, 'spec': {'volumes': [{'name': 'data', 'hostPath': {'path': str(c.DATA)}}],
                'containers': [{'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}]}}]}])
        with self.assertRaisesRegex(c.Refused, 'unknown_data_writer'):
            api.no_unknown_writers()

    def test_readonly_collector_mount_does_not_block_backup(self):
        api = c.Kubernetes()
        api.get = Mock(side_effect=[{'items': []}, {'items': [{
            'metadata': {'namespace': 'observability', 'labels': {'app.kubernetes.io/name': 'otel-collector'}}, 'status': {'phase': 'Running'},
            'spec': {'volumes': [{'name': 'hostfs', 'hostPath': {'path': '/'}}],
                     'containers': [{'volumeMounts': [{'name': 'hostfs', 'mountPath': '/hostfs', 'readOnly': True}]}]}}]}])
        api.no_unknown_writers()

    def test_unknown_readonly_ancestor_hostpath_is_potential_nested_mount_writer(self):
        api = c.Kubernetes()
        api.get = Mock(side_effect=[{'items': []}, {'items': [{
            'metadata': {'namespace': 'other'}, 'status': {'phase': 'Running'},
            'spec': {'volumes': [{'name': 'hostfs', 'hostPath': {'path': '/'}}],
                     'containers': [{'volumeMounts': [{'name': 'hostfs', 'mountPath': '/hostfs', 'readOnly': True}]}]}}]}])
        with self.assertRaisesRegex(c.Refused, 'unknown_data_writer'):
            api.no_unknown_writers()


if __name__ == '__main__':
    unittest.main()
