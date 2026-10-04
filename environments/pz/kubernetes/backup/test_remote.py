"""Offline protocol tests: no cloud credentials or production data needed."""

import dataclasses
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("pz_backup_remote", Path(__file__).with_name("remote.py"))
remote = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = remote
SPEC.loader.exec_module(remote)


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.corrupt = set()
        self.failed_put_suffix = None
        self.ambiguous_put_suffix = None
        self.failed_delete = False
        self.ambiguous_delete = False
        self.list_fail_after = None
        self.page_size = 1000
        self.list_calls = 0
        self.versioning = False
        self.version_ids = {}
        self.uploads = {}

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        self.calls.append(("put", key))
        assert kwargs["IfNoneMatch"] == "*"
        if key in self.objects:
            raise S3Error("PreconditionFailed")
        if self.failed_put_suffix and key.endswith(self.failed_put_suffix):
            raise S3Error("ServiceUnavailable")
        body = kwargs["Body"]
        body = body.read() if hasattr(body, "read") else body
        assert len(body) == kwargs["ContentLength"]
        self.objects[key] = body
        if self.versioning:
            self.version_ids[key] = str(len(self.version_ids) + 1)
        if self.ambiguous_put_suffix and key.endswith(self.ambiguous_put_suffix):
            raise TimeoutError
        return {"VersionId": self.version_ids[key]} if self.versioning else {}

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        self.calls.append(("get", key))
        if key not in self.objects:
            raise S3Error("NoSuchKey")
        if "VersionId" in kwargs and kwargs["VersionId"] != self.version_ids.get(key):
            raise S3Error("NoSuchVersion")
        body = self.objects[key]
        if key in self.corrupt:
            body = bytes([body[0] ^ 0x01]) + body[1:] if body else b"corrupt"
        result = {"Body": io.BytesIO(body), "ContentLength": len(body)}
        if self.versioning:
            result["VersionId"] = self.version_ids[key]
        return result

    def list_objects_v2(self, **kwargs):
        self.list_calls += 1
        if self.list_fail_after is not None and self.list_calls > self.list_fail_after:
            raise TimeoutError
        keys = sorted(k for k in self.objects if k.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken", "0"))
        stop = start + self.page_size
        result = {"Contents": [{"Key": k, "Size": len(self.objects[k]), "ETag": hashlib.md5(self.objects[k]).hexdigest()}
                               for k in keys[start:stop]], "IsTruncated": stop < len(keys)}
        if result["IsTruncated"]:
            result["NextContinuationToken"] = str(stop)
        return result

    def get_bucket_versioning(self, **kwargs):
        return {"Status": "Enabled"} if self.versioning else {}

    def list_object_versions(self, **kwargs):
        return {"IsTruncated": False, "Versions": [
            {"Key": k, "VersionId": self.version_ids[k], "IsLatest": True} for k in self.objects
        ]}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs["Key"]))
        if self.failed_delete:
            raise S3Error("AccessDenied")
        if self.versioning:
            assert kwargs["VersionId"] == self.version_ids[kwargs["Key"]]
        self.objects.pop(kwargs["Key"], None)
        if self.ambiguous_delete:
            raise TimeoutError
        return {}

    def create_multipart_upload(self, **kwargs):
        upload_id = str(len(self.uploads) + 1)
        self.uploads[upload_id] = {}
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs):
        self.uploads[kwargs["UploadId"]][kwargs["PartNumber"]] = kwargs["Body"]
        return {"ETag": str(kwargs["PartNumber"])}

    def complete_multipart_upload(self, **kwargs):
        assert kwargs["IfNoneMatch"] == "*"
        parts = self.uploads[kwargs["UploadId"]]
        body = b"".join(parts[index] for index in sorted(parts))
        result = self.put_object(Bucket=kwargs["Bucket"], Key=kwargs["Key"], Body=body,
                                 ContentLength=len(body), IfNoneMatch="*")
        del self.uploads[kwargs["UploadId"]]
        return result

    def abort_multipart_upload(self, **kwargs):
        if kwargs["UploadId"] not in self.uploads:
            raise S3Error("NoSuchUpload")
        del self.uploads[kwargs["UploadId"]]
        return {}


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = remote.RemoteConfig(bucket="pz-private", prefix="pz/test",
                                           lock_path=str(self.directory / "remote.lock"))
        self.s3 = FakeS3()
        self.payload = self.directory / "payload.age"
        self.manifest = self.directory / "manifest.age"
        self.payload.write_bytes(b"encrypted archive bytes")
        self.manifest.write_bytes(b"encrypted private manifest bytes")
        self.journal = self.directory / "deletions.json"
        self.attestation = self.directory / "restore.json"

    def upload(self, index=0, captured_at=None):
        captured_at = captured_at or "2026-01-01T00:00:%02dZ" % index
        sid = "20260101T000000Z-" + format(index, "032x")
        commit = remote.upload_snapshot(self.s3, self.config, snapshot_id=sid, captured_at=captured_at,
                                         payload_path=self.payload, manifest_path=self.manifest)
        return sid, commit

    def attest(self, snapshot_id, commit, **changes):
        record = {"format": remote.ATTESTATION_FORMAT, **self.config.scope,
                  "snapshot_id": snapshot_id, "commit_sha256": remote.commit_sha256(commit),
                  "restored_at": "2026-01-02T00:00:00Z", "checks": {
                      "archive_verified": True, "isolated_runtime": True,
                      "rcon_health": True, "world_loaded": True}}
        record.update(changes)
        remote._atomic_json(self.attestation, record)

    def retain(self, enabled=True):
        return remote.retain_snapshots(self.s3, self.config, attestation_path=self.attestation,
                                        journal_path=self.journal, enabled=enabled)

    def many(self, count):
        results = [self.upload(i) for i in range(count)]
        self.attest(*results[0])
        return results

    def test_commit_is_last_and_restore_verifies_bytes(self):
        sid, commit = self.upload()
        puts = [key for call, key in self.s3.calls if call == "put"]
        self.assertTrue(puts[-1].endswith("COMMITTED.json"))
        marker_index = self.s3.calls.index(("put", puts[-1]))
        self.assertIn(("get", commit["payload"]["key"]), self.s3.calls[:marker_index])
        self.assertIn(("get", commit["manifest"]["key"]), self.s3.calls[:marker_index])
        destination = self.directory / "restored"
        self.assertEqual(remote.restore_snapshot(self.s3, self.config, snapshot_id=sid,
                                                destination=destination), commit)
        self.assertEqual((destination / "payload.enc").read_bytes(), self.payload.read_bytes())
        self.assertEqual((destination / "manifest.enc").read_bytes(), self.manifest.read_bytes())
        self.assertEqual((destination / "payload.enc").stat().st_mode & 0o777, 0o600)

    def test_corrupted_get_never_commits(self):
        sid = "20260101T000000Z-" + "0" * 32
        self.s3.corrupt.add(remote._key(self.config, sid, "payload.enc"))
        with self.assertRaises(remote.BackupError):
            self.upload()
        self.assertFalse(any(key.endswith("COMMITTED.json") for key in self.s3.objects))

    def test_incomplete_upload_never_commits_or_rotates(self):
        self.many(6)
        self.s3.failed_put_suffix = "manifest.enc"
        with self.assertRaises(remote.BackupError):
            self.upload(6)
        self.s3.failed_put_suffix = None
        with self.assertRaises(remote.BackupError):
            self.retain()
        self.assertFalse(any(call == "delete" for call, key in self.s3.calls))
        self.assertEqual(sum(k.endswith("COMMITTED.json") for k in self.s3.objects), 6)

    def test_ambiguous_commit_is_reconciled_with_same_key(self):
        self.s3.ambiguous_put_suffix = "COMMITTED.json"
        sid, commit = self.upload()
        self.assertEqual(self.upload()[1], commit)
        self.assertEqual(len(self.s3.objects), 3)
        self.assertEqual(sum(call == "put" and key.endswith("COMMITTED.json") for call, key in self.s3.calls), 1)

    def test_ambiguous_payload_is_reconciled(self):
        self.s3.ambiguous_put_suffix = "payload.enc"
        self.upload()
        self.assertEqual(len(self.s3.objects), 3)

    def test_existing_id_cannot_overwrite_successful_snapshot(self):
        self.upload()
        before = self.s3.objects.copy()
        self.payload.write_bytes(b"different")
        with self.assertRaises(remote.BackupError):
            self.upload()
        self.assertEqual(self.s3.objects, before)

    def test_multipart_upload_is_conditional_and_fully_verified(self):
        self.config = dataclasses.replace(self.config, multipart_threshold=1)
        self.upload()
        self.assertEqual(len(self.s3.objects), 3)
        self.assertEqual(self.s3.uploads, {})

    def test_retention_disabled_does_not_even_list(self):
        self.assertEqual(self.retain(enabled=False), {"enabled": False, "deleted_snapshots": 0})
        self.assertEqual(self.s3.calls, [])

    def test_less_than_five_successful_snapshots_are_preserved(self):
        self.many(4)
        self.assertEqual(self.retain()["deleted_snapshots"], 0)
        self.assertEqual(len(self.s3.objects), 12)

    def test_retention_keeps_five_successful_snapshots(self):
        snapshots = self.many(8)
        self.s3.page_size = 4
        result = self.retain()
        self.assertEqual(result["deleted_snapshots"], 3)
        self.assertEqual(set(result["keepers"]), {sid for sid, _ in snapshots[3:]})
        self.assertEqual(len(self.s3.objects), 15)
        self.assertEqual(remote._private_json(self.journal)["phase"], "complete")
        self.assertEqual(self.retain()["deleted_snapshots"], 0)

    def test_failed_delete_preserves_five_and_can_resume(self):
        snapshots = self.many(7)
        self.s3.failed_delete = True
        with self.assertRaises(remote.BackupError):
            self.retain()
        self.assertEqual(len(self.s3.objects), 21)
        journal = remote._private_json(self.journal)
        self.assertEqual(journal["phase"], "deleting")
        self.s3.failed_delete = False
        self.assertEqual(self.retain()["deleted_snapshots"], 2)
        self.assertEqual(len(self.s3.objects), 15)
        for sid, _ in snapshots[-5:]:
            remote.restore_snapshot(self.s3, self.config, snapshot_id=sid, destination=self.directory / sid)

    def test_interrupted_partial_deletion_can_resume(self):
        self.many(6)
        original = self.s3.delete_object
        deleted = []

        def delete_once(**kwargs):
            if deleted:
                raise TimeoutError
            deleted.append(kwargs["Key"])
            return original(**kwargs)
        self.s3.delete_object = delete_once
        with self.assertRaises(remote.BackupError):
            self.retain()
        self.assertEqual(len(self.s3.objects), 17)
        self.s3.delete_object = original
        self.retain()
        self.assertEqual(len(self.s3.objects), 15)

    def test_ambiguous_delete_success_can_be_reconciled(self):
        self.many(6)
        self.s3.ambiguous_delete = True
        self.retain()
        self.assertEqual(len(self.s3.objects), 15)

    def test_resume_after_new_upload_still_keeps_the_latest_five(self):
        snapshots = self.many(6)
        self.s3.failed_delete = True
        with self.assertRaises(remote.BackupError):
            self.retain()
        snapshots.append(self.upload(6))
        self.s3.failed_delete = False
        result = self.retain()
        self.assertEqual(result["verified_snapshots"], 5)
        self.assertEqual(set(result["keepers"]), {sid for sid, _ in snapshots[-5:]})
        self.assertEqual(len(self.s3.objects), 15)

    def test_protected_full_snapshot_survives_five_newer_state_snapshots(self):
        snapshots = self.many(7)
        base_id, base_commit = snapshots[0]
        self.config = dataclasses.replace(self.config, protected_snapshot={
            "snapshot_id": base_id, "commit_sha256": remote.commit_sha256(base_commit)})
        result = self.retain()
        self.assertEqual(set(result["keepers"]), {base_id, *(sid for sid, _ in snapshots[-5:])})
        self.assertEqual(result["verified_snapshots"], 6)
        self.assertEqual(len(self.s3.objects), 18)

    def test_changed_protected_snapshot_blocks_all_retention(self):
        snapshots = self.many(7)
        base_id, _ = snapshots[0]
        self.config = dataclasses.replace(self.config, protected_snapshot={
            "snapshot_id": base_id, "commit_sha256": "0" * 64})
        with self.assertRaisesRegex(remote.BackupError, "Protected snapshot"):
            self.retain()
        self.assertFalse(any(call == "delete" for call, _ in self.s3.calls))

    def test_protection_can_start_after_completed_five_keeper_journal(self):
        snapshots = self.many(6)
        self.retain()
        base_id, base_commit = snapshots[1]
        self.config = dataclasses.replace(self.config, protected_snapshot={
            "snapshot_id": base_id, "commit_sha256": remote.commit_sha256(base_commit)})
        self.upload(6)
        result = self.retain()
        self.assertIn(base_id, result["keepers"])
        self.assertEqual(result["verified_snapshots"], 6)

    def test_listing_failure_never_deletes(self):
        self.many(6)
        self.s3.page_size = 4
        self.s3.list_fail_after = 1
        with self.assertRaises(remote.BackupError):
            self.retain()
        self.assertFalse(any(call == "delete" for call, key in self.s3.calls))

    def test_keeper_full_get_corruption_prevents_all_deletions(self):
        snapshots = self.many(6)
        self.s3.corrupt.add(snapshots[-1][1]["payload"]["key"])
        with self.assertRaises(remote.BackupError):
            self.retain()
        self.assertFalse(any(call == "delete" for call, key in self.s3.calls))

    def test_invalid_attestation_scope_prevents_all_deletions(self):
        snapshots = self.many(6)
        self.attest(*snapshots[0], prefix="pz/other")
        with self.assertRaises(remote.BackupError):
            self.retain()
        self.assertFalse(any(call == "delete" for call, key in self.s3.calls))

    def test_restore_requires_real_drill_checks(self):
        snapshots = self.many(6)
        self.attest(*snapshots[0], checks={"archive_verified": True})
        with self.assertRaises(remote.BackupError):
            self.retain()

    def test_versioned_objects_deleted_by_exact_version(self):
        self.s3.versioning = True
        self.many(6)
        self.retain()
        self.assertEqual(len(self.s3.objects), 15)

    def test_unknown_historical_version_prevents_retention(self):
        self.s3.versioning = True
        self.many(6)
        original = self.s3.list_object_versions

        def add_old_version(**kwargs):
            result = original(**kwargs)
            result["Versions"].append({**result["Versions"][0], "VersionId": "old", "IsLatest": False})
            return result
        self.s3.list_object_versions = add_old_version
        with self.assertRaises(remote.BackupError):
            self.retain()
        self.assertFalse(any(call == "delete" for call, key in self.s3.calls))

    def test_unsafe_marker_object_reference_is_rejected(self):
        sid, commit = self.upload()
        commit["payload"]["key"] = "another-prefix/private"
        self.s3.objects[remote._key(self.config, sid, "COMMITTED.json")] = remote.canonical_json(commit)
        with self.assertRaises(remote.BackupError):
            remote.restore_snapshot(self.s3, self.config, snapshot_id=sid, destination=self.directory / "bad")

    def test_duplicate_json_marker_members_are_rejected(self):
        with self.assertRaises(remote.BackupError):
            remote._read_json(b'{"format":"ok","format":"other"}')

    def test_same_capture_time_uses_stable_id(self):
        snapshots = [self.upload(i, "2026-01-01T00:00:00Z") for i in range(6)]
        self.attest(*snapshots[0])
        self.assertEqual(set(self.retain()["keepers"]), {sid for sid, _ in snapshots[1:]})

    def test_client_disables_optional_checksum_trailers_and_checks_model(self):
        captured = {}
        hooks = {}

        class Config:
            OPTION_DEFAULTS = {"request_checksum_calculation": None, "response_checksum_validation": None}

            def __init__(self, **kwargs):
                captured.update(kwargs)

        client = types.SimpleNamespace(meta=types.SimpleNamespace(events=types.SimpleNamespace(register=lambda event, hook: hooks.setdefault(event, hook)), service_model=types.SimpleNamespace(
            operation_model=lambda operation: types.SimpleNamespace(input_shape=types.SimpleNamespace(members={"IfNoneMatch": {}})))))
        credentials = self.directory / "credentials.json"
        remote._atomic_json(credentials, {"aws_access_key_id": "test-only-id", "aws_secret_access_key": "test-only-secret"})
        modules = {"boto3": types.SimpleNamespace(client=lambda *args, **kwargs: client),
                   "botocore": types.ModuleType("botocore"), "botocore.config": types.SimpleNamespace(Config=Config)}
        with mock.patch.dict(sys.modules, modules):
            self.assertIs(remote.create_client(self.config, credentials), client)
        self.assertEqual(captured["request_checksum_calculation"], "when_required")
        self.assertEqual(captured["response_checksum_validation"], "when_required")
        self.assertEqual(set(hooks), {'before-sign.s3.CreateMultipartUpload', 'before-sign.s3.UploadPart'})
        for hook in hooks.values():
            request = types.SimpleNamespace(headers={})
            hook(request, operation_name='fixture')
            self.assertEqual(request.headers, {'If-None-Match': '*'})

    def test_old_client_model_cannot_silently_omit_conditional_writes(self):
        class Config:
            OPTION_DEFAULTS = {}

            def __init__(self, **kwargs):
                pass

        client = types.SimpleNamespace(meta=types.SimpleNamespace(service_model=types.SimpleNamespace(
            operation_model=lambda operation: types.SimpleNamespace(input_shape=types.SimpleNamespace(members={})))))
        credentials = self.directory / "credentials.json"
        remote._atomic_json(credentials, {"aws_access_key_id": "test-only-id", "aws_secret_access_key": "test-only-secret"})
        modules = {"boto3": types.SimpleNamespace(client=lambda *args, **kwargs: client),
                   "botocore": types.ModuleType("botocore"), "botocore.config": types.SimpleNamespace(Config=Config)}
        with mock.patch.dict(sys.modules, modules), self.assertRaisesRegex(remote.BackupError, "too old"):
            remote.create_client(self.config, credentials)


if __name__ == "__main__":
    unittest.main()
