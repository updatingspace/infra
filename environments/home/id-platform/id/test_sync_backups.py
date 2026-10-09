import hashlib
from pathlib import Path
import runpy
import tempfile
import unittest

VERIFY = runpy.run_path(str(Path(__file__).with_name('sync-backups.py')))['verify_archive']


class BackupIntegrityTest(unittest.TestCase):
    def test_corrupt_payload_and_malformed_manifest_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'backup.gpg'
            archive.write_bytes(b'synthetic encrypted backup')
            expected = hashlib.sha256(archive.read_bytes()).hexdigest() + '\n'
            VERIFY(archive, expected)
            for malformed in ['bad', '../unexpected\n', '0' * 64 + '\n']:
                with self.assertRaises(ValueError):
                    VERIFY(archive, malformed)
            archive.write_bytes(b'corrupted')
            with self.assertRaises(ValueError):
                VERIFY(archive, expected)


if __name__ == '__main__':
    unittest.main()
