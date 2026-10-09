"""Offline transaction/provenance/storage fixtures. Never access a cluster."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
import hashlib
import urllib.error
import http.client


spec = importlib.util.spec_from_file_location("panel_updater", Path(__file__).with_name("updater.py"))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)

OLD_IMAGE = "docker.io/local/panel:migration-aabbcc"
CANDIDATE = {"version": "1.4.0", "digest": "sha256:" + "a" * 64,
             "revision": "b" * 40, "image": u.IMAGE + ":1.4.0@sha256:" + "a" * 64,
             "slot": "r-" + "a" * 32}


class FakeAPI:
    def __init__(self, data):
        self.data, self.budget = data, u.Budget(60)
        self.actions = []
        self.fail_candidate_health = False
        self.ambiguous_candidate = False
        self.ambiguous_switch = False
        self.corrupt_backup = False
        self.foreign_owner = False
        self.document = {
            "metadata": {"uid": "deployment-uid", "resourceVersion": "1", "generation": 1},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                     "selector": {"matchLabels": {"app.kubernetes.io/name": "panel"}},
                     "template": {"metadata": {"labels": {"app.kubernetes.io/name": "panel", u.SLOT: "initial"}},
                                  "spec": {"containers": [{"name": "panel", "image": OLD_IMAGE, "imagePullPolicy": "Never"}]}}},
        }
        self.service_document = {"metadata": {"uid": "service-uid", "resourceVersion": "1"},
                                 "spec": {"selector": {"app.kubernetes.io/name": "panel", u.SLOT: "initial"}}}

    def deployment(self):
        result = copy.deepcopy(self.document)
        count = result["spec"]["replicas"]
        result["status"] = {"observedGeneration": result["metadata"]["generation"],
                            "replicas": count, "readyReplicas": count, "updatedReplicas": count}
        return result

    def service(self):
        return copy.deepcopy(self.service_document)

    def replicasets(self):
        return [{"metadata": {"uid": "rs-uid", "ownerReferences": [{"kind": "Deployment", "uid": "deployment-uid", "controller": True}]}}]

    def pods(self):
        if not self.document["spec"]["replicas"]:
            return []
        template = copy.deepcopy(self.document["spec"]["template"])
        template["metadata"].update(uid="pod-" + str(self.document["metadata"]["generation"]),
                                    ownerReferences=[{"kind": "ReplicaSet", "uid": "foreign" if self.foreign_owner else "rs-uid", "controller": True}])
        template["status"] = {"phase": "Running", "podIP": "10.42.0.3", "conditions": [{"type": "Ready", "status": "True"}],
                              "containerStatuses": [{"name": "panel", "ready": True, "restartCount": 0}]}
        return [template]

    def patch(self, kind, original, operations):
        target = self.document if kind == "deployments" else self.service_document
        if target["metadata"]["resourceVersion"] != original["metadata"]["resourceVersion"]:
            raise u.Refused("cas_conflict")
        self.actions.append((kind, copy.deepcopy(operations)))
        for operation in operations:
            parts = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].strip("/").split("/")]
            current = target
            for part in parts[:-1]:
                current = current[int(part)] if isinstance(current, list) else current[part]
            current[parts[-1]] = copy.deepcopy(operation["value"])
        target["metadata"]["resourceVersion"] = str(int(target["metadata"]["resourceVersion"]) + 1)
        if kind == "deployments":
            target["metadata"]["generation"] += 1
            if target["spec"]["replicas"] == 1 and u.deployment_state(target)["image"] == CANDIDATE["image"]:
                (self.data / "db.json").write_text("new-version-data")
                (self.data / "new-file").write_text("created by new version")
                if self.corrupt_backup:
                    saved = next((self.data / u.MANAGED_NAME / "backups").glob("*/data/db.json"))
                    saved.write_text("corrupted")
                if self.ambiguous_candidate:
                    raise u.Ambiguous("lost_launch_ack")
        elif self.ambiguous_switch:
            raise u.Ambiguous("lost_service_ack")
        return self.deployment() if kind == "deployments" else self.service()

    def health(self, url, **kwargs):
        assert url == "http://10.42.0.3:3001/api/health"
        candidate = u.deployment_state(self.document)["image"] == CANDIDATE["image"]
        value = "0.0.0" if candidate and self.fail_candidate_health else "1.4.0" if candidate else "1.3.7"
        return {"status": "ok", "version": value}, b"", {}


class Transactions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name) / "data"
        self.data.mkdir(mode=0o700)
        (self.data / "db.json").write_text("original-data")
        (self.data / "db.json").chmod(0o640)
        (self.data / "nested").mkdir(mode=0o750)
        (self.data / "nested" / "credential.secret").write_text("fixture-only")
        (self.data / "nested" / "credential.secret").chmod(0o600)
        self.api = FakeAPI(self.data)
        self.free = patch.object(u.shutil, "disk_usage", return_value=type("Usage", (), {"free": 4 * 1024 ** 3})())
        self.free.start()

    def tearDown(self):
        self.free.stop()
        self.temporary.cleanup()

    def run_update(self):
        with u.locked(self.data) as managed, patch.object(u, "http_json", side_effect=self.api.health), redirect_stdout(io.StringIO()):
            return u.Updater(self.api, self.data, managed, CANDIDATE, "1.3.7", 60).run()

    def journal(self):
        return json.loads((self.data / u.MANAGED_NAME / "journal.json").read_text())

    def test_success_backs_up_before_launch_and_only_then_switches_service(self):
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.journal()["phase"], "committed")
        self.assertEqual(self.api.service()["spec"]["selector"][u.SLOT], CANDIDATE["slot"])
        self.assertEqual([kind for kind, _ in self.api.actions], ["deployments", "deployments", "services"])
        backup = next((self.data / u.MANAGED_NAME / "backups").glob("*/data/db.json"))
        self.assertEqual(backup.read_text(), "original-data")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.data / "db.json").read_text(), "new-version-data")

    def test_required_telemetry_marker_checked_before_stopping_old_panel(self):
        with patch.dict(os.environ, {"PANEL_TELEMETRY_REQUIRED": "true"}):
            with self.assertRaisesRegex(u.Refused, "panel_telemetry_compatibility_missing"):
                self.run_update()
        self.assertEqual(self.api.actions, [])
        self.assertFalse((self.data / u.MANAGED_NAME / "journal.json").exists())

    def test_candidate_without_telemetry_contract_rolls_back_verified_data(self):
        original_health = self.api.health
        def health(url, **kwargs):
            document, body, headers = original_health(url, **kwargs)
            if u.deployment_state(self.api.document)["image"] == OLD_IMAGE:
                document["telemetry"] = {"adapter": "kubernetes-game-v1", "compatibility": "verified", "scope": "game-container-v1"}
            return document, body, headers
        with patch.object(self.api, "health", side_effect=health), patch.dict(os.environ, {"PANEL_TELEMETRY_REQUIRED": "true"}):
            self.assertEqual(self.run_update(), 2)
        self.assertEqual(self.journal()["phase"], "rolled_back")
        self.assertEqual((self.data / "db.json").read_text(), "original-data")
        self.assertFalse((self.data / "new-file").exists())
        self.assertEqual(self.api.service()["spec"]["selector"][u.SLOT], "initial")

    def test_adapter_compatibility_does_not_require_game_to_be_running(self):
        original_health = self.api.health
        def health(url, **kwargs):
            document, body, headers = original_health(url, **kwargs)
            document["telemetry"] = {"adapter": "kubernetes-game-v1", "compatibility": "verified", "scope": "game-container-v1", "ready": False}
            return document, body, headers
        with patch.object(self.api, "health", side_effect=health), patch.dict(os.environ, {"PANEL_TELEMETRY_REQUIRED": "true"}):
            self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.journal()["phase"], "committed")

    def test_failed_candidate_restores_offline_and_keeps_failed_data(self):
        self.api.fail_candidate_health = True
        self.assertEqual(self.run_update(), 2)
        self.assertEqual(self.journal()["phase"], "rolled_back")
        self.assertEqual((self.data / "db.json").read_text(), "original-data")
        self.assertFalse((self.data / "new-file").exists())
        self.assertEqual((self.data / "db.json").stat().st_mode & 0o777, 0o640)
        self.assertEqual(u.deployment_state(self.api.deployment())["image"], OLD_IMAGE)
        self.assertEqual(u.deployment_state(self.api.deployment())["policy"], "Never")
        self.assertEqual(self.api.service()["spec"]["selector"][u.SLOT], "initial")
        preserved = next((self.data / u.MANAGED_NAME).glob("failed-data-*/db.json"))
        self.assertEqual(preserved.read_text(), "new-version-data")
        actions = len(self.api.actions)
        with self.assertRaisesRegex(u.Refused, "previously_failed"):
            self.run_update()
        self.assertEqual(len(self.api.actions), actions)
        with u.locked(self.data) as managed:
            replacement = {**CANDIDATE, "digest": "sha256:" + "f" * 64}
            with self.assertRaisesRegex(u.Refused, "version_previously_failed"):
                u.Updater(self.api, self.data, managed, replacement, "1.3.7", 60).run()
        self.assertEqual(len(self.api.actions), actions)

    def test_lost_service_ack_never_rolls_back_or_overwrites_new_data(self):
        self.api.ambiguous_switch = True
        self.assertEqual(self.run_update(), 2)
        self.assertEqual(self.journal()["phase"], "traffic_release_requested")
        self.assertEqual((self.data / "db.json").read_text(), "new-version-data")
        self.assertEqual(len(self.api.actions), 3)
        with self.assertRaisesRegex(u.Refused, "incomplete_journal"):
            self.run_update()
        self.assertEqual(len(self.api.actions), 3)

    def test_lost_candidate_ack_preserves_journal_without_retry(self):
        self.api.ambiguous_candidate = True
        self.assertEqual(self.run_update(), 2)
        self.assertEqual(self.journal()["phase"], "starting_candidate")
        self.assertEqual(len(self.api.actions), 2)
        self.assertEqual((self.data / "db.json").read_text(), "new-version-data")

    def test_corrupt_backup_cannot_replace_current_data(self):
        self.api.fail_candidate_health = self.api.corrupt_backup = True
        self.assertEqual(self.run_update(), 2)
        self.assertEqual(self.journal()["phase"], "restoring_data")
        self.assertEqual((self.data / "db.json").read_text(), "new-version-data")
        self.assertEqual(u.deployment_state(self.api.deployment())["replicas"], 0)

    def test_corrupt_backup_readback_prevents_candidate_launch(self):
        original_backup = u.backup_data
        def corrupted_backup(*args):
            destination = original_backup(*args)
            (destination / "data/db.json").write_text("corrupted readback")
            return destination
        with patch.object(u, "backup_data", side_effect=corrupted_backup):
            self.assertEqual(self.run_update(), 2)
        self.assertEqual((self.data / "db.json").read_text(), "original-data")
        self.assertEqual(u.deployment_state(self.api.deployment())["replicas"], 1)
        self.assertTrue(all(operation.get("value") != CANDIDATE["image"]
                            for _, operations in self.api.actions for operation in operations))
        self.assertEqual(self.api.service()["spec"]["selector"][u.SLOT], "initial")
        self.assertEqual(self.journal()["phase"], "rolled_back")

    def test_foreign_pod_owner_is_rejected_without_mutation(self):
        self.api.foreign_owner = True
        with self.assertRaisesRegex(u.Refused, "foreign_panel_labeled_pod"):
            self.run_update()
        self.assertFalse(self.api.actions)

    def test_symlink_backup_escape_is_rejected_before_stopping(self):
        (self.data / "escape").symlink_to(Path(self.temporary.name))
        with self.assertRaisesRegex(u.Refused, "symlink_or_special"):
            self.run_update()
        self.assertFalse(self.api.actions)

    def test_total_managed_cap_and_low_disk_refuse_before_stopping(self):
        with patch.object(u, "MAX_MANAGED", 1024):
            with self.assertRaisesRegex(u.Refused, "managed_backup_budget"):
                self.run_update()
        self.assertFalse(self.api.actions)
        with patch.object(u.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1024})()):
            with self.assertRaisesRegex(u.Refused, "insufficient_backup_reserve"):
                self.run_update()
        self.assertFalse(self.api.actions)

    def test_observed_map_cache_is_included_in_full_backup_preflight(self):
        cache = self.data / "map-tiles-cache"
        cache.mkdir()
        # Sparse fixture checks the real inventory sizes without copying 679 MB.
        with (cache / "tiles.bin").open("wb") as output:
            output.truncate(662901276)
        with (self.data / "panel-extra-data").open("wb") as output:
            output.truncate(16 * 1024 ** 2)
        with u.locked(self.data) as managed:
            records, total = u.backup_preflight(self.data, managed, u.Budget(60))
        self.assertGreater(total, 256 * 1024 ** 2)
        self.assertLess(total, 1024 ** 3)
        self.assertEqual(next(row["size"] for row in records if row["path"] == "map-tiles-cache/tiles.bin"), 662901276)
        self.assertFalse(self.api.actions)

    def test_one_gib_source_boundary_is_accepted_then_one_extra_byte_refuses_before_stop(self):
        _, existing = u.inventory(self.data, u.Budget(60))
        cache = self.data / "map-tiles-cache"
        cache.mkdir()
        payload = cache / "tiles.bin"
        with payload.open("wb") as output:
            output.truncate(1024 ** 3 - existing)
        with u.locked(self.data) as managed:
            _, total = u.backup_preflight(self.data, managed, u.Budget(60))
            self.assertEqual(total, 1024 ** 3)
        with payload.open("r+b") as output:
            output.truncate(payload.stat().st_size + 1)
        with self.assertRaisesRegex(u.Refused, "panel_data_backup_bound_exceeded"):
            self.run_update()
        self.assertFalse(self.api.actions)
        self.assertFalse((self.data / u.MANAGED_NAME / "journal.json").exists())

    def test_three_gib_managed_boundary_reserves_both_snapshots_and_free_disk(self):
        with u.locked(self.data) as managed:
            records, total = u.inventory(self.data, u.Budget(60))
            retained = 3 * 1024 ** 3 - 2 * u.snapshot_estimate(records) - 1024 ** 2
            with patch.object(u, "managed_bytes", return_value=retained):
                self.assertEqual(u.backup_preflight(self.data, managed, u.Budget(60))[1], total)
            with patch.object(u, "managed_bytes", return_value=retained + 1), \
                 self.assertRaisesRegex(u.Refused, "managed_backup_budget_requires_manual_retention"):
                u.backup_preflight(self.data, managed, u.Budget(60))
            with patch.object(u.shutil, "disk_usage", return_value=type("Usage", (), {"free": 512 * 1024 ** 2 + total})()):
                self.assertEqual(u.backup_preflight(self.data, managed, u.Budget(60))[1], total)
            with patch.object(u.shutil, "disk_usage", return_value=type("Usage", (), {"free": 512 * 1024 ** 2 + total - 1})()), \
                 self.assertRaisesRegex(u.Refused, "insufficient_backup_reserve"):
                u.backup_preflight(self.data, managed, u.Budget(60))
        self.assertFalse(self.api.actions)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses mode000; run this permission regression as an ordinary user")
    def test_unreadable_source_or_managed_subtree_refuses_before_stopping(self):
        for in_managed in (False, True):
            parent = self.data / u.MANAGED_NAME if in_managed else self.data
            parent.mkdir(mode=0o700, exist_ok=True)
            hidden = parent / "inaccessible"
            hidden.mkdir(mode=0o700)
            (hidden / "file").write_text("must not disappear")
            hidden.chmod(0)
            try:
                with self.assertRaisesRegex(u.Refused, "filesystem_walk_failed"):
                    self.run_update()
                self.assertFalse(self.api.actions)
            finally:
                hidden.chmod(0o700)

    def test_downgrade_refused_and_equal_version_does_not_mutate(self):
        container = self.api.document["spec"]["template"]["spec"]["containers"][0]
        container["image"] = u.IMAGE + ":1.4.1@sha256:" + "c" * 64
        with self.assertRaisesRegex(u.Refused, "downgrade_refused"):
            self.run_update()
        container["image"] = CANDIDATE["image"]
        self.assertEqual(self.run_update(), 0)
        self.assertFalse(self.api.actions)

    def test_lock_excludes_second_updater(self):
        with u.locked(self.data):
            with self.assertRaisesRegex(u.Refused, "another_updater"):
                with u.locked(self.data):
                    self.fail("Second lock unexpectedly acquired")


class Discovery(unittest.TestCase):
    def registry(self, release_tag="v1.4.0", revision="b" * 40):
        config = {"architecture": "amd64", "os": "linux", "config": {"Labels": {
            "org.opencontainers.image.version": "1.4.0", "org.opencontainers.image.revision": revision,
            "org.opencontainers.image.source": "https://github.com/" + u.REPOSITORY}}}
        def serialized(value):
            raw = json.dumps(value).encode()
            return raw, "sha256:" + hashlib.sha256(raw).hexdigest()
        config_raw, config_digest = serialized(config)
        manifest = {"config": {"digest": config_digest}}
        manifest_raw, manifest_digest = serialized(manifest)
        index = {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": manifest_digest}]}
        index_raw, index_digest = serialized(index)
        def request(url, **kwargs):
            if url.endswith("/releases/latest"):
                value = {"tag_name": release_tag, "draft": False, "prerelease": False}
            elif "/git/ref/tags/" in url:
                value = {"object": {"type": "commit", "sha": "b" * 40}}
            elif url.startswith("https://ghcr.io/token?"):
                value = {"token": "fixture-token-not-real"}
            elif url.endswith("/manifests/1.4.0"):
                return index, index_raw, {}
            elif url.endswith("/manifests/" + manifest_digest):
                return manifest, manifest_raw, {}
            elif url.endswith("/blobs/" + config_digest):
                return config, config_raw, {}
            else:
                raise AssertionError("Unexpected network destination in fixture")
            return value, json.dumps(value).encode(), {}
        return request, index_digest

    def test_official_stable_release_and_content_digests(self):
        request, digest = self.registry()
        with patch.object(u, "http_json", side_effect=request):
            result = u.discover(u.Budget(60))
        self.assertEqual(result["image"], u.IMAGE + ":1.4.0@" + digest)
        self.assertEqual(result["revision"], "b" * 40)

    def test_latest_main_prerelease_and_unrelated_revision_are_rejected(self):
        for tag in ("latest", "main", "v1.5.0-rc.1", "v01.4.0"):
            request, _ = self.registry(release_tag=tag)
            with patch.object(u, "http_json", side_effect=request), self.assertRaises(u.Refused):
                u.discover(u.Budget(60))
        request, _ = self.registry(revision="c" * 40)
        with patch.object(u, "http_json", side_effect=request), self.assertRaisesRegex(u.Refused, "provenance_mismatch"):
            u.discover(u.Budget(60))

    def test_check_only_never_opens_or_creates_pvc_lock(self):
        api = type("ReadAPI", (), {"deployment": lambda self: FakeAPI(Path("/unused")).deployment()})()
        with patch.object(u.os, "geteuid", return_value=1000), patch.object(u, "Kubernetes", return_value=api), \
             patch.object(u, "discover", return_value=CANDIDATE), patch.object(u, "locked", side_effect=AssertionError("PVC mutation")), \
             patch.object(u.sys, "argv", ["updater.py", "--check-only"]), redirect_stdout(io.StringIO()):
            self.assertEqual(u.main(), 0)


class Transport(unittest.TestCase):
    def test_patch_transport_or_server_failure_is_ambiguous(self):
        errors = [TimeoutError(), http.client.IncompleteRead(b"partial"), u.Deadline("deadline_reached")]
        for code in (408, 429, 500, 503):
            errors.append(urllib.error.HTTPError("https://api", code, "fixture", {}, None))
        for error in errors:
            opener = type("Opener", (), {"open": lambda self, *args, **kwargs: (_ for _ in ()).throw(error)})()
            with patch.object(u.urllib.request, "build_opener", return_value=opener), self.assertRaises(u.Ambiguous):
                u.http_json("https://api", method="PATCH", body=[])

    def test_atomic_cas_rejection_does_not_claim_unknown_mutation(self):
        error = urllib.error.HTTPError("https://api", 422, "JSON patch test failed", {}, None)
        opener = type("Opener", (), {"open": lambda self, *args, **kwargs: (_ for _ in ()).throw(error)})()
        with patch.object(u.urllib.request, "build_opener", return_value=opener):
            try:
                u.http_json("https://api", method="PATCH", body=[])
            except u.Refused as caught:
                self.assertNotIsInstance(caught, u.Ambiguous)
            else:
                self.fail("Expected CAS refusal")

    def test_kubernetes_patch_includes_uid_and_resource_version_tests(self):
        api = u.Kubernetes.__new__(u.Kubernetes)
        captured = []
        api.request = lambda path, body: captured.append((path, body)) or {}
        api.patch("deployments", {"metadata": {"uid": "uid", "resourceVersion": "45"}},
                  [{"op": "replace", "path": "/spec/replicas", "value": 0}])
        self.assertEqual(captured[0][0], "/apis/apps/v1/namespaces/zomboid/deployments/panel")
        self.assertEqual(captured[0][1][:2], [
            {"op": "test", "path": "/metadata/resourceVersion", "value": "45"},
            {"op": "test", "path": "/metadata/uid", "value": "uid"}])


if __name__ == "__main__":
    unittest.main()
