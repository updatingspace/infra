import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "sync_backups", Path(__file__).with_name("sync-postgres-backups.py")
)
sync_backups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_backups)

NAME = "20261009T120000Z-Ab1234"
PAYLOAD = b"synthetic encrypted archive"
CHECKSUM = hashlib.sha256(PAYLOAD).hexdigest() + "  postgres.tar.gpg\n"


class BackupSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "copies"
        root_patch = patch.object(sync_backups, "LOCAL_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def download(self, _command, *, stdout, **_kwargs):
        stdout.write(PAYLOAD)

    def test_commits_only_after_matching_sha256(self):
        with patch.object(sync_backups.subprocess, "check_output", side_effect=[NAME + "\n", CHECKSUM]), \
             patch.object(sync_backups.subprocess, "run", side_effect=self.download):
            sync_backups.sync()
        self.assertTrue((self.root / NAME / "COMMITTED").exists())
        self.assertEqual((self.root / NAME / "postgres.tar.gpg").read_bytes(), PAYLOAD)
        self.assertEqual((self.root / NAME / "postgres.tar.gpg").stat().st_mode & 0o777, 0o600)

    def test_mismatched_sha256_never_commits(self):
        wrong_checksum = "0" * 64 + "  postgres.tar.gpg\n"
        with patch.object(sync_backups.subprocess, "check_output", side_effect=[NAME + "\n", wrong_checksum]), \
             patch.object(sync_backups.subprocess, "run", side_effect=self.download):
            with self.assertRaisesRegex(ValueError, "does not match"):
                sync_backups.sync()
        self.assertFalse((self.root / NAME / "COMMITTED").exists())
        self.assertFalse((self.root / NAME / "postgres.tar.gpg").exists())

    def test_rejects_remote_path_before_any_download(self):
        with patch.object(sync_backups.subprocess, "check_output", return_value="../../outside\n"), \
             patch.object(sync_backups.subprocess, "run") as download:
            with self.assertRaisesRegex(ValueError, "Unexpected remote"):
                sync_backups.sync()
            download.assert_not_called()

    def test_does_not_overwrite_committed_copy(self):
        target = self.root / NAME
        target.mkdir(parents=True)
        (target / "COMMITTED").touch()
        (target / "postgres.tar.gpg").write_bytes(PAYLOAD)
        (target / "postgres.tar.gpg.sha256").write_text(CHECKSUM)
        with patch.object(sync_backups.subprocess, "check_output", return_value=NAME + "\n") as command, \
             patch.object(sync_backups.subprocess, "run") as download:
            sync_backups.sync()
            self.assertEqual(command.call_count, 1)
            download.assert_not_called()

    def test_rejects_corrupt_previously_committed_copy(self):
        target = self.root / NAME
        target.mkdir(parents=True)
        (target / "COMMITTED").touch()
        (target / "postgres.tar.gpg").write_bytes(b"damaged archive")
        (target / "postgres.tar.gpg.sha256").write_text(CHECKSUM)
        with patch.object(sync_backups.subprocess, "check_output", return_value=NAME + "\n"), \
             patch.object(sync_backups.subprocess, "run") as download:
            with self.assertRaisesRegex(ValueError, "does not match"):
                sync_backups.sync()
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
