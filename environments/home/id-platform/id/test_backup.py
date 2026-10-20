import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('id_backup', Path(__file__).with_name('backup.py'))
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class RecoveryTest(unittest.TestCase):
    def test_ydb_timeout_still_restores_id_and_every_cron_then_retries(self):
        state = {'replicas': 1, 'ydb_replicas': 1,
                 'cron_suspend': {'recover-mail': False, 'refresh-gravatars': True}}
        calls = []
        def failing_kube(*args):
            calls.append(args)
            if 'rollout' in args and 'statefulset/id-ydb' in args:
                raise subprocess.CalledProcessError(1, args, stderr=b'timed out')
            return b'{}'
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'resume-state.json'
            marker.write_text(json.dumps(state))
            with patch.object(backup, 'kube', failing_kube):
                with self.assertRaises(Exception):
                    backup.restore(state, marker)
            self.assertTrue(marker.exists(), 'Do not clear an unverified recovery')
            self.assertTrue(any('deployment/id' in x and 'scale' in x for x in calls),
                            'YDB timeout stranded ID at zero replicas')
            for name in state['cron_suspend']:
                self.assertTrue(any('patch' in x and name in x for x in calls),
                                'YDB timeout left a CronJob suspended')
            with patch.object(backup, 'kube', return_value=b'{}'):
                backup.restore(state, marker)
            self.assertFalse(marker.exists())


class RecoverySafetyTest(unittest.TestCase):
    def test_one_failed_cron_does_not_skip_remaining_crons(self):
        state = {'replicas': 1, 'ydb_replicas': 1,
                 'cron_suspend': {'first': False, 'second': True}}
        calls = []
        def kube(*args):
            calls.append(args)
            if 'first' in args:
                raise subprocess.TimeoutExpired(args, 30)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'resume-state.json'
            backup.save_state(marker, state)
            with patch.object(backup, 'kube', kube), self.assertRaises(RuntimeError):
                backup.restore(state, marker)
            self.assertTrue(any('second' in x for x in calls))
            self.assertEqual(json.loads(marker.read_text()), state)

    def test_replaced_workload_is_not_modified(self):
        state = {'resources': [{'namespace': 'updspace-id', 'name': 'deployment/id', 'uid': 'old'}]}
        with patch.object(backup, 'kube', return_value=b'{"metadata":{"uid":"new"}}') as kube:
            with self.assertRaisesRegex(RuntimeError, 'replaced'):
                backup.restore(state, Path('/not-used'))
            self.assertEqual(kube.call_count, 1)
            self.assertIn('get', kube.call_args.args)

    def test_resume_skips_a_running_backup(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / 'resume-state.json'
            marker.write_text('{}')
            with (base / 'backup.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(backup, 'BASE', base), patch('sys.argv', ['backup.py', '--resume']), \
                        patch.object(backup, 'restore') as restore:
                    backup.main()
                    restore.assert_not_called()
            self.assertTrue(marker.exists())

    def test_resume_without_marker_does_not_start_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(backup, 'BASE', Path(directory)), \
                    patch('sys.argv', ['backup.py', '--resume']), patch.object(backup, 'kube') as kube:
                backup.main()
                kube.assert_not_called()
            self.assertEqual([x.name for x in Path(directory).iterdir()], ['backup.lock'])


if __name__ == '__main__':
    unittest.main()
