import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


module_spec = importlib.util.spec_from_file_location("pz_storage", Path(__file__).with_name("storage.py"))
storage = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(storage)


class StorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.writer_checks = {}
        for name in ("verify_docker_stopped", "verify_kubernetes_idle"):
            check = patch.object(storage, name)
            self.writer_checks[name] = check.start()
            self.addCleanup(check.stop)

    def test_fstab_is_idempotent_and_preserves_unmanaged_entries(self):
        original = "UUID=host-root / ext4 defaults 0 1\n"
        lines = ["/var/lib/pz-volumes/edge.ext4 /srv/pz-storage/edge ext4 loop,nodev,nosuid,noatime 0 0"]
        result = storage.managed_fstab(original, lines)
        self.assertTrue(result.startswith(original))
        self.assertEqual(storage.managed_fstab(result, lines), result)
        self.assertEqual(result.count(storage.BEGIN), 1)

    def test_broken_fstab_block_is_rejected(self):
        with self.assertRaises(RuntimeError):
            storage.managed_fstab(storage.BEGIN, [])

    def test_existing_image_with_wrong_size_is_never_formatted(self):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "edge.ext4"
            image.write_bytes(b"existing-data")
            with patch.object(storage, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "automatic resizing/formatting"):
                    storage.prepare_filesystem("edge", {"size_mib": 256}, {"image_root": root, "mount_root": root}, {"filesystems": {}}, lambda: None)
                command.assert_not_called()
            self.assertEqual(image.read_bytes(), b"existing-data")

    def migration_fixture(self, root, release):
        source_root = Path(root) / "source"
        mount = Path(root) / "mounted"
        source_root.mkdir()
        mount.mkdir()
        source = source_root / "world"
        source.mkdir()
        (source / "map.bin").write_bytes(b"saved-world")
        spec = {"source_root": str(source_root), "image_root": root, "minimum_host_free_mib": 2048, "release_verified_sources": release}
        journal = {"directories": {}, "filesystems": {}}
        def save():
            (Path(root) / "journal.json").write_text(json.dumps(journal))
        return source, mount, spec, journal, save

    def test_copy_mismatch_preserves_original_directory(self):
        with tempfile.TemporaryDirectory() as root:
            source, mount, spec, journal, save = self.migration_fixture(root, True)
            with patch.object(storage, "verify_mount"), patch.object(storage, "capture", return_value="16 world"), patch.object(storage, "run"), patch.object(storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)), patch.object(storage, "verify_copy", side_effect=RuntimeError("checksum differs")):
                with self.assertRaisesRegex(RuntimeError, "checksum differs"):
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
            self.assertFalse(source.is_symlink())
            self.assertEqual((source / "map.bin").read_bytes(), b"saved-world")
            self.assertFalse(source.with_name("world.pre-k3s").exists())
            self.assertEqual(journal["directories"]["world"]["status"], "copying")

    def test_verified_migration_retains_original_unless_release_enabled(self):
        for release in (False, True):
            with self.subTest(release=release), tempfile.TemporaryDirectory() as root:
                source, mount, spec, journal, save = self.migration_fixture(root, release)
                def copy(argv, **kwargs):
                    shutil.copytree(source, mount / "world", dirs_exist_ok=True)
                with patch.object(storage, "verify_mount"), patch.object(storage, "capture", return_value="16 world"), patch.object(storage, "run", side_effect=copy), patch.object(storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)), patch.object(storage, "verify_copy") as comparison, patch.object(storage.os, "sync"):
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
                self.assertTrue(source.is_symlink())
                self.assertEqual((source / "map.bin").read_bytes(), b"saved-world")
                self.assertEqual(source.with_name("world.pre-k3s").exists(), not release)
                self.assertEqual(journal["directories"]["world"]["status"], "released" if release else "linked")
                comparison.assert_called_once_with(source, mount / "world")
                self.writer_checks["verify_docker_stopped"].assert_called()
                self.writer_checks["verify_kubernetes_idle"].assert_called()

    def test_resumed_migration_rechecks_retired_source_once_before_release(self):
        for already_linked in (False, True):
            with self.subTest(already_linked=already_linked), tempfile.TemporaryDirectory() as root:
                source, mount, spec, journal, save = self.migration_fixture(root, True)
                shutil.copytree(source, mount / "world")
                retired = source.with_name("world.pre-k3s")
                source.rename(retired)
                if already_linked:
                    source.symlink_to(mount / "world")
                journal["directories"]["world"] = {"backup_sha256": "backup-sha", "status": "linked" if already_linked else "verified"}
                with patch.object(storage, "verify_mount"), patch.object(storage, "verify_copy") as comparison:
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
                comparison.assert_called_once_with(retired, mount / "world")
                self.assertEqual(journal["directories"]["world"]["status"], "released")
                self.assertEqual((source / "map.bin").read_bytes(), b"saved-world")
                self.assertFalse(retired.exists())

    def test_resumed_linked_mismatch_keeps_retired_source(self):
        with tempfile.TemporaryDirectory() as root:
            source, mount, spec, journal, save = self.migration_fixture(root, True)
            shutil.copytree(source, mount / "world")
            retired = source.with_name("world.pre-k3s")
            source.rename(retired)
            source.symlink_to(mount / "world")
            journal["directories"]["world"] = {"backup_sha256": "backup-sha", "status": "linked"}
            with patch.object(storage, "verify_mount"), patch.object(storage, "verify_copy", side_effect=RuntimeError("checksum differs")) as comparison, patch.object(storage.shutil, "rmtree") as delete:
                with self.assertRaisesRegex(RuntimeError, "checksum differs"):
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
                comparison.assert_called_once_with(retired, mount / "world")
                delete.assert_not_called()
            self.assertEqual((retired / "map.bin").read_bytes(), b"saved-world")

    def test_replaced_inode_after_rename_cannot_reuse_verification(self):
        with tempfile.TemporaryDirectory() as root:
            source, mount, spec, journal, save = self.migration_fixture(root, True)
            real_rename = storage.os.rename
            def replace_after_rename(old, new):
                real_rename(old, new)
                real_rename(new, new.with_name("original-preserved"))
                new.mkdir()
            def copy(argv, **kwargs):
                shutil.copytree(source, mount / "world", dirs_exist_ok=True)
            with patch.object(storage, "verify_mount"), patch.object(storage, "capture", return_value="16 world"), patch.object(storage, "run", side_effect=copy), patch.object(storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)), patch.object(storage, "verify_copy") as comparison, patch.object(storage.os, "sync"), patch.object(storage.os, "rename", side_effect=replace_after_rename), patch.object(storage.shutil, "rmtree") as delete:
                with self.assertRaisesRegex(RuntimeError, "Verified directory identity changed"):
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
                comparison.assert_called_once()
                delete.assert_not_called()

    def test_active_writer_blocks_comparison_and_release(self):
        with tempfile.TemporaryDirectory() as root:
            source, mount, spec, journal, save = self.migration_fixture(root, True)
            self.writer_checks["verify_docker_stopped"].side_effect = RuntimeError("Compose service is active")
            def copy(argv, **kwargs):
                shutil.copytree(source, mount / "world", dirs_exist_ok=True)
            with patch.object(storage, "verify_mount"), patch.object(storage, "capture", return_value="16 world"), patch.object(storage, "run", side_effect=copy), patch.object(storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)), patch.object(storage, "verify_copy") as comparison, patch.object(storage.shutil, "rmtree") as delete:
                with self.assertRaisesRegex(RuntimeError, "Compose service is active"):
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
                comparison.assert_not_called()
                delete.assert_not_called()
            self.assertFalse(source.is_symlink())
            self.assertEqual((source / "map.bin").read_bytes(), b"saved-world")

    def test_interrupted_partial_release_requires_review(self):
        with tempfile.TemporaryDirectory() as root:
            source, mount, spec, journal, save = self.migration_fixture(root, True)
            shutil.copytree(source, mount / "world")
            source.rename(source.with_name("world.pre-k3s"))
            source.symlink_to(mount / "world")
            journal["directories"]["world"] = {"backup_sha256": "backup-sha", "status": "releasing"}
            with patch.object(storage, "verify_mount"), patch.object(storage.shutil, "rmtree") as delete:
                with self.assertRaisesRegex(RuntimeError, "release was interrupted"):
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
                delete.assert_not_called()

    def test_insufficient_host_space_blocks_copy(self):
        with tempfile.TemporaryDirectory() as root:
            source, mount, spec, journal, save = self.migration_fixture(root, True)
            with patch.object(storage, "verify_mount"), patch.object(storage, "capture", return_value="16 world"), patch.object(storage.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(2048 * storage.MIB, 0, 2048 * storage.MIB)), patch.object(storage, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "physical host free space"):
                    storage.migrate_directory("world", (Path(root) / "image", mount, "uuid"), spec, journal, save, "backup-sha")
                command.assert_not_called()
            self.assertFalse(source.is_symlink())


