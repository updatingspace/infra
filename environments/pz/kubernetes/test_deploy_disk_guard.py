"""Interrupted data-disk cutovers must block every new host mutation."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deploy_disk_guard', Path(__file__).with_name('deploy.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DiskGuardTests(unittest.TestCase):
    def test_migration_preflight_error_is_reported_without_polling(self):
        reply = subprocess.CompletedProcess('ssh', 0, stdout=json.dumps({
            'status': 'blocked', 'error': 'Data disk migration requires operator recovery'}))
        with patch.object(deploy, 'ssh', return_value=reply), patch.object(deploy.time, 'monotonic', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'operator recovery'):
                deploy.updater_request('pause', 60)

    def test_only_absent_or_valid_completed_journal_allows_mutation(self):
        namespace = {}
        exec(deploy.DISK_MIGRATION_GUARD, namespace)
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / 'journal.json'
            info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_size=100)
            with patch.dict(namespace, Path=lambda _: journal), patch.object(os, 'fstat', return_value=info):
                check = namespace['require_disk_migration_terminal']
                check()
                for phase in ('copying', 'boot_configuration_written', 'new_disk_mounted', 'cutover_verified', 'unexpected'):
                    journal.write_text(json.dumps({'format': 'pz-disk-migration-v1', 'phase': phase}))
                    with self.subTest(phase=phase), self.assertRaisesRegex(RuntimeError, 'recovery'):
                        check()
                journal.write_text(json.dumps({'format': 'pz-disk-migration-v1', 'phase': 'complete'}))
                check()
                for text in ('invalid-json', '{"phase":"complete"}', '[]'):
                    journal.write_text(text)
                    with self.assertRaises(RuntimeError):
                        check()
                journal.unlink()
                journal.symlink_to(Path(temporary) / 'missing')
                with self.assertRaises(RuntimeError):
                    check()

    def test_actual_locked_exec_checks_journal_before_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / 'journal.json'
            journal.write_text('{"format":"pz-disk-migration-v1","phase":"copying"}')
            script = deploy.GUARDED_EXEC.replace('Path("/var/lib/pz-backup/disk-migration/journal.json")', f'Path({str(journal)!r})')
            with patch('sys.argv', ['-c', 'tar', '-xzf', '-']), patch.object(os, 'execvp') as execute:
                with self.assertRaises(RuntimeError):
                    exec(script, {})
                execute.assert_not_called()

    def test_incomplete_cutover_blocks_new_apply_before_launch_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / 'journal.json'
            journal.write_text('{"format":"pz-disk-migration-v1","phase":"new_disk_mounted"}')
            script = deploy.APPLY_REMOTE.replace('Path("/opt/pz-infrastructure")', f'Path({temporary!r})')
            script = script.replace('Path("/var/lib/pz-backup/disk-migration/journal.json")', f'Path({str(journal)!r})', 1)
            def command(arguments, **kwargs):
                self.assertEqual(arguments[0], 'systemctl')
                return subprocess.CompletedProcess(arguments, 0, stdout='LoadState=not-found\n', stderr='')
            with patch.object(subprocess, 'run', side_effect=command), \
                    patch('sys.argv', ['-', 'launch', 'platform', 'a' * 64]), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit):
                    exec(script, {})
                self.assertIn('disk migration', json.loads(output.getvalue())['error'])
                self.assertFalse(list(Path(temporary).rglob('launch-attempt')))


if __name__ == '__main__':
    unittest.main()
