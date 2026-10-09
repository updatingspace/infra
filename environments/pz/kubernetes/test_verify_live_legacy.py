"""Offline acceptance fixtures; never contact Docker, systemd or Kubernetes."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("verify_live", Path(__file__).with_name("verify-live.py"))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class RetiredLegacyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "docker" / "containers"
        self.path.mkdir(parents=True)
        self.units = {unit: {"LoadState": "loaded", "ActiveState": "inactive",
                            "SubState": "dead", "UnitFileState": "disabled"} for unit in verify.LEGACY_UNITS}
        self.units["k3s.service"] = {"LoadState": "loaded", "ActiveState": "active",
                                     "SubState": "running", "UnitFileState": "enabled"}
        self.commands = []

    def capture(self, argv, label, timeout=45):
        self.commands.append(argv)
        self.assertEqual(argv[:3], ["systemctl", "show", "--no-pager"])
        return "\n".join(key + "=" + value for key, value in self.units[argv[3]].items())

    def check_legacy(self):
        verification = verify.Verification(SimpleNamespace(legacy_retired=True))
        with patch.object(verify, "capture", side_effect=self.capture), patch.object(verify, "DOCKER_CONTAINERS", self.path):
            verification.section("legacy", verification.legacy)
        return verification.report

    def test_empty_and_absent_directories_pass_without_docker_cli(self):
        for present in (True, False):
            with self.subTest(present=present):
                if not present:
                    self.path.rmdir()
                report = self.check_legacy()
                self.assertEqual(report["failures"], [])
                self.assertEqual(report["legacy_mode"], "retired")
                self.assertEqual(report["legacy_container_directory"]["present"], present)
        self.assertEqual(len(self.commands), 8)
        self.assertTrue(all(command[0] == "systemctl" for command in self.commands))

    def test_each_active_legacy_unit_is_rejected(self):
        for unit in verify.LEGACY_UNITS:
            with self.subTest(unit=unit):
                self.units[unit]["ActiveState"] = "active"
                self.units[unit]["SubState"] = "running"
                self.assertIn(unit + ": inactive and disabled", self.check_legacy()["failures"])
                self.units[unit].update(ActiveState="inactive", SubState="dead")

    def test_enabled_socket_is_rejected_even_when_inactive(self):
        self.units["docker.socket"]["UnitFileState"] = "enabled"
        self.assertIn("docker.socket: inactive and disabled", self.check_legacy()["failures"])

    def test_unknown_or_incomplete_status_fails_closed(self):
        unit = self.units["docker.service"]
        for key, value in (("LoadState", "not-found"), ("ActiveState", "unknown"), ("UnitFileState", "")):
            with self.subTest(key=key):
                previous = unit[key]
                unit[key] = value
                self.assertTrue(self.check_legacy()["failures"])
                unit[key] = previous
        del unit["UnitFileState"]
        report = self.check_legacy()
        self.assertTrue(report["failures"])
        self.assertEqual(report["section_errors"], [{"section": "legacy", "type": "ValueError"}])

    def test_systemctl_error_fails_closed_without_exposing_stderr(self):
        verification = verify.Verification(SimpleNamespace(legacy_retired=True))
        with patch.object(verify, "capture", side_effect=verify.CommandFailure("systemctl show docker.service", "timeout")):
            verification.section("legacy", verification.legacy)
        self.assertEqual(verification.report["failures"], ["legacy: command succeeded"])
        self.assertEqual(verification.report["command_errors"][0]["result"], "timeout")

    def test_container_entry_is_rejected_without_listing_its_name(self):
        (self.path / "sensitive-container-id").mkdir()
        report = self.check_legacy()
        self.assertIn("legacy container directory: empty or absent", report["failures"])
        self.assertNotIn("sensitive-container-id", json.dumps(report))

    def test_file_or_symlink_is_rejected(self):
        self.path.rmdir()
        self.path.write_text("not a container directory")
        self.assertIn("legacy container directory: regular directory", self.check_legacy()["failures"])
        self.path.unlink()
        self.path.symlink_to(self.path.with_name("missing"))
        self.assertIn("legacy container directory: no symlink traversal", self.check_legacy()["failures"])

    def test_symlinked_parent_is_rejected(self):
        self.path.rmdir()
        self.path.parent.rmdir()
        target = self.path.parent.with_name("elsewhere")
        target.mkdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        self.assertIn("legacy container directory: no symlink traversal", self.check_legacy()["failures"])

    def test_stopped_k3s_is_rejected(self):
        self.units["k3s.service"].update(ActiveState="inactive", SubState="dead")
        self.assertIn("k3s.service: active and running", self.check_legacy()["failures"])

    def test_default_still_accounts_for_four_stopped_containers(self):
        verification = verify.Verification(SimpleNamespace())
        commands = []

        def capture(argv, label, timeout=45):
            commands.append(argv)
            if argv[:3] == ["docker", "ps", "-aq"]:
                return " ".join(verify.APPS)
            self.assertEqual(argv[:2], ["docker", "inspect"])
            return json.dumps({"service": argv[-1], "running": False, "paused": False,
                               "restarting": False, "status": "exited", "exit_code": 0})

        with patch.object(verify, "capture", side_effect=capture):
            verification.section("legacy", verification.legacy)
        self.assertEqual(verification.report["failures"], [])
        self.assertEqual(verification.report["legacy_mode"], "migration")
        self.assertEqual(len(verification.report["legacy_containers"]), 4)
        self.assertEqual(len(commands), 5)

    def test_cli_flag_selects_retired_mode(self):
        with patch.object(verify.sys, "argv", ["verify-live.py", "--legacy-retired"]), patch.object(verify, "Verification") as check:
            check.return_value.run.return_value = 0
            self.assertEqual(verify.main(), 0)
            self.assertTrue(check.call_args.args[0].legacy_retired)


if __name__ == "__main__":
    unittest.main()
