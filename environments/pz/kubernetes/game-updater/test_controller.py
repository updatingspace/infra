import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack, nullcontext

spec = importlib.util.spec_from_file_location('updater', Path(__file__).with_name('controller.py'))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class Tests(unittest.TestCase):
    def test_checks_do_not_stop_current_busy_or_newer_server(self):
        for installed, target, count, outcome in [('123', '123', 0, 'current'),
                ('123', '124', 3, 'deferred_players'), ('124', '123', 0, 'deferred_older_public_build')]:
            with self.subTest(outcome=outcome), ExitStack() as guards:
                guards.enter_context(patch.object(u.sys, 'argv', ['controller.py']))
                guards.enter_context(patch.object(u.os, 'geteuid', return_value=0))
                guards.enter_context(patch.object(u.b, 'configuration', return_value={}))
                guards.enter_context(patch.object(u.b, 'locked', return_value=nullcontext()))
                guards.enter_context(patch.object(u.b, 'disk_migration_ready'))
                guards.enter_context(patch.object(u.b, 'read_json', return_value={'phase': 'ready'}))
                worker = guards.enter_context(patch.object(u, 'Update')).return_value
                worker.original_state.return_value = {n: {'spec': {'replicas': 1}} for n in ('zomboid', 'panel', 'otel-collector')}
                worker.api.pods.return_value = []
                guards.enter_context(patch.object(u, 'installed', return_value=installed))
                guards.enter_context(patch.object(u, 'latest', return_value=target))
                guards.enter_context(patch.object(u, 'players', return_value=count))
                report = guards.enter_context(patch.object(u, 'status'))
                u.main()
                worker.run.assert_not_called()
                self.assertEqual(report.call_args.args[0], outcome)

    def test_public_build_not_other_branch(self):
        raw = 'noise\n"380870" { "depots" { "branches" { "unstable" { "buildid" "999" } "public" { "buildid" "123" } } } }\nend'
        with patch.object(u, 'kubectl', return_value=raw.encode()):
            self.assertEqual(u.latest(), '123')

    def test_missing_truncated_duplicate_fail_closed(self):
        for raw in ['"1" {}', '"380870" { "depots" {', '"380870" { "a" "1" "a" "2" }']:
            with self.subTest(raw=raw), self.assertRaises(u.b.Refused):
                u.vdf(raw, '380870')

    def test_players_rejects_unrecognized_response(self):
        with patch.object(u, 'kubectl', return_value=b'Players connected (0):'):
            self.assertEqual(u.players(), 0)
        with patch.object(u, 'kubectl', return_value=b'RCON unavailable'), self.assertRaises(u.b.Refused):
            u.players()

    def test_failure_never_starts_game_or_archives_old_snapshot(self):
        instance = u.Update({}, '123')
        instance.journal = {'phase': 'staging_verified'}
        instance.perform_update = Mock(side_effect=u.b.Refused('steam_update_failed'))
        instance.phase = Mock()
        instance.restore_replica = Mock()
        instance.restore_updater = Mock()
        with self.assertRaises(u.b.Refused):
            instance.restore_apps()
        instance.restore_replica.assert_called_once_with('otel-collector')
        instance.restore_updater.assert_not_called()
        instance.phase.assert_called_with('game_update_failed')

    def test_candidate_start_phases_cannot_be_recovered_as_backup(self):
        instance = u.Update({}, '123')
        instance.mutating = True
        with patch.object(u.b.Coordinator, 'phase') as parent:
            instance.phase('apps_restoring')
            parent.assert_called_once_with('game_update_starting')
        self.assertNotIn('game_update_starting', u.b.RECOVERABLE)

    def test_update_job_cannot_access_world_or_credentials(self):
        original = {'spec': {'template': {'spec': {
            'securityContext': {'runAsUser': 1000},
            'containers': [{'name': 'zomboid', 'image': 'local:test',
                'volumeMounts': [{'name': n, 'mountPath': '/' + n} for n in ('pz-server', 'steam', 'zomboid')]}],
            'volumes': [{'name': n, 'persistentVolumeClaim': {'claimName': n}} for n in ('pz-server', 'steam', 'zomboid')]
        }}}}
        pod = u.job_document(original, 'test')['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual({v['name'] for v in pod['volumes']}, {'pz-server', 'steam'})
        self.assertNotIn('envFrom', pod['containers'][0])
        self.assertIn("'-beta', 'public'", u.WORKER)


if __name__ == '__main__':
    unittest.main()
