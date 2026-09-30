"""Offline fixtures use the observed tar member prefix, independently of code."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location("recovery", Path(__file__).with_name("restore-startup-backups.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
ARCHIVED_STARTUP = "pz-stack/data/zomboid/backups/startup"


def zip_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("Server/test.ini", b"fixture")
    return output.getvalue()


class RecoveryTests(unittest.TestCase):
    def run_case(self, extra=None, omit=None, wrong_hash=False, no_space=False, prefix=ARCHIVED_STARTUP,
                 directory_mode=0o775, directory_owner=(1000, 1000), zip_mode=0o644, reject_metadata=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup = root / "archive.tar.gz"
            stage = root / "stage"
            stage.mkdir()
            with tarfile.open(backup, "w:gz") as archive:
                directory = tarfile.TarInfo(prefix)
                directory.type = tarfile.DIRTYPE
                directory.uid, directory.gid = directory_owner
                directory.mode = directory_mode
                archive.addfile(directory)
                world = tarfile.TarInfo("pz-stack/data/zomboid/Saves/world.bin")
                world.size = 5
                archive.addfile(world, io.BytesIO(b"world"))
                for name in [f"backup_{number}.zip" for number in range(1, 6)]:
                    if name == omit:
                        continue
                    member = tarfile.TarInfo(prefix + "/" + name)
                    member.uid = member.gid = 1000
                    member.mode = zip_mode
                    data = zip_bytes()
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                if extra:
                    member = tarfile.TarInfo(prefix + "/" + extra[0])
                    member.uid = member.gid = 1000
                    member.mode = 0o644
                    if extra[1] == "link":
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/tmp/target"
                        archive.addfile(member)
                    else:
                        data = zip_bytes()
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
            with contextlib.ExitStack() as patches:
                patches.enter_context(patch.object(recovery, "ARCHIVE", backup))
                patches.enter_context(patch.object(recovery, "TRUSTED_SHA256", "0" * 64 if wrong_hash else hashlib.sha256(backup.read_bytes()).hexdigest()))
                patches.enter_context(patch.object(recovery.os, "chown"))
                patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
                if no_space:
                    patches.enter_context(patch.object(recovery.os, "statvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)))
                if extra or omit or wrong_hash or no_space or prefix != ARCHIVED_STARTUP or reject_metadata:
                    with self.assertRaises(RuntimeError):
                        recovery.extract_stage(stage)
                else:
                    files, directory = recovery.extract_stage(stage)
                    expected = {f"backup_{number}.zip" for number in range(1, 6)}
                    self.assertEqual(set(files), expected)
                    self.assertEqual({path.name for path in stage.iterdir()}, expected)
                    self.assertTrue(all(value["zip_entries"] == 1 for value in files.values()))
                    self.assertEqual(directory.mode, directory_mode)
                    self.assertTrue(all(path.stat().st_mode & 0o777 == zip_mode for path in stage.iterdir()))
                self.assertFalse((root / "world.bin").exists())

    def test_observed_archive_prefix_restores_exact_set(self):
        self.assertEqual(recovery.PREFIX, ARCHIVED_STARTUP)
        self.run_case()

    def test_incorrect_opt_prefix_cannot_be_activated(self):
        self.run_case(prefix="opt/" + ARCHIVED_STARTUP)

    def test_non_group_writable_directory_remains_valid(self):
        self.run_case(directory_mode=0o755)

    def test_reject_directory_world_write_special_bits_or_other_owner(self):
        for mode in (0o777, 0o2775, 0o4775, 0o1775):
            with self.subTest(mode=oct(mode)):
                self.run_case(directory_mode=mode, reject_metadata=True)
        for owner in ((0, 1000), (1000, 0)):
            with self.subTest(owner=owner):
                self.run_case(directory_owner=owner, reject_metadata=True)

    def test_zip_write_and_special_bit_guards_unchanged(self):
        for mode in (0o664, 0o646, 0o2644, 0o4644):
            with self.subTest(mode=oct(mode)):
                self.run_case(zip_mode=mode, reject_metadata=True)

    def test_reject_duplicate(self):
        self.run_case(extra=("backup_1.zip", "file"))

    def test_reject_traversal_extra_nested_and_symlink(self):
        for name in ("../world.bin", "backup_6.zip", "nested/backup_1.zip"):
            with self.subTest(name=name):
                self.run_case(extra=(name, "file"))
        self.run_case(extra=("backup_5.zip", "link"), omit="backup_5.zip")

    def test_reject_missing_member(self):
        self.run_case(omit="backup_5.zip")

    def test_reject_untrusted_archive(self):
        self.run_case(wrong_hash=True)

    def test_free_space_gate(self):
        self.run_case(no_space=True)


if __name__ == "__main__":
    unittest.main()
