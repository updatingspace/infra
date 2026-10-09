import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configure as c


class ConfigurationTests(unittest.TestCase):
    def test_ini_preserves_secrets_unknown_fields_identity_comments_and_crlf(self):
        original = (b'# private config\r\nPublic=false\r\nMaxPlayers=16\r\n'
                    b'Password=p=a=s=s\r\nRCONPassword=private\r\nDiscordToken=token\r\n'
                    b'WebhookAddress=https://invalid/private\r\nResetID=123\r\n'
                    b'ServerPlayerID=456\r\nSeed=789\r\nFutureSecret=keep\r\n')
        result, keys = c.merge_ini(original, {'Public': 'false', 'MaxPlayers': '20'})
        self.assertEqual(result, original.replace(b'MaxPlayers=16', b'MaxPlayers=20'))
        self.assertEqual(keys, ['MaxPlayers'])

    def test_rejects_secret_unknown_duplicate_missing_and_public_keys(self):
        original = b'Public=false\nMaxPlayers=16\n'
        for desired in [{'Public': 'true'}, {'Public': 'false', 'Password': 'x'},
                        {'Public': 'false', 'FutureSetting': '1'}, {'Public': 'false', 'MaxPlayers': '1\nPassword=x'},
                        {'Public': 'false', 'PVP': 'true'}]:
            with self.subTest(desired=list(desired)), self.assertRaises(ValueError):
                c.merge_ini(original, desired)
        with self.assertRaises(ValueError):
            c.merge_ini(original + b'Password=x\nPassword=y\n', {'Public': 'false'})
        with self.assertRaises(ValueError):
            json.loads('{"Public":"false","Public":"true"}', object_pairs_hook=c.unique)

    def fixture(self, directory):
        root = Path(directory)
        server, profile = root / 'Server', root / 'profile'
        server.mkdir(); profile.mkdir()
        (server / 'survival42.ini').write_bytes(b'Public=false\nPassword=private\n')
        (profile / 'survival42.ini.json').write_text('{"Public":"false"}')
        baseline = {}
        for name in c.LUA:
            data = b'return {}\n'
            (server / name).write_bytes(data); (profile / name).write_bytes(data)
            baseline[name] = c.sha(data)
        (profile / 'lua-base-sha256.json').write_text(json.dumps(baseline))
        return root, server, profile

    def test_lua_requires_reviewed_baseline_and_syntax_check(self):
        with tempfile.TemporaryDirectory() as directory:
            _, server, profile = self.fixture(directory)
            self.assertEqual(c.plan(server, profile), ([], []))
            (profile / c.LUA[0]).write_text('return { Zombies = 2 }\n')
            with patch.object(c, 'validate_lua') as validate:
                changes, _ = c.plan(server, profile)
                self.assertEqual(len(changes), 1); validate.assert_called_once()
            (server / c.LUA[0]).write_text('return { PrivateToken = "keep" }\n')
            with self.assertRaisesRegex(ValueError, 'unreviewed'):
                c.plan(server, profile)

    def test_transaction_failure_rolls_back_and_preserves_world_and_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server, _ = self.fixture(directory)
            world = root / 'world.db'; world.write_bytes(b'world-must-not-change')
            first, second = server / 'survival42.ini', server / c.LUA[0]
            first.chmod(0o640)
            original = first.read_bytes()
            changes = [(first, original, original + b'# changed\n'),
                       (second, second.read_bytes(), b'return { Zombies = 2 }\n')]
            real = c.replace_file
            calls = 0
            def fail_once(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('simulated second file failure')
                return real(*args)
            with patch.object(c, 'replace_file', side_effect=fail_once), self.assertRaises(OSError):
                c.write_changes(changes, root / 'private')
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(second.read_bytes(), b'return {}\n')
            self.assertEqual(first.stat().st_mode & 0o777, 0o640)
            self.assertEqual(world.read_bytes(), b'world-must-not-change')
            self.assertFalse((root / 'private/pending.json').exists())
            self.assertEqual((root / 'private').stat().st_mode & 0o777, 0o700)

    def test_pending_transaction_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server, _ = self.fixture(directory)
            target = server / 'survival42.ini'
            link = server / 'link'; link.symlink_to(target)
            with self.assertRaises(ValueError): c.read_regular(link)
            private = root / 'private'; private.mkdir(mode=0o700)
            (private / 'pending.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'unfinished'):
                c.write_changes([(target, target.read_bytes(), b'Public=false\n')], private)
            self.assertIn(b'Password=private', target.read_bytes())

    def test_lua_validation_is_fail_closed(self):
        with patch.object(c.shutil, 'which', return_value=None), self.assertRaises(ValueError):
            c.validate_lua(Path('reviewed.lua'))
        with patch.object(c.shutil, 'which', return_value='/usr/bin/luac'):
            with patch.object(c.subprocess, 'run') as run:
                run.return_value.returncode = 1
                with self.assertRaises(ValueError): c.validate_lua(Path('reviewed.lua'))

    def test_zero_replicas_do_not_allow_remaining_active_pod(self):
        responses = [{'spec': {'replicas': 0}}, {'spec': {'replicas': 0}},
                     {'spec': {'suspend': True}}, {'items': [{'status': {'phase': 'Running'}}]}]
        with patch.object(c.subprocess, 'check_output', side_effect=[json.dumps(x).encode() for x in responses]):
            with self.assertRaises(ValueError): c.require_stopped()

    def test_active_workload_cannot_apply(self):
        active = json.dumps({'spec': {'replicas': 1}}).encode()
        with patch.object(c.subprocess, 'check_output', return_value=active), self.assertRaises(ValueError):
            c.require_stopped()


if __name__ == '__main__':
    unittest.main()
