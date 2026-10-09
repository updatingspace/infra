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
    return {'zomboid': {'kind': 'statefulset', 'uid': 'game-uid',
                        'spec': {'replicas': game, 'template': {'spec': {'containers': [{'name': 'game'}]}}}},
            'panel': {'kind': 'deployment', 'uid': 'panel-uid', 'spec': {'replicas': panel}},
            'panel-auto-update': {'kind': 'cronjob', 'uid': 'updater-uid', 'spec': {'suspend': suspended}}}


class FakeKubernetes:
    def __init__(self, game=0, panel=0, suspended=True):
        self.current = original(game, panel, suspended)
        self.patches = []
        self.game_pod = {
            'metadata': {'name': 'zomboid-0', 'namespace': 'zomboid', 'uid': 'new-pod',
                         'ownerReferences': [{'kind': 'StatefulSet', 'name': 'zomboid',
                                              'uid': 'game-uid', 'controller': True}]},
            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'containerStatuses': [{'name': 'game', 'ready': True, 'state': {'running': {}}}]}}

    def get(self, kind, name=None, namespace='zomboid', timeout=60, selector=None):
        if kind == 'endpointslices':
            assert selector == 'kubernetes.io/service-name=zomboid'
            return {'items': [{'endpoints': [{'addresses': ['10.42.0.42'], 'conditions': {'ready': True},
                'targetRef': {'kind': 'Pod', 'namespace': 'zomboid', 'name': 'zomboid-0', 'uid': 'new-pod'}}]}]}
        row = self.current[name]
        return {'metadata': {'uid': row['uid']}, 'spec': deepcopy(row['spec'])}

    def patch(self, kind, name, uid, field, previous, value, namespace='zomboid'):
        assert self.current[name]['uid'] == uid
        assert self.current[name]['spec'][field.split('/')[-1]] == previous
        self.current[name]['spec'][field.split('/')[-1]] = value
        self.patches.append((name, value))

    def updater_idle(self):
        return True

    def pods(self, app, namespace='zomboid', timeout=60):
        assert app == 'zomboid'
        return [deepcopy(self.game_pod)] if self.current[app]['spec']['replicas'] else []


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
        data = patch.object(c, 'DATA', self.root / 'zomboid')
        data.start()
        self.addCleanup(data.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def coordinator(self, api=None):
        value = c.Coordinator(self.cfg, api or FakeKubernetes())
        value.journal_path = self.root / 'journal.json'
        value.journal = {'snapshot_id': '20260930T010203Z-' + 'a' * 32, 'original': original()}
        return value

    def test_changed_base_falls_back_to_full_snapshot_before_stopping_game(self):
        data = self.root / 'data'
        for component in c.BASE_COMPONENTS:
            path = data / component
            path.mkdir(parents=True)
            (path / 'artifact').write_bytes(component.encode())
        expected = {part: c.component_digest(data, part, max_entries=100)
                    for part in c.BASE_COMPONENTS}
        coordinator = self.coordinator()
        coordinator.cfg.update(base_snapshot={'snapshot_id': '20261004T030122Z-' + 'a' * 32,
                                              'commit_sha256': 'b' * 64, 'components': expected},
                               max_entries=100, excluded_paths=[*c.BASE_COMPONENTS, 'zomboid/backups'])
        with patch.object(c, 'DATA', data):
            self.assertTrue(coordinator.verify_base_components())
            (data / c.BASE_COMPONENTS[0] / 'artifact').write_bytes(b'different')
            self.assertFalse(coordinator.verify_base_components())
        self.assertIsNone(coordinator.active_base_snapshot)
        self.assertEqual(coordinator.active_exclusions, ['zomboid/backups'])

    def with_collector(self, coordinator, replicas=1):
        state = {'kind': 'deployment', 'namespace': 'observability',
                 'uid': 'collector-uid', 'spec': {'replicas': 0}}
        coordinator.api.current['otel-collector'] = deepcopy(state)
        coordinator.journal['original']['otel-collector'] = {**state, 'spec': {'replicas': replicas}}
        return coordinator

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
        with patch.object(c, 'updater_journal'), patch.object(coordinator, 'wait_restored_game') as wait:
            coordinator.restore_preparation()
        wait.assert_not_called()
        self.assertEqual(api.patches, [('otel-collector', 1), ('panel-auto-update', False)])

    def test_collector_waits_for_owned_ready_running_game_and_deadline_precedes_restart(self):
        coordinator = self.with_collector(self.coordinator())
        api = coordinator.api
        observed = []

        def pods(app, **kwargs):
            self.assertEqual(api.patches, [('zomboid', 1), ('panel', 1)])
            saved = c.read_json(coordinator.journal_path)['game_ready_wait']
            observed.append(saved['deadline_at'])
            pod = deepcopy(api.game_pod)
            if len(observed) == 1:
                pod['status']['conditions'][0]['status'] = 'False'
            return [pod]

        original_patch = api.patch

        def replica_patch(*args, **kwargs):
            self.assertIn('game_ready_wait', c.read_json(coordinator.journal_path))
            return original_patch(*args, **kwargs)

        with patch.object(c, 'updater_journal'), patch.object(api, 'pods', side_effect=pods), \
                patch.object(api, 'patch', side_effect=replica_patch), patch.object(c.time, 'sleep'):
            coordinator.restore_apps()
        self.assertEqual(len(set(observed)), 1)
        self.assertEqual(coordinator.journal['game_ready_wait']['result'], 'ready')
        self.assertEqual(coordinator.journal['game_ready_wait']['pod_uid'], 'new-pod')
        self.assertEqual(coordinator.journal['game_ready_wait']['endpoint_pod_uid'], 'new-pod')
        self.assertEqual(api.patches[-2:], [('otel-collector', 1), ('panel-auto-update', False)])

    def test_ready_pod_waits_for_service_endpoint_to_reference_same_uid(self):
        coordinator = self.with_collector(self.coordinator())
        api_get = coordinator.api.get
        calls = [0]

        def get(kind, *args, **kwargs):
            result = api_get(kind, *args, **kwargs)
            if kind == 'endpointslices':
                calls[0] += 1
                self.assertNotIn(('otel-collector', 1), coordinator.api.patches)
                if calls[0] == 1:
                    result['items'] = []
                elif calls[0] == 2:
                    result['items'][0]['endpoints'][0]['targetRef']['uid'] = 'previous-pod'
            return result

        with patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'get', side_effect=get), \
                patch.object(c.time, 'sleep'):
            coordinator.restore_apps()
        self.assertEqual(calls[0], 3)
        self.assertEqual(coordinator.journal['game_ready_wait']['result'], 'ready')

    def test_stopped_game_or_collector_has_no_readiness_wait(self):
        for stopped in ('zomboid', 'otel-collector'):
            with self.subTest(stopped=stopped):
                coordinator = self.with_collector(self.coordinator())
                coordinator.journal['original'][stopped]['spec']['replicas'] = 0
                with patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'pods') as pods:
                    coordinator.restore_apps()
                pods.assert_not_called()
                self.assertNotIn('game_ready_wait', coordinator.journal)

    def test_recovery_expired_ready_deadline_restores_collection_and_archives_without_new_wait(self):
        coordinator = self.with_collector(self.coordinator())
        wait = {'started_at': '2026-09-30T01:00:00+00:00',
                'deadline_at': '2026-09-30T01:15:00+00:00', 'result': 'pending', 'pod_uid': 'new-pod'}
        c.atomic_json(coordinator.journal_path,
                      {**coordinator.journal, 'phase': 'apps_restoring', 'game_ready_wait': wait})
        partial = self.root / (coordinator.journal['snapshot_id'] + '.partial')
        partial.mkdir()
        with patch.object(c, 'SPOOL', self.root), patch.object(c, 'check_mount'), \
                patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'pods') as pods, \
                patch.object(c.time, 'sleep') as sleep, patch.object(coordinator, 'archive') as archive:
            coordinator.recover()
        pods.assert_not_called()
        sleep.assert_not_called()
        archive.assert_called_once_with(partial)
        self.assertEqual(coordinator.journal['game_ready_wait']['deadline_at'], wait['deadline_at'])
        self.assertEqual(coordinator.journal['game_ready_wait']['result'], 'timeout')
        self.assertEqual(coordinator.journal['phase'], 'apps_restored')
        self.assertEqual(coordinator.api.patches[-2:], [('otel-collector', 1), ('panel-auto-update', False)])

    def test_ready_condition_alone_or_ready_container_alone_never_suffices(self):
        for field in ('pod_condition', 'container_ready', 'container_running', 'container_names'):
            with self.subTest(field=field):
                coordinator = self.with_collector(self.coordinator())
                status = coordinator.api.game_pod['status']
                if field == 'pod_condition':
                    status['conditions'][0]['status'] = 'False'
                elif field == 'container_ready':
                    status['containerStatuses'][0]['ready'] = False
                elif field == 'container_running':
                    status['containerStatuses'][0]['state'] = {'terminated': {'exitCode': 0}}
                else:
                    status['containerStatuses'][0]['name'] = 'other'
                clock = [0.0]
                with patch.object(c, 'updater_journal'), \
                        patch.object(c.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(c.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, 901.0)):
                    coordinator.restore_apps()
                self.assertEqual(coordinator.journal['game_ready_wait']['result'], 'timeout')
                self.assertEqual(coordinator.api.patches[-2:], [('otel-collector', 1), ('panel-auto-update', False)])

    def test_ambiguous_foreign_or_replaced_pod_fails_after_restoring_collector(self):
        for variant in ('ambiguous', 'owner', 'terminating', 'namespace', 'replaced'):
            with self.subTest(variant=variant):
                coordinator = self.with_collector(self.coordinator())
                pod = coordinator.api.game_pod
                if variant == 'owner':
                    pod['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                elif variant == 'terminating':
                    pod['metadata']['deletionTimestamp'] = 'now'
                elif variant == 'namespace':
                    pod['metadata']['namespace'] = 'elsewhere'
                calls = [0]

                def pods(app, **kwargs):
                    calls[0] += 1
                    result = deepcopy(pod)
                    if variant == 'replaced':
                        result['metadata']['uid'] = 'new-pod' if calls[0] == 1 else 'replacement'
                        result['status']['conditions'][0]['status'] = 'False'
                    return [result, deepcopy(result)] if variant == 'ambiguous' else [result]

                with patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'pods', side_effect=pods), \
                        patch.object(c.time, 'sleep'), self.assertRaisesRegex(c.Refused, 'game_ready_pod_'):
                    coordinator.restore_apps()
                self.assertEqual(coordinator.journal['game_ready_wait']['result'], 'identity_failed')
                self.assertEqual(coordinator.api.patches[-2:], [('otel-collector', 1), ('panel-auto-update', False)])
                with patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'pods') as no_retry, \
                        self.assertRaisesRegex(c.Refused, 'requires_inspection'):
                    coordinator.restore_apps()
                no_retry.assert_not_called()

    def test_api_wait_failure_does_not_strand_collector_or_verified_archive(self):
        coordinator = self.with_collector(self.coordinator())
        partial = self.root / (coordinator.journal['snapshot_id'] + '.partial')
        partial.mkdir()
        c.atomic_json(coordinator.journal_path, {**coordinator.journal, 'phase': 'staging_verified'})
        with patch.object(c, 'SPOOL', self.root), patch.object(c, 'check_mount'), \
                patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'pods',
                    side_effect=c.Refused('command_unavailable_or_timed_out')), \
                patch.object(coordinator, 'archive') as archive:
            coordinator.recover()
        archive.assert_called_once_with(partial)
        self.assertEqual(coordinator.journal['game_ready_wait']['result'], 'api_failed')
        self.assertEqual(coordinator.api.patches[-2:], [('otel-collector', 1), ('panel-auto-update', False)])

    def test_interrupted_wait_restores_collector_and_updater_then_reraises(self):
        coordinator = self.with_collector(self.coordinator())
        with patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'pods', side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            coordinator.restore_apps()
        self.assertEqual(coordinator.journal['phase'], 'apps_restoring')
        self.assertEqual(coordinator.api.patches[-2:], [('otel-collector', 1), ('panel-auto-update', False)])

    def test_updater_safety_failure_still_restores_collector(self):
        coordinator = self.with_collector(self.coordinator())
        with patch.object(c, 'updater_journal', side_effect=c.Refused('updater_unresolved')), \
                self.assertRaisesRegex(c.Refused, 'updater_unresolved'):
            coordinator.restore_apps()
        self.assertEqual(coordinator.api.patches, [('zomboid', 1), ('panel', 1), ('otel-collector', 1)])

    def test_collector_restore_failure_still_attempts_safe_updater_restore(self):
        coordinator = self.with_collector(self.coordinator())
        coordinator.api.current['otel-collector']['uid'] = 'replacement'
        with patch.object(c, 'updater_journal'), self.assertRaisesRegex(c.Refused, 'workload_identity_changed'):
            coordinator.restore_apps()
        self.assertEqual(coordinator.api.patches, [('zomboid', 1), ('panel', 1), ('panel-auto-update', False)])
        self.assertEqual(coordinator.journal['phase'], 'apps_restoring')

    def test_resumed_wait_limits_each_api_call_to_remaining_original_budget(self):
        coordinator = self.with_collector(self.coordinator())
        coordinator.journal['game_ready_wait'] = {
            'started_at': '1970-01-01T00:16:40+00:00', 'deadline_at': '1970-01-01T00:31:40+00:00',
            'result': 'pending'}
        pod = deepcopy(coordinator.api.game_pod)
        pod['status']['conditions'][0]['status'] = 'False'
        clock = [0.0]
        with patch.object(c, 'updater_journal'), patch.object(c.time, 'time', return_value=1898.0), \
                patch.object(c.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(c.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(coordinator.api, 'pods', return_value=[pod]) as pods:
            coordinator.restore_apps()
        self.assertEqual([call.kwargs['timeout'] for call in pods.call_args_list], [2.0, 1.0])
        self.assertEqual(clock[0], 2.0)
        self.assertEqual(coordinator.journal['game_ready_wait']['result'], 'timeout')

    def test_invalid_saved_deadline_fails_without_wait_but_restores_collection(self):
        for end in ('invalid', '1970-01-01T00:31:41+00:00', '1970-01-01T00:31:40'):
            with self.subTest(end=end):
                coordinator = self.with_collector(self.coordinator())
                coordinator.journal['game_ready_wait'] = {
                    'started_at': '1970-01-01T00:16:40+00:00', 'deadline_at': end, 'result': 'pending'}
                with patch.object(c, 'updater_journal'), patch.object(coordinator.api, 'pods') as pods, \
                        self.assertRaisesRegex(c.Refused, 'game_ready_wait_deadline_invalid'):
                    coordinator.restore_apps()
                pods.assert_not_called()
                self.assertEqual(coordinator.api.patches[-2:], [('otel-collector', 1), ('panel-auto-update', False)])

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
        command.assert_called_once_with([c.K3S, 'kubectl', 'get', 'pvc', '-A', '-o', 'json'], timeout=60)
        args = command.call_args.args[0]
        self.assertGreater(args.index('-A'), args.index('get'))

    def test_explicit_observability_namespace_keeps_workload_name(self):
        api = c.Kubernetes()
        with patch.object(c, 'command', return_value=b'{"spec": {"replicas": 1}}') as command:
            api.get('deployment', 'otel-collector', namespace='observability')
            command.assert_called_once_with([c.K3S, 'kubectl', 'get', 'deployment', '-n', 'observability',
                                             'otel-collector', '-o', 'json'], timeout=60)

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
        data, partial = self.root / 'zomboid', self.root / 'snapshot.partial'
        data.mkdir()
        (data / 'world').write_bytes(b'world state')
        os.link(data / 'world', data / 'world-link')
        os.setxattr(data / 'world', 'user.binary', b'\0\xff')
        (data / 'relative').symlink_to('world')
        (data / 'zomboid/backups').mkdir(parents=True)
        (data / 'zomboid/backups/old.zip').write_bytes(b'excluded')
        (data / 'zomboid/Saves').mkdir()
        (data / 'zomboid/Server').mkdir()
        (partial / 'recovery').mkdir(parents=True)
        (partial / 'recovery/index.json').write_text('{}')
        coordinator = self.coordinator()
        coordinator.cfg.update(max_entries=100, max_snapshot_bytes=100000,
                               server_name='survival42', infra_revision='b' * 40,
                               excluded_paths=['zomboid/backups'])
        coordinator.cfg['base_snapshot'] = {'snapshot_id': '20261004T030122Z-' + 'a' * 32,
                                            'commit_sha256': 'b' * 64, 'components': {}}
        coordinator.active_base_snapshot = None  # Mismatch already selected a full fallback.
        proof = {'exit_code': 0, 'runtime_child_exit': 0, 'cgroup_empty': True}
        calls = []
        def command(args, timeout=60):
            calls.append((args, timeout))
            return b''
        with patch.object(c, 'DATA', data), patch.object(c, 'free_space'), patch.object(c, 'command', side_effect=command), patch.object(c.time, 'monotonic', return_value=100):
            coordinator.stage_stopped(partial, proof, deadline=250)
        self.assertEqual(coordinator.journal['phase'], 'staging_verified')
        manifest = c.read_json(partial / 'manifest.json')
        records = {row['path']: row for row in manifest['files']}
        self.assertEqual(records['data/world']['sha256'], hashlib.sha256(b'world state').hexdigest())
        self.assertEqual(records['data/world-link']['hardlink'], 'data/world')
        self.assertEqual(manifest['exit_evidence'], proof)
        self.assertNotIn('snapshot_profile', manifest)
        self.assertNotIn('base_snapshot', manifest)
        self.assertEqual(calls[0][1], 150)
        self.assertEqual(coordinator.journal['staging_format'], 'tar-v1')
        self.assertEqual(coordinator.journal['staged_tar']['sha256'], c.file_hash(partial / 'staging.tar'))
        self.assertEqual((partial / 'staging.tar').stat().st_mode & 0o777, 0o600)
        self.assertFalse((partial / 'data').exists())
        self.assertFalse((partial / 'staging.tar.partial').exists())
        with (partial / 'staging.tar').open('rb') as stream:
            c.verify_tar(stream, manifest['files'], (partial / 'manifest.json').read_bytes())
        self.assertNotIn('data/zomboid/backups', records)
        self.assertEqual(records['data/relative']['target'], 'world')

    def test_manifest_size_limit_refuses_before_tar_creation(self):
        data, partial = self.root / 'zomboid', self.root / 'partial'
        data.mkdir()
        (data / 'world').write_bytes(b'world')
        (partial / 'recovery').mkdir(parents=True)
        coordinator = self.coordinator()
        coordinator.cfg.update(max_entries=100, max_snapshot_bytes=100000,
                               server_name='survival42', infra_revision='b' * 40)
        with patch.object(c, 'DATA', data), patch.object(c, 'free_space'), \
                patch.object(c, 'MANIFEST_MAX_BYTES', 1), patch.object(c, 'create_staging_tar') as create:
            with self.assertRaisesRegex(c.Refused, 'manifest_size_limit_exceeded'):
                coordinator.stage_stopped(partial, {'already_stopped': True})
            create.assert_not_called()
        self.assertEqual(coordinator.journal['phase'], 'writers_stopped')
        self.assertFalse((partial / 'staging.tar').exists())

    def test_corrupted_plaintext_tar_is_refused_before_encryption_subprocesses(self):
        stage = self.root / 'partial'
        stage.mkdir()
        plain = stage / 'staging.tar'
        plain.write_bytes(b'expected original plaintext')
        record = {'path': 'staging.tar', 'sha256': c.file_hash(plain), 'size': plain.stat().st_size}
        plain.write_bytes(b'corrupt! original plaintext')
        with patch.object(c.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(c.Refused, 'staging_tar_changed'):
                c.encrypted_archive(stage, stage / 'payload.enc', {}, 'unused', staged_tar=record)
            popen.assert_not_called()
        self.assertFalse((stage / 'payload.partial').exists())

    def test_tar_read_deadline_is_checked_before_and_after_every_underlying_read(self):
        source = Mock()
        source.read.return_value = b'bytes'
        stream = c.HashTee(source, deadline=100)
        with patch.object(c.time, 'monotonic', return_value=101):
            with self.assertRaisesRegex(c.Refused, 'staging_deadline_exceeded'):
                stream.read(1024)
            source.read.assert_not_called()
        with patch.object(c.time, 'monotonic', side_effect=[99, 101]):
            with self.assertRaisesRegex(c.Refused, 'staging_deadline_exceeded'):
                stream.read(1024)
        self.assertEqual(stream.size, 0)

    def test_tar_creation_failure_reaps_child_and_preserves_unverified_partial(self):
        stage = self.root / 'partial'
        stage.mkdir()
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired(['tar'], 1), 0]
        process.poll.return_value = None
        with patch.object(c.subprocess, 'Popen', return_value=process), patch.object(c.time, 'monotonic', return_value=99):
            with self.assertRaisesRegex(c.Refused, 'staging_deadline_exceeded'):
                c.create_staging_tar(stage, {'excluded': []}, 100)
        process.terminate.assert_called_once()
        self.assertTrue((stage / 'staging.tar.partial').exists())
        self.assertFalse((stage / 'staging.tar').exists())

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
            'status': {'phase': 'Running'}, 'spec': {'volumes': [{'name': 'data', 'hostPath': {'path': str(c.DATA.parent)}}],
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
