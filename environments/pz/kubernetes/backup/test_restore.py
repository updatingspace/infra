"""Safe extraction tests run entirely offline on disposable temporary files."""

import base64
import copy
from decimal import Decimal
import errno
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

    def download_fixture(self, *, age_status=0):
        source = self.root / "download"
        source.mkdir(exist_ok=True)
        identity = self.root / "age.key"
        identity.write_bytes(b"private fixture; decryption is mocked")
        identity.chmod(0o600)
        manifest = self.manifest()
        stream, encoded = self.archive(manifest)
        marker = {"format": remote.FORMAT, "snapshot_format": "pz-backup-v1",
                  "snapshot_id": manifest["snapshot_id"], "captured_at": manifest["captured_at"]}
        for kind in ("payload", "manifest"):
            path = source / (kind + ".enc")
            path.write_bytes(kind.encode())
            marker[kind] = remote._local_digest(path)
        remote._atomic_json(source / "COMMITTED.json", marker)
        age = mock.Mock(stdout=io.BytesIO())
        age.wait.return_value = age.poll.return_value = age_status
        zstd = mock.Mock(stdout=stream)
        zstd.wait.return_value = zstd.poll.return_value = 0
        return source, identity, encoded, age, zstd

    def test_filesystem_barrier_open_precedes_mkdir_and_stays_open_through_report(self):
        source, identity, encoded, age, zstd = self.download_fixture()
        descriptors, events = [], []
        original_open, original_mkdir = os.open, Path.mkdir
        original_atomic = remote._atomic_json
        def opening(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            if Path(path) == self.root and flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW:
                descriptors.append(fd)
                events.append("open")
                self.assertFalse(self.destination.exists())
            return fd
        def mkdir(path, *args, **kwargs):
            self.assertTrue(descriptors)
            os.fstat(descriptors[0])
            events.append("mkdir")
            return original_mkdir(path, *args, **kwargs)
        def syncfs(fd):
            self.assertEqual(fd, descriptors[0])
            self.assertEqual((self.destination / "manifest.json").read_bytes(), encoded)
            restore._verify_extracted(self.destination, restore.validate_manifest(self.manifest()))
            events.append("syncfs")
            return 0
        def atomic(path, value):
            self.assertEqual(path.name, "restored.json")
            self.assertEqual(events[-1], "syncfs")
            os.fstat(descriptors[0])
            original_atomic(path, value)
            os.fstat(descriptors[0])
            events.append("report")
        barrier = mock.Mock(side_effect=syncfs)
        with mock.patch.object(restore.platform, "system", return_value="Linux"), \
             mock.patch.object(restore.platform, "release", return_value="6.8.0-test"), \
             mock.patch.object(restore.ctypes, "CDLL", return_value=mock.Mock(syncfs=barrier)), \
             mock.patch.object(restore, "_decrypt_manifest", return_value=encoded), \
             mock.patch.object(restore.subprocess, "Popen", side_effect=[age, zstd]), \
             mock.patch.object(os, "open", side_effect=opening), \
             mock.patch.object(Path, "mkdir", mkdir), \
             mock.patch.object(remote, "_atomic_json", side_effect=atomic):
            report = restore.restore_download(source, identity, self.destination, durability="filesystem")
        self.assertEqual(events[0], "open")
        self.assertLess(events.index("open"), events.index("mkdir"))
        self.assertEqual(events[-1], "report")
        self.assertLess(events.index("syncfs"), events.index("report"))
        self.assertEqual(report["durability"], "filesystem-syncfs")
        barrier.assert_called_once()
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_filesystem_writeback_errors_are_fatal_once_and_never_write_success(self):
        for code in (errno.EIO, errno.ENOSPC, errno.EDQUOT):
            with self.subTest(errno=code):
                self.destination = self.root / ("restored-" + str(code))
                source, identity, encoded, age, zstd = self.download_fixture()
                seen = []
                def syncfs(fd):
                    seen.append(fd)
                    return -1 if len(seen) == 1 else 0
                barrier = mock.Mock(side_effect=syncfs)
                with mock.patch.object(restore.platform, "system", return_value="Linux"), \
                     mock.patch.object(restore.platform, "release", return_value="6.8.0"), \
                     mock.patch.object(restore.ctypes, "CDLL", return_value=mock.Mock(syncfs=barrier)), \
                     mock.patch.object(restore.ctypes, "get_errno", return_value=code), \
                     mock.patch.object(restore, "_decrypt_manifest", return_value=encoded), \
                     mock.patch.object(restore.subprocess, "Popen", side_effect=[age, zstd]), \
                     self.assertRaisesRegex(restore.RestoreError, "filesystem_durability_failed_" + errno.errorcode[code]):
                    restore.restore_download(source, identity, self.destination, durability="filesystem")
                barrier.assert_called_once()
                self.assertFalse((self.destination / "restored.json").exists())
                with self.assertRaises(OSError):
                    os.fstat(seen[0])

    def test_late_age_failure_still_prevents_success_after_filesystem_barrier(self):
        source, identity, encoded, age, zstd = self.download_fixture(age_status=1)
        barrier = mock.Mock(return_value=0)
        with mock.patch.object(restore.platform, "system", return_value="Linux"), \
             mock.patch.object(restore.platform, "release", return_value="6.8.0"), \
             mock.patch.object(restore.ctypes, "CDLL", return_value=mock.Mock(syncfs=barrier)), \
             mock.patch.object(restore, "_decrypt_manifest", return_value=encoded), \
             mock.patch.object(restore.subprocess, "Popen", side_effect=[age, zstd]), \
             self.assertRaisesRegex(restore.RestoreError, "payload_decryption_or_decompression_failed"):
            restore.restore_download(source, identity, self.destination, durability="filesystem")
        barrier.assert_called_once()
        self.assertTrue((self.destination / "manifest.json").exists())
        self.assertFalse((self.destination / "restored.json").exists())
        with self.assertRaises(OSError):
            os.fstat(barrier.call_args.args[0])

    def test_filesystem_mode_retains_manifest_fsync_and_default_retains_file_fsync(self):
        original_fsync = os.fsync
        for mode in ("per-file", "filesystem"):
            with self.subTest(mode=mode):
                self.destination = self.root / mode
                stream, encoded = self.archive(self.manifest())
                synced = []
                def fsync(fd):
                    synced.append(Path(os.readlink("/proc/self/fd/" + str(fd))).name)
                    original_fsync(fd)
                barrier = mock.Mock(return_value=0)
                with mock.patch.object(restore.platform, "system", return_value="Linux"), \
                     mock.patch.object(restore.platform, "release", return_value="6.8.0"), \
                     mock.patch.object(restore.ctypes, "CDLL", return_value=mock.Mock(syncfs=barrier)), \
                     mock.patch.object(os, "fsync", side_effect=fsync):
                    report = restore.extract_verified(stream, self.manifest(), encoded, self.destination, durability=mode)
                self.assertIn(".manifest.json.partial", synced)
                self.assertEqual("world.db" in synced, mode == "per-file")
                if mode == "per-file":
                    self.assertNotIn("durability", report)
                    barrier.assert_not_called()
                else:
                    barrier.assert_called_once()

    def test_filesystem_mode_rejects_old_kernel_or_untrusted_parent_before_mkdir(self):
        for system, version in (("Linux", "5.7.19"), ("Darwin", "23.0"), ("Linux", "invalid")):
            with self.subTest(system=system, version=version), \
                 mock.patch.object(restore.platform, "system", return_value=system), \
                 mock.patch.object(restore.platform, "release", return_value=version), \
                 self.assertRaisesRegex(restore.RestoreError, "requires_linux_5_8"):
                with restore._durability(self.destination, "filesystem"):
                    self.fail("Unsupported barrier accepted")
            self.assertFalse(self.destination.exists())
        self.root.chmod(0o770)
        with mock.patch.object(restore.platform, "system", return_value="Linux"), \
             mock.patch.object(restore.platform, "release", return_value="6.8.0"), \
             self.assertRaisesRegex(restore.RestoreError, "parent_untrusted"):
            with restore._durability(self.destination, "filesystem"):
                self.fail("Untrusted parent accepted")
        self.assertFalse(self.destination.exists())

    def test_filesystem_barrier_fd_closes_on_early_extraction_error(self):
        barrier = mock.Mock(return_value=0)
        stream, encoded = self.archive(self.manifest(), contents={"data/world.db": b"corrupt contents!"})
        with mock.patch.object(restore.platform, "system", return_value="Linux"), \
             mock.patch.object(restore.platform, "release", return_value="6.8.0"), \
             mock.patch.object(restore.ctypes, "CDLL", return_value=mock.Mock(syncfs=barrier)):
            with self.assertRaisesRegex(restore.RestoreError, "archive_sha256_mismatch"):
                with restore._durability(self.destination, "filesystem") as descriptor:
                    restore._extract_verified(stream, self.manifest(), encoded, self.destination, descriptor)
        barrier.assert_not_called()
        with self.assertRaises(OSError):
            os.fstat(descriptor[0])

    def test_filesystem_barrier_rejects_destination_on_different_filesystem(self):
        barrier = mock.Mock(return_value=0)
        original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            if path == self.destination:
                return mock.Mock(st_dev=result.st_dev + 1)
            return result
        stream, encoded = self.archive(self.manifest())
        with mock.patch.object(restore.platform, "system", return_value="Linux"), \
             mock.patch.object(restore.platform, "release", return_value="6.8.0"), \
             mock.patch.object(restore.ctypes, "CDLL", return_value=mock.Mock(syncfs=barrier)), \
             mock.patch.object(Path, "lstat", lstat), \
             self.assertRaisesRegex(restore.RestoreError, "filesystem_durability_destination_changed"):
            restore.extract_verified(stream, self.manifest(), encoded, self.destination, durability="filesystem")
        barrier.assert_not_called()
        self.assertFalse((self.destination / "data").exists())
        self.assertFalse((self.destination / "restored.json").exists())


if __name__ == "__main__":
    unittest.main()
