"""Offline checks that deployment sync never bundles updater state or credentials."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("deploy_sync", Path(__file__).with_name("deploy.py"))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentSyncTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for stage in deploy.STAGES:
            directory = self.root / stage
            directory.mkdir()
            (directory / "main.tf").write_text("# fixture")
            (directory / "terraform.tfstate").write_text("private-state-fixture")
            (directory / "terraform.tfvars").write_text("private-variable-fixture")
        updater = self.root / "panel-updater"
        updater.mkdir()
        self.source = updater / "updater.py"
        self.source.write_text("# exact updater fixture")
        (updater / "state.json").write_text("private-update-state-fixture")
        (updater / "token").write_text("private-token-fixture")
        (updater / "backup.sqlite").write_text("private-backup-fixture")
        telemetry = self.root / "panel-telemetry"
        telemetry.mkdir()
        for name in deploy.TELEMETRY_MODULES:
            (telemetry / name).write_text("// runtime fixture")

    def sync(self):
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy, "ssh") as ssh:
            with mock.patch("sys.argv", ["deploy.py", "sync"]):
                deploy.main()
            return ssh

    def test_bundles_exact_script_without_private_neighbor_files(self):
        ssh = self.sync()
        self.assertEqual(ssh.call_count, 2)
        payload = ssh.call_args_list[1].kwargs["input"]
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {
                "platform/main.tf", "workloads/main.tf", "observability/main.tf",
                "panel-updater/updater.py",
            } | {"panel-telemetry/" + name for name in deploy.TELEMETRY_MODULES})
            self.assertEqual(archive.extractfile("panel-updater/updater.py").read(), self.source.read_bytes())

    def test_missing_source_fails_before_any_remote_action(self):
        self.source.unlink()
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy, "ssh") as ssh:
            with mock.patch("sys.argv", ["deploy.py", "sync"]), self.assertRaisesRegex(RuntimeError, "regular panel-updater/updater.py"):
                deploy.main()
            ssh.assert_not_called()

    def test_telemetry_sync_includes_only_runtime_modules(self):
        directory = self.root / "panel-telemetry"
        (directory / "provider.test.mjs").write_text("// test fixture")
        (directory / "test-provider.mjs").write_text("// test fixture")
        (directory / "token").write_text("private-token-fixture")
        (directory / "runtime.json").write_text("private-state-fixture")
        payload = self.sync().call_args_list[1].kwargs["input"]
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            names = {name for name in archive.getnames() if name.startswith("panel-telemetry/")}
            self.assertEqual(names, {"panel-telemetry/" + name for name in deploy.TELEMETRY_MODULES})

    def test_telemetry_symlink_cannot_export_private_file(self):
        directory = self.root / "panel-telemetry"
        (directory / "preload.mjs").unlink()
        (directory / "preload.mjs").symlink_to(self.source.with_name("token"))
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy, "ssh") as ssh:
            with mock.patch("sys.argv", ["deploy.py", "sync"]), self.assertRaisesRegex(RuntimeError, "regular panel telemetry"):
                deploy.main()
            ssh.assert_not_called()

    def test_symlink_source_fails_before_any_remote_action(self):
        self.source.unlink()
        self.source.symlink_to(self.source.with_name("token"))
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy, "ssh") as ssh:
            with mock.patch("sys.argv", ["deploy.py", "sync"]), self.assertRaisesRegex(RuntimeError, "regular panel-updater/updater.py"):
                deploy.main()
            ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
