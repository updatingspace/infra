#!/usr/bin/env python3
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import cleanup as cleanup
import remote
import coordinator as c


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = '20260930T010203Z-' + 'a' * 32
        self.cfg = remote.RemoteConfig(bucket='private-backups', prefix='pz/production', endpoint_url='https://storage.yandexcloud.net')
        self.directory = self.root / (self.sid + '.ready')
        self.directory.mkdir(mode=0o750)
        self.ready = {'format': 'pz-backup-v1', 'snapshot_id': self.sid, 'captured_at': '2026-09-30T01:02:03Z'}
        self.commit = {'format': remote.FORMAT, 'snapshot_format': self.cfg.snapshot_format, 'snapshot_id': self.sid,
                       'captured_at': self.ready['captured_at'], 'verified_at': '2026-09-30T01:03:03Z'}
        for kind in ('payload', 'manifest'):
            value = (kind + ' ENCRYPTED CONTENT').encode()
            path = self.directory / (kind + '.enc')
            path.write_bytes(value)
            path.chmod(0o640)
            spec = {'sha256': hashlib.sha256(value).hexdigest(), 'size': len(value)}
            self.ready[kind] = {'path': kind + '.enc', **spec}
            self.commit[kind] = {'key': 'pz/production/' + self.sid + '/' + kind + '.enc', **spec}
        c.atomic_json(self.directory / 'ready.json', self.ready)
        digest = remote.commit_sha256(self.commit)
        self.receipt = {'format': 'pz-upload-receipt-v1', 'snapshot_id': self.sid, 'scope': self.cfg.scope,
                        'commit': self.commit, 'commit_sha256': digest}
        self.result = {'format': 'pz-backup-remote-verified-v1', 'snapshot_id': self.sid, 'scope': self.cfg.scope,
                       'commit': self.commit, 'commit_sha256': digest}
        self.journal = self.root / 'cleanup-journal.json'
        self.receipt_patch = patch.object(cleanup, 'read_receipt', return_value=self.receipt)
        self.spool_patch = patch.object(cleanup, 'SPOOL', self.root)
        self.receipt_patch.start()
        self.spool_patch.start()

    def tearDown(self):
        self.receipt_patch.stop()
        self.spool_patch.stop()
        self.tmp.cleanup()

    def clean(self, result=None, **kwargs):
        return cleanup.cleanup_one(self.cfg, self.sid, self.journal, owner_uid=os.getuid(),
                                   verify=kwargs.pop('verify', lambda _sid: result or self.result), **kwargs)

    def test_deletes_only_committed_ciphertext_leaving_receipts_and_partial(self):
        partial = self.root / ('20260930T010204Z-' + 'b' * 32 + '.partial')
        partial.mkdir()
        (partial / 'source-world').write_bytes(b'private untouched')
        self.clean()
        self.assertFalse(self.directory.exists())
        self.assertEqual((partial / 'source-world').read_bytes(), b'private untouched')
        self.assertEqual(c.read_json(self.journal)['phase'], 'complete')
        self.assertEqual(self.receipt['snapshot_id'], self.sid)

    def test_receipt_alone_cannot_authorize_cleanup_when_remote_readback_fails(self):
        with self.assertRaises(remote.BackupError):
            self.clean(verify=Mock(side_effect=remote.BackupError('download mismatch')))
        self.assertEqual(set(p.name for p in self.directory.iterdir()), set(cleanup.FILES))
        self.assertFalse(self.journal.exists())

    def test_wrong_bucket_scope_and_wrong_payload_bytes_never_delete(self):
        result = deepcopy(self.result)
        result['scope']['bucket'] = 'attacker-bucket'
        with self.assertRaisesRegex(c.Refused, 'scope_mismatch'):
            self.clean(result=result)
        (self.directory / 'payload.enc').write_bytes(b'CORRUPTED')
        with self.assertRaisesRegex(c.Refused, 'local_content_mismatch'):
            self.clean()
        self.assertTrue((self.directory / 'manifest.enc').exists())

    def test_unknown_ready_member_and_symlink_are_not_deleted(self):
        unknown = self.directory / 'unrelated-backup'
        unknown.write_bytes(b'manual backup')
        with self.assertRaisesRegex(c.Refused, 'unexpected_ready_members'):
            self.clean()
        unknown.unlink()
        (self.directory / 'payload.enc').unlink()
        (self.directory / 'payload.enc').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(c.Refused, 'ciphertext_file_invalid'):
            self.clean()

    def test_interrupted_unlink_requires_fresh_remote_verification_and_resumes(self):
        original_unlink = Path.unlink
        def interrupt(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            if path.name == 'payload.enc':
                raise OSError('power loss after unlink before journal update')
        with patch.object(Path, 'unlink', interrupt):
            with self.assertRaises(OSError):
                self.clean()
        previous = c.read_json(self.journal)
        self.assertEqual(previous['files']['payload.enc']['state'], 'deleting')
        verify = Mock(return_value=self.result)
        self.clean(existing=previous, verify=verify)
        verify.assert_called_once_with(self.sid)
        self.assertFalse(self.directory.exists())

    def test_pending_file_missing_on_resume_is_ambiguous_and_retained(self):
        original_write = c.atomic_json
        def interrupt(path, value):
            original_write(path, value)
            if value.get('phase') == 'deleting':
                raise OSError('power loss before deleting')
        with patch.object(c, 'atomic_json', interrupt):
            with self.assertRaises(OSError):
                self.clean()
        (self.directory / 'payload.enc').unlink()
        with self.assertRaisesRegex(c.Refused, 'pending_file_disappeared'):
            self.clean(existing=c.read_json(self.journal))
        self.assertTrue((self.directory / 'manifest.enc').exists())


class ReadOnlyVerifierTests(unittest.TestCase):
    def test_requires_full_payload_manifest_gets_and_repeated_marker(self):
        spec = importlib.util.spec_from_file_location('verify_remote', Path(__file__).with_name('verify-remote.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cfg = remote.RemoteConfig(bucket='private-backups', prefix='pz/prod')
        sid = '20260930T010203Z-' + 'a' * 32
        commit = {'payload': {'key': 'payload'}, 'manifest': {'key': 'manifest'}}
        from contextlib import nullcontext
        with patch.object(remote, '_lock', return_value=nullcontext()), \
                patch.object(remote, 'read_commit', return_value=(commit, {})) as marker, \
                patch.object(remote, '_verify') as verify:
            result = module.verify_snapshot(object(), cfg, sid)
        self.assertEqual(marker.call_count, 2)
        self.assertEqual([call.args[2] for call in verify.call_args_list], [commit['payload'], commit['manifest']])
        self.assertEqual(result['scope'], cfg.scope)


if __name__ == '__main__':
    unittest.main()
