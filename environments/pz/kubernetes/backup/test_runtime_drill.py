"""Offline Docker-boundary and restore-evidence tests; no Docker daemon used."""

import ast
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

import remote

SPEC = importlib.util.spec_from_file_location("runtime_drill", Path(__file__).with_name("runtime-drill.py"))
drill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drill)


class ReadWatch:
    def __init__(self, paths):
        self.paths = paths

    def poll(self):
        return True

    def close(self):
        pass


class FakeDocker:
    def __init__(self, root, image):
        self.root, self.image = root, image
        self.calls = []
        self.document = None
        self.bad_mount = False
        self.bad_image = False
        self.oom = False
        self.health_ok = True
        self.fail_load = False
        self.env_seen = None

    def run(self, args, **kwargs):
        self.calls.append(args)
        output, code = b"", 0
        if args[:1] == ["info"]:
            output = json.dumps({"OSType": "linux", "Architecture": "x86_64", "MemTotal": 12 * drill.GIB}).encode()
        elif args[:2] == ["image", "load"]:
            if self.fail_load:
                raise drill.DrillError("docker_command_failed")
        elif args[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": "sha256:" + "b" * 64 if self.bad_image else self.image}]).encode()
        elif args[0] == "create":
            identifier = args[args.index("--label") + 1].split("=", 1)[1]
            self.env_seen = Path(args[args.index("--env-file") + 1])
            assert self.env_seen.stat().st_mode & 0o777 == 0o600
            self.document = {
                "Id": "a" * 64, "Name": "/pz-restore-drill-" + identifier, "Image": self.image,
                "Config": {"User": "1000:1000", "Labels": {drill.LABEL: identifier}},
                "HostConfig": {"NetworkMode": "none", "PortBindings": {}, "PublishAllPorts": False,
                               "Privileged": False, "PidMode": "", "IpcMode": "private", "CapDrop": ["ALL"],
                               "CapAdd": None, "SecurityOpt": ["no-new-privileges"], "Memory": 10 * drill.GIB,
                               "MemorySwap": 10 * drill.GIB, "NanoCpus": 2_000_000_000},
                "Mounts": [{"Type": "bind", "Source": str(self.root / "data" / relative), "Destination": target}
                           for relative, target in drill.MOUNTS.items()],
                "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
            }
            if self.bad_mount:
                self.document["Mounts"][0]["Source"] = "/srv/pz-storage/zomboid/pz-server"
            output = b"a" * 64
        elif args[:2] == ["container", "start"]:
            assert not self.env_seen.exists()
            self.document["State"]["Running"] = True
        elif args[:2] == ["container", "exec"]:
            if not self.health_ok:
                self.document["State"].update(Running=False, ExitCode=1)
                code = 1
        elif args[:2] == ["container", "kill"]:
            assert args[2:4] == ["--signal", "SIGTERM"]
            self.document["State"].update(Running=False, ExitCode=137 if self.oom else 0, OOMKilled=self.oom)
        elif args[:2] == ["container", "rm"]:
            assert self.document["State"]["Running"] is False and args[-1] == self.document["Id"]
        elif args[:2] == ["container", "logs"]:
            output = b"private runtime diagnostics"
        else:
            raise AssertionError("Unexpected Docker command: " + repr(args))
        return subprocess.CompletedProcess(args, code, stdout=output)

    def inspect(self, container_id):
        assert container_id == self.document["Id"]
        return copy.deepcopy(self.document)


class RuntimeDrillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in drill.MOUNTS:
            (self.root / "data" / relative).mkdir(parents=True)
        (self.root / "recovery").mkdir()
        self.manifest = {"snapshot_id": "20260101T000000Z-" + "0" * 32, "server_name": "world"}
        self.archive_report = {"commit_sha256": "f" * 64}
        self.image = "sha256:" + "c" * 64
        self.engine = FakeDocker(self.root, self.image)
        self.secret = {"PZ_ADMIN_PASSWORD": "test-admin-secret", "RCON_PASSWORD": "test-rcon-secret", "PZ_SERVER_NAME": "world"}
        self.set_config()

    def set_config(self, mods="", workshop=""):
        directory = self.root / "data/zomboid/Server"
        directory.mkdir(exist_ok=True)
        (directory / "world.ini").write_text("RCONEnabled=true\nRCONPassword=test-rcon-secret\nRCONPort=27015\nMods=" + mods + "\nWorkshopItems=" + workshop + "\n")
        world = self.root / "data/zomboid/Saves/Multiplayer/world"
        world.mkdir(parents=True, exist_ok=True)
        for name in drill.WORLD_FILES:
            (world / name).write_bytes(b"existing-world-" + name.encode())
        resources = {"items": [{"kind": "Secret", "metadata": {"name": "pz-runtime"},
                                  "data": {key: base64.b64encode(value.encode()).decode() for key, value in self.secret.items()}}]}
        remote._atomic_json(self.root / "recovery/zomboid-resources.json", resources)

    def run_fake(self):
        with mock.patch.object(drill, "validated_root", return_value=(self.root, self.manifest, self.archive_report)), \
             mock.patch.object(drill, "archived_image", return_value=(self.root / "recovery/runtime-images.oci.tar", self.image)), \
             mock.patch.object(drill, "prepare_offline_mods", return_value={"configured_mods": ["ExampleMod"]}), \
             mock.patch.object(drill, "WorldReadWatch", ReadWatch), mock.patch.object(drill.time, "sleep"), \
             mock.patch.object(drill.os, "statvfs", return_value=type("Space", (), {"f_bavail": 10 * drill.GIB, "f_frsize": 1})()):
            return drill.run_drill(self.root, startup_timeout=60, docker=self.engine)

    def test_offline_success_requires_all_evidence_and_never_writes_attestation(self):
        result = self.run_fake()
        self.assertTrue(result["passed"], result)
        self.assertTrue(all(result["checks"].values()))
        self.assertFalse(result["retention_attestation_created"])
        self.assertEqual(result["format"], "pz-offline-runtime-drill-v1")
        self.assertFalse((self.root / "restore-attestation.json").exists())
        self.assertTrue(result["container_removed"])
        self.assertEqual((self.root / "runtime-drill.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob("*.env")))
        self.assertFalse(list((self.root / "data/pz-server").glob(".pz-restore-drill-*")))
        commands = json.dumps(self.engine.calls)
        self.assertNotIn("test-admin-secret", commands)
        self.assertNotIn("test-rcon-secret", commands)
        self.assertNotIn("test-admin-secret", (self.root / "runtime-drill.json").read_text())
        self.assertFalse(any(command[:2] in (["image", "rm"], ["system", "prune"]) for command in self.engine.calls))

    def test_network_and_mount_arguments_are_narrow_and_resource_limits_fixed(self):
        arguments = drill.create_arguments(self.root, "a" * 32, self.image, self.root / "private.env", "helper.py")
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertEqual(arguments[arguments.index("--memory") + 1], "10g")
        self.assertEqual(arguments[arguments.index("--memory-swap") + 1], "10g")
        self.assertEqual(arguments[arguments.index("--cpus") + 1], "2")
        self.assertEqual(arguments.count("--mount"), 3)
        self.assertFalse(any(value in arguments for value in ("--publish", "-p", "--privileged", "--volumes-from")))

    def test_mods_make_offline_drill_inconclusive_despite_health_and_world_reads(self):
        self.set_config(mods="ExampleMod", workshop="123456")
        result = self.run_fake()
        self.assertFalse(result["passed"])
        self.assertTrue(result["checks"]["rcon_health"])
        self.assertTrue(result["checks"]["world_file_reads"])
        self.assertFalse(result["checks"]["mods_verified"])
        self.assertIn("inconclusive", result["mods_result"])
        self.assertTrue(result["container_removed"])

    def test_production_mount_is_detected_before_container_start(self):
        self.engine.bad_mount = True
        result = self.run_fake()
        self.assertFalse(result["passed"])
        self.assertIn("mount", result["failure"])
        self.assertFalse(any(command[:2] == ["container", "start"] for command in self.engine.calls))
        self.assertTrue(result["container_removed"])

    def test_loaded_image_digest_mismatch_never_creates_container(self):
        self.engine.bad_image = True
        result = self.run_fake()
        self.assertFalse(result["passed"])
        self.assertFalse(any(command[0] == "create" for command in self.engine.calls))

    def test_unsupported_oci_load_never_pulls_replacement(self):
        self.engine.fail_load = True
        result = self.run_fake()
        self.assertFalse(result["passed"])
        self.assertFalse(any("pull" in command for command in self.engine.calls))
        self.assertFalse(any(command[0] == "create" for command in self.engine.calls))

    def test_oom_or_unclean_shutdown_cannot_pass(self):
        self.engine.oom = True
        result = self.run_fake()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["clean_shutdown"])
        self.assertTrue(result["container_removed"])

    def test_rcon_failure_is_not_replaced_by_hash_evidence(self):
        self.engine.health_ok = False
        result = self.run_fake()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["rcon_health"])
        self.assertTrue(result["container_removed"])

    def test_cleanup_ownership_requires_id_name_and_label(self):
        document = {"Id": "a" * 64, "Name": "/pz-restore-drill-" + "b" * 32,
                    "Config": {"Labels": {drill.LABEL: "b" * 32}}}
        drill.owned_container(document, "a" * 64, "b" * 32)
        for field in ("Id", "Name", "Config"):
            wrong = copy.deepcopy(document)
            wrong[field] = {} if field == "Config" else "different"
            with self.assertRaises(drill.DrillError):
                drill.owned_container(wrong, "a" * 64, "b" * 32)

    def test_missing_existing_world_files_refuse_drill(self):
        (self.root / "data/zomboid/Saves/Multiplayer/world/map_t.bin").unlink()
        with self.assertRaisesRegex(drill.DrillError, "world_evidence"):
            drill.read_server_config(self.root, self.manifest)

    def test_secret_cannot_change_world_identity_or_inject_environment_line(self):
        self.secret["PZ_SERVER_NAME"] = "different"
        self.set_config()
        with self.assertRaisesRegex(drill.DrillError, "world_name"):
            drill.read_server_config(self.root, self.manifest)
        self.secret["PZ_SERVER_NAME"] = "world"
        self.secret["PZ_ADMIN_PASSWORD"] = "secret\nOTHER=value"
        self.set_config()
        with self.assertRaisesRegex(drill.DrillError, "multiline"):
            drill.read_server_config(self.root, self.manifest)

    def test_production_root_is_rejected_before_reading_any_secrets(self):
        for path in ("/srv/pz-storage/zomboid", "/opt/pz-stack/data", "relative/path", "/tmp/../srv/pz-storage/zomboid"):
            with self.subTest(path=path), self.assertRaises(drill.DrillError):
                drill.validated_root(path)

    def test_actual_inotify_read_evidence_does_not_pass_from_file_presence(self):
        paths = [self.root / name for name in drill.WORLD_FILES]
        for path in paths:
            path.write_bytes(b"world bytes")
        watcher = drill.WorldReadWatch(paths)
        try:
            self.assertFalse(watcher.poll())
            paths[0].read_bytes()
            self.assertFalse(watcher.poll())
            paths[1].read_bytes()
            self.assertTrue(watcher.poll())
        finally:
            watcher.close()

    def test_wrapper_is_valid_python_and_uses_explicit_offline_argument(self):
        compile(drill.WRAPPER, "drill-wrapper", "exec")
        self.assertIn(b"'-nosteam'", drill.WRAPPER)
        self.assertIn(b"runtime['rcon']('save')", drill.WRAPPER)
        self.assertIn(b"runtime['rcon']('quit')", drill.WRAPPER)
        constants = [node.value for node in ast.walk(ast.parse(drill.WRAPPER))
                     if isinstance(node, ast.Constant)]
        self.assertIn("\n", constants)
        self.assertIn(b"save\nquit\n", constants)
        self.assertNotIn("\\n", constants)
        self.assertNotIn(b"save\\nquit\\n", constants)

    def test_game_log_evidence_contains_only_bytes_from_this_drill(self):
        path = self.root / "data/zomboid/server-console-docker.log"
        path.write_bytes(b"old startup mod evidence\n")
        before = (path.stat().st_ino, path.stat().st_size)
        with path.open("ab") as stream:
            stream.write(b"new startup evidence\n")
        self.assertTrue(drill.capture_fresh_game_log(self.root, before))
        evidence = self.root / "runtime-drill-game.log"
        self.assertEqual(evidence.read_bytes(), b"new startup evidence\n")
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)

    def test_truncated_old_game_log_is_not_accepted_as_fresh_evidence(self):
        path = self.root / "data/zomboid/server-console-docker.log"
        path.write_bytes(b"old startup mod evidence\n")
        before = (path.stat().st_ino, path.stat().st_size)
        path.write_bytes(b"truncated\n")
        self.assertFalse(drill.capture_fresh_game_log(self.root, before))
        self.assertFalse((self.root / "runtime-drill-game.log").exists())

    def test_only_complete_fresh_mod_loader_messages_pass(self):
        path = self.root / "runtime-drill-game.log"
        adaptation = {"configured_mods": ["One", "Two"]}
        path.write_text("LOG  : Mod          f:0 st:5,032,438,764> loading One\n")
        self.assertFalse(drill.verify_fresh_mod_log(self.root, adaptation)["verified"])
        with path.open("a") as stream:
            stream.write("LOG  : Mod          f:0 st:5,032,438,800> loading Two\n")
        self.assertTrue(drill.verify_fresh_mod_log(self.root, adaptation)["verified"])
        with path.open("a") as stream:
            stream.write('WARN : Mod          f:0 st:5,032,438,801> required mod "Dependency" not found\n')
        self.assertFalse(drill.verify_fresh_mod_log(self.root, adaptation)["verified"])

    def test_offline_mod_links_use_only_manifest_verified_configured_workshop_bytes(self):
        self.set_config(mods="One;Two", workshop="123456")
        parent = self.root / "data/pz-server/steamapps/workshop/content/108600/123456/mods"
        expected = {}
        for name, mod in (("First", "One"), ("Second", "Two")):
            path = parent / name / "42/mod.info"
            path.parent.mkdir(parents=True)
            path.write_text("id=" + mod + "\n")
            expected[path.relative_to(self.root).as_posix()] = {"type": "file", **remote._local_digest(path)}
        config = self.root / "data/zomboid/Server/world.ini"
        before = config.read_bytes()
        with mock.patch.object(drill.restore, "validate_manifest", return_value=expected):
            result = drill.prepare_offline_mods(self.root, self.manifest, "a" * 32)
        self.assertEqual(set(result["provenance"]), {"One", "Two"})
        self.assertEqual(len(result["created_links"]), 2)
        self.assertEqual(config.read_bytes(), before)
        for link in result["created_links"]:
            self.assertEqual((self.root / link["path"]).resolve(), self.root / link["source"])
        with mock.patch.object(drill.restore, "validate_manifest", return_value=expected), self.assertRaises(drill.DrillError):
            drill.prepare_offline_mods(self.root, self.manifest, "a" * 32)

    def test_unverified_mod_metadata_never_creates_links(self):
        self.set_config(mods="One", workshop="123456")
        path = self.root / "data/pz-server/steamapps/workshop/content/108600/123456/mods/First/42/mod.info"
        path.parent.mkdir(parents=True)
        path.write_text("id=One\n")
        with mock.patch.object(drill.restore, "validate_manifest", return_value={}), self.assertRaises(drill.DrillError):
            drill.prepare_offline_mods(self.root, self.manifest, "a" * 32)
        self.assertFalse((self.root / "data/zomboid/mods").exists())

    def test_bound_production_baseline_preserves_effective_provider_and_existing_missing_warning(self):
        self.set_config(mods="One;Missing", workshop="123;456")
        self.manifest["captured_at"] = "2026-01-01T01:00:00Z"
        expected = {}
        sources = []
        for item in ("123", "456"):
            folder = self.root / "data/pz-server/steamapps/workshop/content/108600" / item / "mods/Provider"
            path = folder / "42/mod.info"
            path.parent.mkdir(parents=True)
            path.write_text("id=One\n")
            expected[path.relative_to(self.root).as_posix()] = {"type": "file", **remote._local_digest(path)}
            sources.append(folder.relative_to(self.root).as_posix())
        config = self.root / "data/zomboid/Server/world.ini"
        expected[config.relative_to(self.root).as_posix()] = remote._local_digest(config)
        jar = self.root / "data/pz-server/java/projectzomboid.jar"
        jar.parent.mkdir()
        classes = {name: hashlib.sha256(name.encode()).hexdigest() for name in (
            "zombie/ZomboidFileSystem.class", "zombie/core/znet/SteamWorkshop.class",
            "zombie/network/GameServer.class", "zombie/network/GameServerWorkshopItems.class",
            "zombie/gameStates/ChooseGameInfo.class")}
        with zipfile.ZipFile(jar, "w") as archive:
            for name in classes:
                archive.writestr(name, name.encode())
        baseline = {"format": "pz-production-mod-baseline-v1", "server_name": "world",
                    "observed_at": "2026-01-01T00:00:00Z", "config_sha256": remote._local_digest(config)["sha256"],
                    "configured_mods": ["One", "Missing"], "workshop_items": ["123", "456"],
                    "loaded_mods": ["One"], "missing_mods": ["Missing"], "missing_mod_warnings": ["Missing"],
                    "loader_class_sha256": classes, "duplicate_provider_resolution": {"One": sources[0]}}
        bad = copy.deepcopy(baseline)
        bad["duplicate_provider_resolution"]["One"] = sources[1]
        with mock.patch.object(drill.restore, "validate_manifest", return_value=expected), self.assertRaisesRegex(drill.DrillError, "not_proven"):
            drill.prepare_offline_mods(self.root, self.manifest, "a" * 32, bad)
        with mock.patch.object(drill.restore, "validate_manifest", return_value=expected):
            adaptation = drill.prepare_offline_mods(self.root, self.manifest, "a" * 32, baseline)
        self.assertEqual(adaptation["effective_mods"], ["One"])
        self.assertEqual(adaptation["baseline_missing_mods"], ["Missing"])
        self.assertEqual([link["source"] for link in adaptation["created_links"]], [sources[0]])
        log = self.root / "runtime-drill-game.log"
        log.write_text('WARN : Mod f:0 st:1> required mod "Missing" not found\nLOG : Mod f:0 st:2> loading One\n')
        self.assertTrue(drill.verify_fresh_mod_log(self.root, adaptation)["verified"])
        log.write_text('LOG : Mod f:0 st:2> loading One\n')
        self.assertFalse(drill.verify_fresh_mod_log(self.root, adaptation)["verified"])

    def test_archived_image_returns_verified_oci_config_digest(self):
        blobs = {}

        def blob(value, kind):
            data = json.dumps(value, sort_keys=True).encode()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            blobs["blobs/sha256/" + digest[7:]] = data
            return {"digest": digest, "size": len(data), "mediaType": kind}

        config = blob({"architecture": "amd64", "os": "linux"}, "application/vnd.oci.image.config.v1+json")
        manifest = blob({"schemaVersion": 2, "config": config, "layers": []}, "application/vnd.oci.image.manifest.v1+json")
        manifest["annotations"] = {"io.containerd.image.name": "local/test:restored"}
        members = {"oci-layout": json.dumps({"imageLayoutVersion": "1.0.0"}).encode(),
                   "index.json": json.dumps({"schemaVersion": 2, "manifests": [manifest]}).encode(), **blobs}
        path = self.root / "recovery/runtime-images.oci.tar"
        with tarfile.open(path, "w") as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        image = {"namespace": "zomboid", "workload": "statefulset/zomboid", "export_ref": "local/test:restored",
                 "image_id": config["digest"], "exported_manifest_digest": manifest["digest"]}
        remote._atomic_json(self.root / "recovery/index.json", {"format": "pz-recovery-v1", "complete": True,
                                                               "images": [image], "image_archive": {
                                                                   "path": path.name, **remote._local_digest(path)}})
        self.assertEqual(drill.archived_image(self.root), (path, config["digest"]))


if __name__ == "__main__":
    unittest.main()
