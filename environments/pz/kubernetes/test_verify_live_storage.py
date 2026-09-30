"""Offline storage acceptance fixtures; never call host/VM utilities."""
import copy
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("verify_live_storage", Path(__file__).with_name("verify-live.py"))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
OLD_UUID = "11111111-1111-1111-1111-111111111111"
NEW_UUID = "22222222-2222-2222-2222-222222222222"
DISK_ID = "f" * 20


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.journal = {"format": "pz-disk-migration-v1", "phase": "complete", "old_uuid": OLD_UUID,
                        "new_uuid": NEW_UUID, "disk_id": DISK_ID,
                        "old_copy_path": str(verify.OLD_DATA_MOUNT), "completed_at": "2000-01-01T00:00:00Z"}
        self.config = {"data_root": str(verify.DATA_MOUNT), "data_uuid": NEW_UUID}
        self.rows = {}
        self.images = {}
        self.uuids = {}
        for index, (name, fs) in enumerate(verify.FILESYSTEMS.items()):
            image = "/var/lib/pz-volumes/" + name + ".ext4"
            mount = "/srv/pz-storage/" + name
            uuid = OLD_UUID if name == "zomboid" else str(index) * 8 + "-1111-1111-1111-111111111111"
            self.images[image] = fs["size_mib"] * verify.MIB
            self.uuids[image] = uuid
            self.rows[mount] = {"source": "/dev/loop" + str(index), "fstype": "ext4", "uuid": uuid,
                                "target": mount, "options": "rw,nodev,nosuid,noatime", "fs-options": "rw"}
        self.loops = [{"name": "/dev/loop7", "back-file": "/var/lib/pz-volumes/zomboid.ext4",
                       "offset": 0, "sizelimit": 0, "ro": True}]
        self.disk = {"path": "/dev/vdb", "type": "disk", "serial": DISK_ID,
                     "size": 40 * verify.GIB, "fstype": "ext4"}
        self.uuids["/dev/vdb"] = NEW_UUID
        self.commands = []
        self.migrated = False
        self.image_mode = stat.S_IFREG | 0o600

    def dedicated(self):
        self.migrated = True
        self.rows[str(verify.OLD_DATA_MOUNT)] = {**self.rows[str(verify.DATA_MOUNT)],
            "source": "/dev/loop7", "target": str(verify.OLD_DATA_MOUNT),
            "options": "ro,nodev,nosuid,noatime", "fs-options": "ro"}
        self.rows[str(verify.DATA_MOUNT)].update(source="/dev/vdb", uuid=NEW_UUID)

    def capture(self, argv, label, timeout=45, empty_status=None):
        self.commands.append(argv)
        if argv[0] == "findmnt":
            if argv[3] not in self.rows:
                self.assertEqual(empty_status, 1)
                return ""
            return json.dumps({"filesystems": [self.rows[argv[3]]]})
        if argv[0] == "lsblk":
            return json.dumps({"blockdevices": [self.disk]})
        if argv[:2] == ["losetup", "--json"]:
            return json.dumps({"loopdevices": self.loops})
        if argv[0] == "losetup":
            for mount, row in self.rows.items():
                if row["source"] == argv[-1] and row["source"].startswith("/dev/loop"):
                    return "/var/lib/pz-volumes/" + Path(mount).name + ".ext4"
            raise verify.CommandFailure("losetup", 1)
        if argv[0] == "blkid":
            return self.uuids[argv[-1]]
        self.fail("Unexpected external command: " + argv[0])

    def check_storage(self):
        def private(path):
            return copy.deepcopy(self.journal if path == verify.DISK_MIGRATION_JOURNAL else self.config)

        def resolve(path, strict=False):
            if str(path) == "/dev/disk/by-id/virtio-" + DISK_ID:
                return Path("/dev/vdb")
            if str(path).startswith("/opt/pz-stack/data/"):
                name = path.name
                owner = next(key for key, value in verify.FILESYSTEMS.items() if name in value["directories"])
                return Path("/srv/pz-storage") / owner / name
            return path

        def image_stat(path):
            return SimpleNamespace(st_mode=self.image_mode, st_size=self.images[str(path)], st_blocks=1024,
                                   st_uid=0, st_nlink=1)

        check = verify.Verification(SimpleNamespace(minimum_root_free_mib=2048))
        with patch.object(verify.os.path, "lexists", return_value=self.migrated), \
                patch.object(verify, "trusted_private_json", side_effect=private), \
                patch.object(verify, "capture", side_effect=self.capture), \
                patch.object(Path, "lstat", image_stat), \
                patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK | 0o600)), \
                patch.object(Path, "resolve", resolve), \
                patch.object(Path, "is_symlink", return_value=True), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(verify.os, "statvfs", return_value=SimpleNamespace(f_bavail=5*1024**2, f_frsize=1024, f_blocks=25*1024**2)):
            check.section("filesystems", check.filesystems)
        return check.report

    def test_original_three_loops_keep_all_checks(self):
        report = self.check_storage()
        self.assertEqual(report["failures"], [])
        self.assertEqual([row["layout"] for row in report["filesystems"]], ["legacy-loop"] * 3)
        self.assertEqual(sum(command[0] == "losetup" for command in self.commands), 3)
        self.assertFalse(any(command[0] == "lsblk" for command in self.commands))

    def test_complete_cutover_preserves_edge_and_observability_checks(self):
        self.dedicated()
        report = self.check_storage()
        self.assertEqual(report["failures"], [])
        self.assertEqual([row["layout"] for row in report["filesystems"]], ["dedicated-disk", "legacy-loop", "legacy-loop"])
        self.assertEqual(report["filesystems"][0]["disk_id"], DISK_ID)
        self.assertEqual(report["filesystems"][0]["old_copy_state"], "mounted-read-only")
        for name in ("edge", "observability"):
            self.assertTrue(any(c["name"] == name + ": mounted loop uses exact backing image" and c["passed"] for c in report["checks"]))

    def test_dedicated_disk_without_journal_cannot_fall_back_to_success(self):
        self.dedicated()
        self.migrated = False
        self.assertTrue(self.check_storage()["failures"])

    def test_partial_and_mismatched_evidence_fail_before_device_probes(self):
        self.dedicated()
        for section, key, value in ((self.journal, "phase", "new_disk_mounted"),
                                    (self.journal, "old_copy_path", "/unexpected"),
                                    (self.journal, "new_uuid", "not-a-uuid"),
                                    (self.journal, "old_uuid", NEW_UUID),
                                    (self.journal, "disk_id", "../../wrong"),
                                    (self.journal, "completed_at", "2999-01-01T00:00:00Z"),
                                    (self.config, "data_uuid", OLD_UUID),
                                    (self.config, "data_root", "/unexpected")):
            with self.subTest(key=key, value=value):
                before = section[key]
                section[key] = value
                self.commands.clear()
                self.assertTrue(self.check_storage()["failures"])
                self.assertEqual(self.commands, [])
                section[key] = before

    def test_wrong_disk_serial_partition_and_mount_uuid_are_rejected(self):
        self.dedicated()
        for section, key, value in ((self.disk, "serial", "wrong"), (self.disk, "type", "part"),
                                    (self.disk, "children", [{"type": "part"}]),
                                    (self.rows[str(verify.DATA_MOUNT)], "uuid", OLD_UUID),
                                    (self.rows[str(verify.DATA_MOUNT)], "source", "/dev/vdc")):
            with self.subTest(key=key):
                before = section.get(key)
                section[key] = value
                self.assertTrue(self.check_storage()["failures"])
                if before is None:
                    del section[key]
                else:
                    section[key] = before

    def test_read_only_bind_with_writable_old_superblock_is_rejected(self):
        self.dedicated()
        self.rows[str(verify.OLD_DATA_MOUNT)]["fs-options"] = "rw"
        self.assertTrue(self.check_storage()["failures"])

    def test_preserved_image_may_be_unmounted_after_reboot(self):
        self.dedicated()
        del self.rows[str(verify.OLD_DATA_MOUNT)]
        self.loops = []
        report = self.check_storage()
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["filesystems"][0]["old_copy_state"], "unmounted-preserved")
        self.assertIsNone(report["filesystems"][0]["old_mount"])
        self.assertTrue(any(command[0] == "blkid" and command[-1] == "/var/lib/pz-volumes/zomboid.ext4" for command in self.commands))

    def test_unmounted_image_with_any_attached_loop_is_rejected(self):
        self.dedicated()
        del self.rows[str(verify.OLD_DATA_MOUNT)]
        for ro in (False, True):
            with self.subTest(ro=ro):
                self.loops[0]["ro"] = ro
                self.assertIn("zomboid: unmounted preserved image has no attached loops", self.check_storage()["failures"])

    def test_unmounted_preserved_image_still_requires_exact_original_uuid(self):
        self.dedicated()
        del self.rows[str(verify.OLD_DATA_MOUNT)]
        self.loops = []
        self.uuids["/var/lib/pz-volumes/zomboid.ext4"] = NEW_UUID
        self.assertIn("zomboid: preserved image UUID matches original migration source", self.check_storage()["failures"])

    def test_unmounted_original_image_must_remain_protected(self):
        self.dedicated()
        del self.rows[str(verify.OLD_DATA_MOUNT)]
        self.loops = []
        self.image_mode = stat.S_IFREG | 0o660
        self.assertIn("zomboid: preserved image remains root-owned and protected", self.check_storage()["failures"])

    def test_foreign_mount_at_old_path_is_not_treated_as_unmounted(self):
        self.dedicated()
        self.loops = []
        self.rows[str(verify.OLD_DATA_MOUNT)].update(source="/dev/vdc", uuid=NEW_UUID)
        self.assertIn("zomboid: preserved old ext4 loop is read-only", self.check_storage()["failures"])

    def test_mismatched_device_uuid_and_read_only_new_superblock_are_rejected(self):
        self.dedicated()
        self.uuids["/dev/vdb"] = OLD_UUID
        self.assertTrue(self.check_storage()["failures"])
        self.uuids["/dev/vdb"] = NEW_UUID
        self.rows[str(verify.DATA_MOUNT)]["fs-options"] = "ro"
        self.assertTrue(self.check_storage()["failures"])

    def test_writable_or_wrong_old_backing_loop_is_rejected(self):
        self.dedicated()
        for key, value in (("ro", False), ("back-file", "/unrelated"), ("offset", 4096)):
            with self.subTest(key=key):
                before = self.loops[0][key]
                self.loops[0][key] = value
                self.assertTrue(self.check_storage()["failures"])
                self.loops[0][key] = before

    def test_common_options_and_unchanged_loop_identity_still_fail(self):
        self.dedicated()
        for name in ("zomboid", "edge", "observability"):
            with self.subTest(name=name):
                row = self.rows["/srv/pz-storage/" + name]
                previous = row["options"]
                row["options"] = "rw"
                self.assertTrue(self.check_storage()["failures"])
                row["options"] = previous
        self.rows["/srv/pz-storage/edge"]["uuid"] = NEW_UUID
        self.assertTrue(self.check_storage()["failures"])

    def test_changed_evidence_during_verification_is_rejected(self):
        self.dedicated()
        calls = 0
        original = self.capture
        def capture(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.config["new_unrelated_setting"] = True
            return original(*args, **kwargs)
        with patch.object(self, "capture", side_effect=capture):
            self.assertIn("disk migration evidence remained unchanged during verification", self.check_storage()["failures"])


class TrustedEvidenceTests(unittest.TestCase):
    def test_only_empty_findmnt_absence_is_allowed(self):
        for code, stdout, stderr, accepted in ((1, "", "", True), (1, "", "permission denied", False),
                                               (1, "partial", "", False), (2, "", "", False)):
            with self.subTest(code=code, stdout=stdout, stderr=stderr), patch.object(verify.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, stderr)):
                if accepted:
                    self.assertEqual(verify.capture(["findmnt"], "findmnt old data", empty_status=1), "")
                else:
                    with self.assertRaises(verify.CommandFailure):
                        verify.capture(["findmnt"], "findmnt old data", empty_status=1)

    def test_private_reader_rejects_nonprivate_mode_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            path.write_text('{"phase":"complete"}')
            original_fstat = verify.os.fstat
            def owned_fstat(fd):
                info = original_fstat(fd)
                return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink, st_size=info.st_size)
            trusted_parent = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
            with patch.object(Path, "lstat", return_value=trusted_parent), patch.object(verify.os, "fstat", side_effect=owned_fstat):
                path.chmod(0o600)
                self.assertEqual(verify.trusted_private_json(path), {"phase": "complete"})
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    verify.trusted_private_json(path)
                link = path.with_name("link")
                link.symlink_to(path)
                with self.assertRaises(OSError):
                    verify.trusted_private_json(link)

    def test_evidence_parent_must_be_root_owned_and_not_writable(self):
        for mode, uid in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFDIR | 0o777, 0), (stat.S_IFDIR | 0o755, 1000)):
            with self.subTest(mode=mode, uid=uid), patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=uid)):
                with self.assertRaises(ValueError):
                    verify.trusted_private_json(Path("/private/journal.json"))


if __name__ == "__main__":
    unittest.main()
