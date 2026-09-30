"""Safe extraction tests run entirely offline on disposable temporary files."""

import base64
import copy
from decimal import Decimal
import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import remote
import restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "restored"
        self.content = b"saved world bytes"
        self.records = [
            self.record("data", "dir", mode=0o750),
            self.record("data/world.db", "file", size=len(self.content), sha256=hashlib.sha256(self.content).hexdigest()),
            self.record("recovery", "dir", mode=0o700),
        ]

    def record(self, path, kind, **kwargs):
        return {"path": path, "type": kind, "uid": os.geteuid(), "gid": os.getegid(),
                "mode": 0o640, "mtime_ns": 1767225600123456789, "xattrs": {}, **kwargs}

    def manifest(self, records=None):
        return {"format": "pz-backup-v1", "snapshot_id": "20260101T000000Z-" + "0" * 32,
                "captured_at": "2026-01-01T00:00:00Z", "files": records or self.records}

    def archive(self, manifest, *, extra=None, modify=None, contents=None):
        encoded = remote.canonical_json(manifest) + b"\n"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for row in manifest["files"]:
                info = tarfile.TarInfo(row["path"])
                info.uid, info.gid, info.mode = row["uid"], row["gid"], row["mode"]
                info.pax_headers["mtime"] = str(Decimal(row["mtime_ns"]) / 10**9)
                info.pax_headers.update({"SCHILY.xattr." + key: base64.b64decode(value).decode("utf-8", "surrogateescape")
                                         for key, value in row["xattrs"].items()})
                body = None
                if row["type"] == "dir":
                    info.type = tarfile.DIRTYPE
                elif row["type"] == "symlink":
                    info.type, info.linkname = tarfile.SYMTYPE, row["target"]
                elif "hardlink" in row:
                    info.type, info.linkname = tarfile.LNKTYPE, row["hardlink"]
                else:
                    body = (contents or {}).get(row["path"], self.content)
                    info.size = len(body)
                if modify:
                    modify(info)
                archive.addfile(info, io.BytesIO(body) if body is not None and info.isfile() else None)
            manifest_member = tarfile.TarInfo("manifest.json")
            manifest_member.size = len(encoded)
            archive.addfile(manifest_member, io.BytesIO(encoded))
            if extra:
                archive.addfile(extra)
        buffer.seek(0)
        return buffer, encoded

    def extract(self, manifest=None, **archive_options):
        manifest = manifest or self.manifest()
        stream, encoded = self.archive(manifest, **archive_options)
        return restore.extract_verified(stream, manifest, encoded, self.destination)

    def test_restores_hash_numeric_owner_mode_mtime_and_inventory(self):
        report = self.extract()
        world = self.destination / "data/world.db"
        self.assertEqual(world.read_bytes(), self.content)
        self.assertEqual(world.stat().st_mtime_ns, self.records[1]["mtime_ns"])
        self.assertEqual(stat.S_IMODE(world.stat().st_mode), 0o640)
        self.assertTrue(report["archive_verified"])
        self.assertTrue(report["runtime_drill_required"])
        self.assertFalse((self.destination / "restore-attestation.json").exists())

    def test_manifest_bound_matches_coordinator_reader_capacity(self):
        import coordinator
        self.assertEqual(restore.MAX_MANIFEST_BYTES, 512 * 1024 * 1024)
        self.assertEqual(coordinator.read_json.__defaults__[0], restore.MAX_MANIFEST_BYTES)

    def test_oversized_decrypted_manifest_is_rejected_and_age_reaped(self):
        stream = io.BytesIO(b"x" * 65)
        process = mock.Mock(stdout=stream)
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch.object(restore, "MAX_MANIFEST_BYTES", 64), \
             mock.patch.object(restore.subprocess, "Popen", return_value=process), \
             self.assertRaisesRegex(restore.RestoreError, "manifest_exceeds_limit"):
            restore._decrypt_manifest(self.root / "manifest.enc", self.root / "age.key")
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(stream.closed)

    def test_restored_manifest_keeps_exact_authenticated_bytes(self):
        manifest = self.manifest()
        stream, encoded = self.archive(manifest)
        restore.extract_verified(stream, manifest, encoded, self.destination)
        self.assertEqual((self.destination / "manifest.json").read_bytes(), encoded)

    def test_restore_preserves_hardlink_inode_and_internal_symlink(self):
        linked = {**self.records[1], "path": "data/world2.db", "hardlink": "data/world.db"}
        self.records.insert(2, linked)
        self.records.insert(3, self.record("data/current.db", "symlink", mode=0o777, target="world.db"))
        self.extract()
        self.assertEqual((self.destination / "data/world.db").stat().st_ino,
                         (self.destination / "data/world2.db").stat().st_ino)
        self.assertEqual(os.readlink(self.destination / "data/current.db"), "world.db")

    def test_restores_binary_extended_attribute(self):
        value = b"private\x00binary\xff"
        self.records[1]["xattrs"] = {"user.pz_test": base64.b64encode(value).decode("ascii")}
        self.extract()
        self.assertEqual(os.getxattr(self.destination / "data/world.db", "user.pz_test"), value)

    def test_corrupt_file_content_rejected(self):
        with self.assertRaisesRegex(restore.RestoreError, "sha256"):
            self.extract(contents={"data/world.db": b"corrupt contents!"})

    def test_tar_metadata_mismatch_rejected(self):
        def modify(info):
            if info.name == "data/world.db":
                info.uid += 1
        with self.assertRaisesRegex(restore.RestoreError, "metadata"):
            self.extract(modify=modify)

    def test_missing_file_and_extra_members_rejected(self):
        extra = tarfile.TarInfo("data/unexpected")
        with self.assertRaisesRegex(restore.RestoreError, "unexpected_member"):
            self.extract(extra=extra)

    def test_absolute_archive_member_rejected(self):
        extra = tarfile.TarInfo(str(self.root / "escaped"))
        with self.assertRaisesRegex(restore.RestoreError, "unsafe_archive_path"):
            self.extract(extra=extra)
        self.assertFalse((self.root / "escaped").exists())

    def test_dotdot_archive_member_rejected(self):
        extra = tarfile.TarInfo("data/../../escaped")
        with self.assertRaisesRegex(restore.RestoreError, "unsafe_archive_path"):
            self.extract(extra=extra)
        self.assertFalse((self.root / "escaped").exists())

    def test_device_type_rejected(self):
        def modify(info):
            if info.name == "data/world.db":
                info.type, info.size = tarfile.CHRTYPE, 0
        with self.assertRaisesRegex(restore.RestoreError, "file_type_or_size"):
            self.extract(modify=modify)

    def test_absolute_and_escaping_manifest_symlinks_rejected(self):
        for target in ("/etc/passwd", "../../etc/passwd", "../recovery"):
            with self.subTest(target=target):
                manifest = self.manifest([*self.records, self.record("data/link", "symlink", mode=0o777, target=target)])
                with self.assertRaises(restore.RestoreError):
                    restore.validate_manifest(manifest)

    def test_symlink_cannot_be_archive_ancestor(self):
        manifest = self.manifest([*self.records,
            self.record("data/link", "symlink", mode=0o777, target="."),
            self.record("data/link/evil", "file", size=0, sha256=hashlib.sha256(b"").hexdigest())])
        with self.assertRaisesRegex(restore.RestoreError, "non_directory"):
            restore.validate_manifest(manifest)

    def test_symlink_cycle_is_rejected(self):
        manifest = self.manifest([*self.records,
            self.record("data/link1", "symlink", mode=0o777, target="link2"),
            self.record("data/link2", "symlink", mode=0o777, target="link1")])
        with self.assertRaisesRegex(restore.RestoreError, "cyclic"):
            restore.validate_manifest(manifest)

    def test_hardlink_cannot_reference_outside_manifest(self):
        manifest = self.manifest(copy.deepcopy(self.records))
        manifest["files"][1]["hardlink"] = "../../etc/passwd"
        with self.assertRaises(restore.RestoreError):
            restore.validate_manifest(manifest)

    def test_duplicate_manifest_paths_rejected(self):
        with self.assertRaisesRegex(restore.RestoreError, "duplicate_manifest"):
            restore.validate_manifest(self.manifest([*self.records, self.records[1]]))

    def test_duplicate_archive_members_rejected(self):
        with self.assertRaisesRegex(restore.RestoreError, "duplicate_member"):
            self.extract(extra=tarfile.TarInfo("data/world.db"))

    def test_destination_must_not_exist_or_be_symlink(self):
        self.destination.mkdir()
        with self.assertRaisesRegex(restore.RestoreError, "must_be_new"):
            self.extract()
        self.destination.rmdir()
        self.destination.symlink_to(self.root / "other")
        with self.assertRaisesRegex(restore.RestoreError, "must_be_new"):
            self.extract()

    def test_destination_parent_cannot_be_symlink(self):
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(restore.RestoreError, "unsafe_restore_parent"):
            restore.prepare_destination(self.root / "alias/new")

    def test_production_and_dotdot_destination_rejected(self):
        for path in ("/srv/pz-storage/zomboid/test", "/opt/pz-stack/data/new",
                     "/tmp/../srv/pz-storage/zomboid/test"):
            with self.subTest(path=path):
                with self.assertRaises(restore.RestoreError):
                    restore.prepare_destination(path)

    def test_archive_decompression_budget_is_bounded(self):
        reader = restore.BudgetReader(io.BytesIO(b"x" * 100), 10)
        with self.assertRaisesRegex(restore.RestoreError, "budget"):
            reader.read(100)

    def test_real_gnu_tar_matches_coordinator_inventory_and_restores(self):
        import coordinator
        source = self.root / "source"
        (source / "data").mkdir(parents=True)
        (source / "recovery").mkdir()
        world = source / "data/world.db"
        world.write_bytes(self.content)
        os.setxattr(world, "user.pz_test", b"binary\x00value\xff")
        os.link(world, source / "data/world2.db")
        (source / "data/current.db").symlink_to("world.db")
        records = []
        for prefix in ("data", "recovery"):
            for row in coordinator.inventory(source / prefix):
                record = {**row, "path": prefix if row["path"] == "." else prefix + "/" + row["path"]}
                if "hardlink" in row:
                    record["hardlink"] = prefix + "/" + row["hardlink"]
                records.append(record)
        manifest = self.manifest(records)
        encoded = remote.canonical_json(manifest) + b"\n"
        (source / "manifest.json").write_bytes(encoded)
        archive = subprocess.run(["tar", "--format=pax", "--sort=name", "--acls", "--xattrs",
                                  "--xattrs-include=*", "--pax-option=delete=atime,delete=ctime", "--numeric-owner",
                                  "-C", str(source), "-cf", "-", "data", "recovery", "manifest.json"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        report = restore.extract_verified(io.BytesIO(archive), manifest, encoded, self.destination)
        self.assertTrue(report["archive_verified"])
        self.assertEqual((self.destination / "data/world.db").stat().st_ino,
                         (self.destination / "data/world2.db").stat().st_ino)


if __name__ == "__main__":
    unittest.main()
