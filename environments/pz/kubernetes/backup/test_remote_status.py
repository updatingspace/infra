"""Remote monitoring reports contain observed facts and fixed failure classes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_remote import remote


class RemoteStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'remote-status.json'
        self.config = remote.RemoteConfig(bucket='pz-private', prefix='pz/test',
                                          endpoint_url='https://storage.yandexcloud.net')

    def test_full_multipart_listing_uses_all_pages(self):
        client = Mock()
        client.list_multipart_uploads.side_effect = [
            {'IsTruncated': True, 'Uploads': [{'Key': 'pz/test/a'}],
             'NextKeyMarker': 'pz/test/a', 'NextUploadIdMarker': 'u1'},
            {'IsTruncated': False, 'Uploads': [{'Key': 'pz/test/b'}]},
        ]
        self.assertEqual(remote.multipart_inventory(client, self.config), 2)
        self.assertEqual(client.list_multipart_uploads.call_args.kwargs,
                         {'Bucket': 'pz-private', 'Prefix': 'pz/test/',
                          'KeyMarker': 'pz/test/a', 'UploadIdMarker': 'u1'})

    def test_partial_and_malformed_inventory_never_return_zero(self):
        for page in ({}, {'IsTruncated': True},
                     {'IsTruncated': False, 'Uploads': [{'Key': 'outside/secret'}]},
                     {'IsTruncated': False, 'Uploads': [None]}):
            with self.subTest(page=page), self.assertRaises(remote.BackupError):
                remote.multipart_inventory(Mock(list_multipart_uploads=Mock(return_value=page)), self.config)
        repeated = {'IsTruncated': True, 'Uploads': [], 'NextKeyMarker': 'pz/test/a', 'NextUploadIdMarker': 'u1'}
        with self.assertRaises(remote.BackupError):
            remote.multipart_inventory(Mock(list_multipart_uploads=Mock(return_value=repeated)), self.config)

    def test_failure_never_leaks_error_text_or_reuses_stale_counts(self):
        remote.write_remote_status(self.path, result={'verified_snapshots': 5}, incomplete_multipart=0)
        remote.write_remote_status(self.path, error=remote.BackupError('SHA256 mismatch PRIVATE_PLAYER token=SECRET'))
        body = self.path.read_text()
        report = json.loads(body)
        self.assertTrue(report['verification_failed'])
        self.assertEqual(report['verification_failure_class'], 'checksum')
        self.assertNotIn('verified_snapshots', report)
        self.assertNotIn('incomplete_multipart', report)
        self.assertNotIn('PRIVATE_PLAYER', body)
        self.assertNotIn('SECRET', body)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_disabled_retention_and_upload_do_not_claim_verified_count(self):
        remote.write_remote_status(self.path, result={'enabled': False}, incomplete_multipart=0)
        report = json.loads(self.path.read_text())
        self.assertFalse(report['verification_failed'])
        self.assertEqual(report['incomplete_multipart'], 0)
        self.assertNotIn('verified_snapshots', report)

    def test_complete_retention_count_and_sdk_failure(self):
        remote.write_remote_status(self.path, result={'verified_snapshots': 5})
        self.assertEqual(json.loads(self.path.read_text())['verified_snapshots'], 5)
        remote.write_remote_status(self.path, error=TimeoutError('SECRET signed URL'))
        self.assertEqual(json.loads(self.path.read_text())['verification_failure_class'], 'remote_io')


if __name__ == '__main__':
    unittest.main()