class PersistentJobTests(unittest.TestCase):
    def properties(self, active="active", substate="running", code="0", status="0"):
        return {"LoadState": "loaded", "ActiveState": active, "SubState": substate, "ExecMainCode": code, "ExecMainStatus": status, "MainPID": "0" if substate == "exited" else "123", "Result": "success"}

    def test_reconnect_uses_one_unit_and_private_immutable_job_files(self):
        with tempfile.TemporaryDirectory() as root:
            spec = {"expected_host": "fixture-host", "image_root": root, "filesystems": {"test": {"directories": ["world"]}}}
            payload = {"spec": spec, "script": "print('fixture worker')\n"}
            missing = {"LoadState": "not-found", "ActiveState": "inactive"}
            with patch.object(storage, "validate_spec"), patch.object(storage.os, "geteuid", return_value=0), patch.object(storage, "capture", return_value="fixture-host"), patch.object(storage, "unit_properties", side_effect=[missing, self.properties(), self.properties()]), patch.object(storage.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as launch:
                first = storage.ensure_persistent_job(payload)
                second = storage.ensure_persistent_job(payload)
            self.assertEqual(first["state"], "running")
            self.assertEqual(first, second)
            launch.assert_called_once()
            command = launch.call_args.args[0]
            self.assertIn("systemd-run", command)
            self.assertIn("--remain-after-exit", command)
            self.assertIn("--no-block", command)
            self.assertNotIn("--scope", command)
            self.assertNotIn("--pipe", command)
            for name in ("storage.py", "spec.json", "identity.json"):
                self.assertEqual((Path(root) / "job" / name).stat().st_mode & 0o777, 0o600)

    def test_success_requires_journal_and_result_and_systemd_exit_zero(self):
        spec = {"filesystems": {"test": {"directories": ["world"]}}}
        identity = storage.job_identity(spec, "fixture")
        journal = {"completed_at": "2026-10-01T12:00:00+00:00", "directories": {"world": {"status": "released"}}}
        result = dict(identity, ok=True, exit_code=0, completed_at=journal["completed_at"])
        success = self.properties(substate="exited", code="1")
        self.assertEqual(storage.job_status(success, identity, journal, result, spec)["state"], "succeeded")
        for properties, changed_journal, changed_result in (
            (success, {}, result),
            (success, journal, {}),
            (success, journal, dict(result, completed_at="stale")),
            (success, journal, dict(result, script_sha256="different")),
            (self.properties(substate="exited", code="1", status="1"), journal, result),
        ):
            with self.subTest(properties=properties, result=changed_result):
                self.assertEqual(storage.job_status(properties, identity, changed_journal, changed_result, spec)["state"], "failed")
        self.assertEqual(storage.job_status(self.properties(), identity, journal, result, spec)["state"], "running")

    def test_failed_unit_is_reported_without_automatic_restart(self):
        with tempfile.TemporaryDirectory() as root:
            spec = {"expected_host": "fixture-host", "image_root": root, "filesystems": {"test": {"directories": ["world"]}}}
            script = "print('fixture worker')\n"
            job = Path(root) / "job"
            job.mkdir()
            identity = storage.job_identity(spec, script)
            (job / "identity.json").write_text(storage.canonical_json(identity) + "\n")
            failed = dict(self.properties(active="failed", substate="failed", code="1", status="1"), Result="exit-code")
            with patch.object(storage, "validate_spec"), patch.object(storage.os, "geteuid", return_value=0), patch.object(storage, "capture", return_value="fixture-host"), patch.object(storage, "unit_properties", return_value=failed), patch.object(storage.subprocess, "run") as launch:
                status = storage.ensure_persistent_job({"spec": spec, "script": script})
            self.assertEqual(status["state"], "failed")
            launch.assert_not_called()

    def test_missing_unit_with_saved_result_never_relaunches(self):
        with tempfile.TemporaryDirectory() as root:
            spec = {"expected_host": "fixture-host", "image_root": root, "filesystems": {"test": {"directories": ["world"]}}}
            script = "print('fixture worker')\n"
            job = Path(root) / "job"
            job.mkdir()
            (job / "identity.json").write_text(storage.canonical_json(storage.job_identity(spec, script)) + "\n")
            (job / "result.json").write_text('{"ok":true}')
            with patch.object(storage, "validate_spec"), patch.object(storage.os, "geteuid", return_value=0), patch.object(storage, "capture", return_value="fixture-host"), patch.object(storage, "unit_properties", return_value={"LoadState": "not-found"}), patch.object(storage.subprocess, "run") as launch:
                with self.assertRaisesRegex(RuntimeError, "systemd unit is missing"):
                    storage.ensure_persistent_job({"spec": spec, "script": script})
            launch.assert_not_called()

    def test_progress_does_not_forward_paths_or_unknown_data(self):
        spec = {"filesystems": {"test": {"directories": ["world"]}}}
        journal = {"directories": {"world": {"status": "copying", "secret": "not-for-output"}, "unrecognized": {"status": "copying"}}}
        self.assertEqual(storage.safe_progress(journal, spec), {"world": "copying"})

    def test_reconnect_waits_and_returns_only_confirmed_success(self):
        running = {"unit": storage.JOB_UNIT, "state": "running", "phase": "copying-or-verifying", "directories": {}}
        success = dict(running, state="succeeded", phase="complete")
        with patch.object(storage, "request_job_status", side_effect=[storage.SSHTransportError("lost launch acknowledgment"), running, success]) as request, patch.object(storage.time, "sleep"):
            result = storage.wait_persistent_job({}, "fixture", "host", "key", timeout=60)
        self.assertEqual(result, success)
        self.assertEqual(request.call_count, 3)
        for call in request.call_args_list:
            self.assertEqual(call.args, ({}, "fixture", "host", "key"))

    def test_ssh_retries_are_bounded_without_stopping_server_job(self):
        with patch.object(storage, "MAX_SSH_FAILURES", 1), patch.object(storage, "request_job_status", side_effect=storage.SSHTransportError("down")) as request, patch.object(storage.time, "sleep"), patch.object(storage.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "persistent storage job was not stopped"):
                storage.wait_persistent_job({}, "fixture", "host", "key", timeout=60)
            self.assertEqual(request.call_count, 2)
            command.assert_not_called()

    def test_overall_timeout_does_not_kill_or_restart_server_job(self):
        now = [0]
        def advance(seconds):
            now[0] += seconds
        running = {"unit": storage.JOB_UNIT, "state": "running", "phase": "copying-or-verifying", "directories": {}}
        with patch.object(storage.time, "monotonic", side_effect=lambda: now[0]), patch.object(storage.time, "sleep", side_effect=advance), patch.object(storage, "request_job_status", return_value=running) as request, patch.object(storage.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "persistent job was not stopped"):
                storage.wait_persistent_job({}, "fixture", "host", "key", timeout=10)
            request.assert_called_once()
            command.assert_not_called()
        self.assertEqual(now[0], 10)

    def test_connection_failure_never_prints_remote_stderr(self):
        completed = subprocess.CompletedProcess([], 255, "", "sensitive remote diagnostic")
        with patch.object(storage.subprocess, "run", return_value=completed):
            with self.assertRaises(storage.SSHTransportError) as raised:
                storage.request_job_status({}, "fixture", "host", "key")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_busy_remote_systemd_status_is_retryable(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"unit": storage.JOB_UNIT, "controller_retry": True}), "")
        with patch.object(storage.subprocess, "run", return_value=completed):
            with self.assertRaises(storage.SSHTransportError):
                storage.request_job_status({}, "fixture", "host", "key")


if __name__ == "__main__":
    unittest.main()
