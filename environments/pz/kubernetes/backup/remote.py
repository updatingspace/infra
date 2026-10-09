#!/usr/bin/env python3
"""Immutable encrypted S3 snapshots; publishing, restore and fail-closed retention.

The caller supplies already encrypted, immutable payload and manifest files. Both
are streamed back from S3 before COMMITTED.json is conditionally created. ETags
and object metadata are never used as integrity evidence. Credentials for upload,
retention and restore must be separate; this module never creates credentials.

RemoteConfig.lock_path must name the same host lock for all publishers/retainers.
The bucket policy must prohibit other writers: a host flock is not a distributed
lock. Conditional multipart completion must be supported by the selected endpoint;
an unsupported condition fails the backup instead of falling back to overwrites.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid
from typing import Any, BinaryIO, Iterator


FORMAT = "pz-backup-commit-v1"
ATTESTATION_FORMAT = "pz-backup-restore-attestation-v1"
JOURNAL_FORMAT = "pz-backup-deletions-v1"
ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1024 * 1024
MARKER_LIMIT = 64 * 1024


class BackupError(RuntimeError):
    """Safe-to-log failure: never includes response bodies or credentials."""


@dataclasses.dataclass(frozen=True)
class RemoteConfig:
    bucket: str
    prefix: str
    endpoint_url: str | None = None
    region_name: str = "ru-central1"
    snapshot_format: str = "pz-backup-v1"
    storage_class: str = "STANDARD"
    lock_path: str = "/var/lib/pz-backup-remote/remote.lock"
    multipart_threshold: int = 128 * 1024 * 1024
    multipart_part_size: int = 64 * 1024 * 1024
    protected_snapshot: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise BackupError("Invalid bucket name")
        prefix = self.prefix.rstrip("/")
        if not prefix or any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in prefix.split("/")):
            raise BackupError("Prefix must contain only nonempty safe path segments")
        object.__setattr__(self, "prefix", prefix)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.snapshot_format):
            raise BackupError("Invalid snapshot format")
        if self.endpoint_url and not self.endpoint_url.startswith("https://"):
            raise BackupError("S3 endpoint must use HTTPS")
        if self.multipart_threshold < 1 or not 5 * 1024 * 1024 <= self.multipart_part_size <= 5 * 1024**3:
            raise BackupError("Invalid multipart limits")
        if self.protected_snapshot is not None:
            entry = self.protected_snapshot
            if (not isinstance(entry, dict) or set(entry) != {"snapshot_id", "commit_sha256"}
                    or not isinstance(entry["snapshot_id"], str)
                    or not ID_PATTERN.fullmatch(entry["snapshot_id"])
                    or not isinstance(entry["commit_sha256"], str)
                    or not SHA_PATTERN.fullmatch(entry["commit_sha256"])):
                raise BackupError("Invalid protected snapshot identity")

    @property
    def object_prefix(self) -> str:
        return self.prefix + "/"

    @property
    def scope(self) -> dict[str, Any]:
        return {"bucket": self.bucket, "prefix": self.prefix,
                "endpoint_url": self.endpoint_url, "snapshot_format": self.snapshot_format}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def commit_sha256(commit: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(commit)).hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta():
            raise ValueError
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise BackupError("Timestamp must be RFC3339 UTC") from None


def new_snapshot_id(captured_at: str | None = None) -> str:
    return _time(captured_at or utc_now()).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex


def _check_id(snapshot_id: Any) -> str:
    if not isinstance(snapshot_id, str) or not ID_PATTERN.fullmatch(snapshot_id):
        raise BackupError("Invalid snapshot ID")
    return snapshot_id


def _key(config: RemoteConfig, snapshot_id: str, filename: str) -> str:
    return config.object_prefix + _check_id(snapshot_id) + "/" + filename


def _missing(error: Exception) -> bool:
    response = getattr(error, "response", {})
    return response.get("Error", {}).get("Code") in ("NoSuchKey", "NoSuchVersion", "NotFound", "404")


def _read_json(data: bytes) -> dict[str, Any]:
    def unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in items:
            if key in obj:
                raise ValueError("Duplicate JSON member")
            obj[key] = value
        return obj
    try:
        value = json.loads(data, object_pairs_hook=unique_pairs)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise BackupError("Invalid JSON record") from None


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _private_json(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()):
                raise BackupError("Private record must be owner-readable only and owned by the service or root")
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise BackupError("Private record is too large")
        return _read_json(data)
    except OSError:
        raise BackupError("Cannot read private record") from None


@contextlib.contextmanager
def _lock(config: RemoteConfig) -> Iterator[None]:
    path = Path(config.lock_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupError("Remote lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("Another publisher or retainer holds the remote lock") from None
        yield
    finally:
        os.close(descriptor)


def _local_digest(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise BackupError("Upload input must be a regular file")
        while block := stream.read(CHUNK):
            digest.update(block)
            size += len(block)
    return {"size": size, "sha256": digest.hexdigest()}


def _get(s3: Any, config: RemoteConfig, key: str, version_id: str | None = None,
         destination: BinaryIO | None = None, collect: bool = False,
         missing_ok: bool = False) -> tuple[dict[str, Any], bytes] | None:
    kwargs = {"Bucket": config.bucket, "Key": key}
    if version_id is not None:
        kwargs["VersionId"] = version_id
    try:
        response = s3.get_object(**kwargs)
    except Exception as error:
        if missing_ok and _missing(error):
            return None
        raise BackupError("S3 GET failed") from None
    body = response["Body"]
    digest, size, pieces = hashlib.sha256(), 0, []
    try:
        while block := body.read(CHUNK):
            size += len(block)
            if collect and size > MARKER_LIMIT:
                raise BackupError("Commit marker exceeds size limit")
            digest.update(block)
            if destination is not None:
                destination.write(block)
            if collect:
                pieces.append(block)
    except BackupError:
        raise
    except Exception:
        raise BackupError("S3 GET stream failed") from None
    finally:
        body.close()
    if response.get("ContentLength") != size:
        raise BackupError("S3 GET length mismatch")
    actual: dict[str, Any] = {"key": key, "size": size, "sha256": digest.hexdigest()}
    if response.get("VersionId") is not None:
        actual["version_id"] = response["VersionId"]
    if version_id is not None and actual.get("version_id") != version_id:
        raise BackupError("S3 GET returned a different object version")
    return actual, b"".join(pieces)


def _verify(s3: Any, config: RemoteConfig, expected: dict[str, Any],
            destination: BinaryIO | None = None) -> dict[str, Any]:
    result = _get(s3, config, expected["key"], expected.get("version_id"), destination)
    assert result is not None
    actual = result[0]
    if any(actual[name] != expected[name] for name in ("size", "sha256")):
        raise BackupError("Remote object failed full SHA256/size verification")
    return actual


def _put_file(s3: Any, config: RemoteConfig, path: Path, key: str,
              expected: dict[str, Any]) -> dict[str, Any]:
    """Conditional create, including safe reconciliation of ambiguous responses."""
    version_id, upload_id = None, None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BackupError("Upload input must be a regular file")
            if expected["size"] < config.multipart_threshold:
                response = s3.put_object(Bucket=config.bucket, Key=key, Body=stream,
                                         ContentLength=expected["size"], IfNoneMatch="*",
                                         StorageClass=config.storage_class)
            else:
                part_size = max(config.multipart_part_size, (expected["size"] + 9999) // 10000)
                if part_size > 5 * 1024**3:
                    raise BackupError("Payload exceeds multipart size limits")
                initiated = s3.create_multipart_upload(Bucket=config.bucket, Key=key,
                                                      StorageClass=config.storage_class)
                upload_id = initiated["UploadId"]
                parts = []
                while block := stream.read(part_size):
                    number = len(parts) + 1
                    part = s3.upload_part(Bucket=config.bucket, Key=key, UploadId=upload_id,
                                          PartNumber=number, Body=block)
                    parts.append({"PartNumber": number, "ETag": part["ETag"]})
                response = s3.complete_multipart_upload(Bucket=config.bucket, Key=key,
                                                        UploadId=upload_id,
                                                        MultipartUpload={"Parts": parts}, IfNoneMatch="*")
            version_id = response.get("VersionId")
    except Exception:
        # A timeout may mean the server has accepted the object. Never blindly
        # retry a write or choose a new ID; read back this exact unique key.
        reconciled = _get(s3, config, key, missing_ok=True)
        if reconciled is None or any(reconciled[0][name] != expected[name] for name in ("size", "sha256")):
            raise BackupError("Conditional upload failed or has an unverified outcome") from None
        version_id = reconciled[0].get("version_id")
    finally:
        if upload_id is not None:
            try:
                s3.abort_multipart_upload(Bucket=config.bucket, Key=key, UploadId=upload_id)
            except Exception as error:
                # Completed uploads no longer exist. Other failures require
                # alerting/reconciliation; they must never be silently ignored.
                if getattr(error, "response", {}).get("Error", {}).get("Code") != "NoSuchUpload":
                    raise BackupError("Multipart cleanup failed; inspect incomplete uploads") from None
    spec = {"key": key, **expected}
    if version_id is not None:
        spec["version_id"] = version_id
    return _verify(s3, config, spec)


def _validate_commit(config: RemoteConfig, snapshot_id: str, commit: dict[str, Any]) -> None:
    if (commit.get("format") != FORMAT or commit.get("snapshot_id") != snapshot_id
            or commit.get("snapshot_format") != config.snapshot_format):
        raise BackupError("Commit identity or format mismatch")
    captured, verified = _time(commit.get("captured_at")), _time(commit.get("verified_at"))
    if captured > verified or verified > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise BackupError("Invalid commit timestamps")
    for kind in ("payload", "manifest"):
        spec = commit.get(kind)
        if not isinstance(spec, dict) or spec.get("key") != _key(config, snapshot_id, kind + ".enc"):
            raise BackupError("Commit references an unexpected object key")
        if type(spec.get("size")) is not int or spec["size"] < 0 or not isinstance(spec.get("sha256"), str) or not SHA_PATTERN.fullmatch(spec["sha256"]):
            raise BackupError("Commit has an invalid object digest or length")
        if "version_id" in spec and (not isinstance(spec["version_id"], str) or not spec["version_id"]):
            raise BackupError("Commit has an invalid version ID")


def read_commit(s3: Any, config: RemoteConfig, snapshot_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _get(s3, config, _key(config, snapshot_id, "COMMITTED.json"), collect=True)
    assert result is not None
    marker, data = result
    commit = _read_json(data)
    _validate_commit(config, snapshot_id, commit)
    return commit, marker


def upload_snapshot(s3: Any, config: RemoteConfig, *, snapshot_id: str, captured_at: str,
                    payload_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """Publish immutable encrypted inputs; returns the verified COMMITTED record."""
    _check_id(snapshot_id)
    if _time(captured_at) > dt.datetime.now(dt.timezone.utc):
        raise BackupError("Capture timestamp is in the future")
    paths = {"payload": Path(payload_path), "manifest": Path(manifest_path)}
    expected = {kind: _local_digest(path) for kind, path in paths.items()}
    with _lock(config):
        existing = _get(s3, config, _key(config, snapshot_id, "COMMITTED.json"), collect=True, missing_ok=True)
        if existing is not None:
            commit = _read_json(existing[1])
            _validate_commit(config, snapshot_id, commit)
            if commit["captured_at"] != captured_at or any(
                commit[kind][field] != expected[kind][field]
                for kind in paths for field in ("size", "sha256")
            ):
                raise BackupError("Snapshot ID is already committed to different inputs")
            for kind in paths:
                _verify(s3, config, commit[kind])
            return commit
        specs = {kind: _put_file(s3, config, path, _key(config, snapshot_id, kind + ".enc"), expected[kind])
                 for kind, path in paths.items()}
        commit = {"format": FORMAT, "snapshot_format": config.snapshot_format,
                  "snapshot_id": snapshot_id, "captured_at": captured_at,
                  "verified_at": utc_now(), **specs}
        encoded = canonical_json(commit)
        marker_key = _key(config, snapshot_id, "COMMITTED.json")
        try:
            s3.put_object(Bucket=config.bucket, Key=marker_key, Body=encoded,
                          ContentLength=len(encoded), IfNoneMatch="*", StorageClass=config.storage_class,
                          ContentType="application/json")
        except Exception:
            # Read-back below is authoritative, including an ambiguous PUT.
            pass
        marker = _get(s3, config, marker_key, collect=True)
        assert marker is not None
        if marker[1] != encoded:
            raise BackupError("Commit marker read-back mismatch")
        return commit


def restore_snapshot(s3: Any, config: RemoteConfig, *, snapshot_id: str,
                     destination: str | Path) -> dict[str, Any]:
    """Download to a NEW directory; leaves .partial on failure, never extracts."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    commit, _ = read_commit(s3, config, snapshot_id)
    for kind in ("payload", "manifest"):
        partial = destination / (kind + ".enc.partial")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            _verify(s3, config, commit[kind], destination=stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, destination / (kind + ".enc"))
        _fsync_dir(destination)
    _atomic_json(destination / "COMMITTED.json", commit)
    return commit


def _list(s3: Any, config: RemoteConfig) -> dict[str, dict[str, Any]]:
    """Reject partial/malformed pagination and names outside the protocol."""
    objects: dict[str, dict[str, Any]] = {}
    token, seen_tokens = None, set()
    while True:
        kwargs = {"Bucket": config.bucket, "Prefix": config.object_prefix, "MaxKeys": 1000}
        if token is not None:
            kwargs["ContinuationToken"] = token
        try:
            page = s3.list_objects_v2(**kwargs)
        except Exception:
            raise BackupError("S3 listing failed; retention is forbidden") from None
        if type(page.get("IsTruncated")) is not bool or not isinstance(page.get("Contents", []), list):
            raise BackupError("Malformed S3 listing; retention is forbidden")
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not isinstance(key, str) or not key.startswith(config.object_prefix) or key in objects:
                raise BackupError("Ambiguous S3 listing; retention is forbidden")
            relative = key[len(config.object_prefix):].split("/")
            if len(relative) != 2 or not ID_PATTERN.fullmatch(relative[0]) or relative[1] not in ("payload.enc", "manifest.enc", "COMMITTED.json"):
                raise BackupError("Unknown object in backup prefix; retention is forbidden")
            if type(obj.get("Size")) is not int or obj["Size"] < 0:
                raise BackupError("Invalid object size in listing")
            objects[key] = obj
        if not page["IsTruncated"]:
            break
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise BackupError("Incomplete or repeated S3 pagination; retention is forbidden")
        seen_tokens.add(token)
    return objects


def _versions(s3: Any, config: RemoteConfig, expected: dict[str, dict[str, Any]]) -> None:
    """Versioned prefixes must have exactly the versions already verified by GET."""
    try:
        status = s3.get_bucket_versioning(Bucket=config.bucket).get("Status")
    except Exception:
        raise BackupError("Cannot establish bucket versioning state; retention is forbidden") from None
    if status is None:
        if any(spec.get("version_id") not in (None, "null") for spec in expected.values()):
            raise BackupError("Unexpected versions in unversioned bucket")
        return
    if status != "Enabled":
        raise BackupError("Suspended or unknown versioning state; retention is forbidden")
    seen, tokens, markers = {}, set(), {}
    while True:
        try:
            page = s3.list_object_versions(Bucket=config.bucket, Prefix=config.object_prefix,
                                           MaxKeys=1000, **markers)
        except Exception:
            raise BackupError("Version listing failed; retention is forbidden") from None
        if type(page.get("IsTruncated")) is not bool or page.get("DeleteMarkers"):
            raise BackupError("Unknown versions/delete markers; retention is forbidden")
        for obj in page.get("Versions", []):
            key = obj.get("Key")
            if key in seen or key not in expected or obj.get("VersionId") != expected[key].get("version_id") or obj.get("IsLatest") is not True:
                raise BackupError("Unexpected object version; retention is forbidden")
            seen[key] = obj["VersionId"]
        if not page["IsTruncated"]:
            break
        token = (page.get("NextKeyMarker"), page.get("NextVersionIdMarker"))
        if not all(isinstance(value, str) and value for value in token) or token in tokens:
            raise BackupError("Incomplete version pagination; retention is forbidden")
        tokens.add(token)
        markers = {"KeyMarker": token[0], "VersionIdMarker": token[1]}
    if set(seen) != set(expected):
        raise BackupError("Version listing differs from verified objects")


def _attestation(config: RemoteConfig, path: Path) -> dict[str, Any]:
    record = _private_json(path)
    if record.get("format") != ATTESTATION_FORMAT or any(record.get(k) != v for k, v in config.scope.items()):
        raise BackupError("Restore attestation does not match backup scope/format")
    _check_id(record.get("snapshot_id"))
    if not isinstance(record.get("commit_sha256"), str) or not SHA_PATTERN.fullmatch(record["commit_sha256"]):
        raise BackupError("Restore attestation lacks a commit digest")
    if _time(record.get("restored_at")) > dt.datetime.now(dt.timezone.utc):
        raise BackupError("Restore attestation timestamp is in the future")
    checks = record.get("checks", {})
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in
                                         ("archive_verified", "isolated_runtime", "rcon_health", "world_loaded")):
        raise BackupError("Complete isolated restore drill is required before retention")
    return record


def _scan(s3: Any, config: RemoteConfig, journal: dict[str, Any] | None = None
          ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    objects = _list(s3, config)
    planned = {spec["key"]: spec for entry in (journal or {}).get("delete", []) for spec in entry["objects"]}
    grouped: dict[str, set[str]] = {}
    for key in objects:
        if key not in planned:
            snapshot_id, filename = key[len(config.object_prefix):].split("/")
            grouped.setdefault(snapshot_id, set()).add(filename)
    commits, verified_objects = {}, {}
    for snapshot_id, files in grouped.items():
        if files != {"payload.enc", "manifest.enc", "COMMITTED.json"}:
            raise BackupError("Incomplete snapshot in prefix; retention is forbidden")
        commit, marker = read_commit(s3, config, snapshot_id)
        verified_objects[marker["key"]] = marker
        for kind in ("payload", "manifest"):
            verified_objects[commit[kind]["key"]] = _verify(s3, config, commit[kind])
        commits[snapshot_id] = {"commit": commit, "marker": marker}
    for key, spec in planned.items():
        if key in objects:
            actual = _verify(s3, config, spec)
            if spec.get("state") == "deleted":
                raise BackupError("Previously deleted object reappeared; retention is forbidden")
            verified_objects[key] = actual
        elif spec.get("state") == "pending":
            raise BackupError("Deletion candidate disappeared before its journaled deletion")
    if set(verified_objects) != set(objects) or any(objects[key]["Size"] != spec["size"] for key, spec in verified_objects.items()):
        raise BackupError("Listing changed during verification; retention is forbidden")
    _versions(s3, config, verified_objects)
    # A second full listing detects additions/removals while checking hashes.
    second = _list(s3, config)
    if {(key, obj["Size"], obj.get("ETag"), str(obj.get("LastModified"))) for key, obj in second.items()} != {
        (key, obj["Size"], obj.get("ETag"), str(obj.get("LastModified"))) for key, obj in objects.items()
    }:
        raise BackupError("Prefix changed during verification; retention is forbidden")
    return commits, verified_objects


def _validate_journal(config: RemoteConfig, journal: dict[str, Any]) -> None:
    if journal.get("format") != JOURNAL_FORMAT or journal.get("scope") != config.scope or journal.get("phase") not in ("planned", "deleting", "complete"):
        raise BackupError("Deletion journal has an invalid scope or phase")
    keepers, deletions = journal.get("keepers"), journal.get("delete")
    expected_keepers = 5 + int(config.protected_snapshot is not None)
    allowed_counts = {5, expected_keepers} if journal["phase"] == "complete" else {expected_keepers}
    if not isinstance(keepers, dict) or len(keepers) not in allowed_counts or not isinstance(deletions, list):
        raise BackupError("Deletion journal has invalid keeper/deletion sets")
    if (journal["phase"] != "complete" and config.protected_snapshot is not None
            and keepers.get(config.protected_snapshot["snapshot_id"]) != config.protected_snapshot["commit_sha256"]):
        raise BackupError("Deletion journal does not preserve protected snapshot")
    for snapshot_id, digest in keepers.items():
        _check_id(snapshot_id)
        if not isinstance(digest, str) or not SHA_PATTERN.fullmatch(digest):
            raise BackupError("Deletion journal has an invalid keeper digest")
    seen = set(keepers)
    for entry in deletions:
        if not isinstance(entry, dict):
            raise BackupError("Invalid deletion journal entry")
        snapshot_id = _check_id(entry.get("snapshot_id"))
        if snapshot_id in seen:
            raise BackupError("Deletion journal repeats or deletes a keeper")
        seen.add(snapshot_id)
        specs = entry.get("objects")
        if not isinstance(specs, list) or len(specs) != 3:
            raise BackupError("Deletion journal must name exactly three objects")
        for spec, filename in zip(specs, ("COMMITTED.json", "payload.enc", "manifest.enc")):
            if not isinstance(spec, dict) or spec.get("key") != _key(config, snapshot_id, filename) or spec.get("state") not in ("pending", "deleting", "deleted"):
                raise BackupError("Deletion journal contains an unsafe object key/state")
            if type(spec.get("size")) is not int or spec["size"] < 0 or not isinstance(spec.get("sha256"), str) or not SHA_PATTERN.fullmatch(spec["sha256"]):
                raise BackupError("Deletion journal has invalid object digest/size")
            if "version_id" in spec and (not isinstance(spec["version_id"], str) or not spec["version_id"]):
                raise BackupError("Deletion journal has invalid object version")


def _retain_once(s3: Any, config: RemoteConfig, *, attestation_path: str | Path,
                 journal_path: str | Path, enabled: bool = False, keep: int = 5) -> dict[str, Any]:
    """Keep five verified snapshots; persist and safely resume exact deletions.

    Retention needs GetBucketVersioning, ListBucket, GetObject, DeleteObject;
    versioned buckets additionally need ListBucketVersions/Get/DeleteObjectVersion.
    Unknown/incomplete objects stop cleanup. Uncommitted objects are never swept.
    """
    if not enabled:
        return {"enabled": False, "deleted_snapshots": 0}
    if keep != 5:
        raise BackupError("This protocol retains exactly five snapshots")
    attestation = _attestation(config, Path(attestation_path))
    path = Path(journal_path)
    with _lock(config):
        journal = _private_json(path) if path.exists() else None
        if journal is not None:
            _validate_journal(config, journal)
            if journal["phase"] == "complete":
                journal = None
        commits, verified = _scan(s3, config, journal)
        protected = config.protected_snapshot
        if protected is not None:
            entry = commits.get(protected["snapshot_id"])
            if entry is None or commit_sha256(entry["commit"]) != protected["commit_sha256"]:
                raise BackupError("Protected snapshot is missing or changed; retention is forbidden")
        restored = commits.get(attestation["snapshot_id"])
        if restored and commit_sha256(restored["commit"]) != attestation["commit_sha256"]:
            raise BackupError("Attested snapshot now has a different commit")
        if journal is None:
            newest = sorted(commits, key=lambda sid: (_time(commits[sid]["commit"]["captured_at"]), sid), reverse=True)
            regular = [sid for sid in newest if protected is None or sid != protected["snapshot_id"]]
            keepers = regular[:keep] + ([protected["snapshot_id"]] if protected is not None else [])
            if len(regular) <= keep:
                return {"enabled": True, "verified_snapshots": len(newest), "deleted_snapshots": 0,
                        "keepers": keepers}
            journal = {"format": JOURNAL_FORMAT, "scope": config.scope, "phase": "planned",
                       "created_at": utc_now(), "keepers": {sid: commit_sha256(commits[sid]["commit"]) for sid in keepers},
                       "delete": []}
            for sid in regular[keep:]:
                journal["delete"].append({"snapshot_id": sid, "objects": [
                    {**verified[_key(config, sid, filename)], "state": "pending"}
                    for filename in ("COMMITTED.json", "payload.enc", "manifest.enc")
                ]})
            _atomic_json(path, journal)
        for sid, digest in journal["keepers"].items():
            if sid not in commits or commit_sha256(commits[sid]["commit"]) != digest:
                raise BackupError("Deletion journal keeper missing or changed; retention is forbidden")
        journal["phase"] = "deleting"
        _atomic_json(path, journal)
        for entry in journal["delete"]:
            for spec in entry["objects"]:
                if spec["state"] == "deleted":
                    continue
                # Journal owns this exact deletion. A missing key after an
                # ambiguous delete is success; it is not evidence for other keys.
                actual = _get(s3, config, spec["key"], spec.get("version_id"), missing_ok=True)
                if actual is None and spec["state"] == "pending":
                    raise BackupError("Deletion candidate disappeared before its journaled deletion")
                if actual is not None:
                    if any(actual[0][field] != spec[field] for field in ("size", "sha256")):
                        raise BackupError("Deletion candidate changed; retention is forbidden")
                    spec["state"] = "deleting"
                    _atomic_json(path, journal)
                    kwargs = {"Bucket": config.bucket, "Key": spec["key"]}
                    if "version_id" in spec:
                        kwargs["VersionId"] = spec["version_id"]
                    try:
                        s3.delete_object(**kwargs)
                    except Exception:
                        pass
                    if _get(s3, config, spec["key"], spec.get("version_id"), missing_ok=True) is not None:
                        raise BackupError("Deletion failed; journal retained for safe resume")
                spec["state"] = "deleted"
                _atomic_json(path, journal)
        journal["phase"] = "complete"
        journal["completed_at"] = utc_now()
        _atomic_json(path, journal)
        remaining = set(commits) - {entry["snapshot_id"] for entry in journal["delete"]}
        return {"enabled": True, "verified_snapshots": len(remaining),
                "deleted_snapshots": len(journal["delete"]), "keepers": sorted(journal["keepers"])}


def retain_snapshots(s3: Any, config: RemoteConfig, *, attestation_path: str | Path,
                     journal_path: str | Path, enabled: bool = False, keep: int = 5) -> dict[str, Any]:
    """Safely resume deletion, retaining five snapshots and an optional base."""
    deleted = 0
    for _ in range(8):
        result = _retain_once(s3, config, attestation_path=attestation_path,
                              journal_path=journal_path, enabled=enabled, keep=keep)
        deleted += result["deleted_snapshots"]
        result["deleted_snapshots"] = deleted
        if result.get("verified_snapshots", 0) <= keep + int(config.protected_snapshot is not None):
            return result
        # Snapshots published since an interrupted cleanup may leave more than
        # five after resume. Re-plan against a fresh complete listing instead
        # of silently keeping an older keeper set until the next scheduled run.
    raise BackupError("Prefix is changing continuously; retry retention later")


def multipart_inventory(s3: Any, config: RemoteConfig) -> int:
    """Count only this prefix after complete pagination; never infer zero on error."""
    count, markers, seen = 0, {}, set()
    while True:
        page = s3.list_multipart_uploads(Bucket=config.bucket, Prefix=config.object_prefix, **markers)
        if type(page.get("IsTruncated")) is not bool or not isinstance(page.get("Uploads", []), list):
            raise BackupError("Incomplete multipart inventory")
        for upload in page.get("Uploads", []):
            if not isinstance(upload, dict) or not isinstance(upload.get("Key"), str) or not upload["Key"].startswith(config.object_prefix):
                raise BackupError("Unexpected multipart inventory prefix")
            count += 1
        if not page["IsTruncated"]:
            return count
        marker = (page.get("NextKeyMarker"), page.get("NextUploadIdMarker"))
        if marker in seen or not all(isinstance(value, str) and value for value in marker):
            raise BackupError("Incomplete multipart inventory pagination")
        seen.add(marker)
        markers = {"KeyMarker": marker[0], "UploadIdMarker": marker[1]}


def write_remote_status(path: Path, *, result: dict[str, Any] | None = None,
                        error: Exception | None = None, incomplete_multipart: int | None = None) -> None:
    """Persist bounded aggregate facts for the credential-free metrics process."""
    report: dict[str, Any] = {"format": "pz-backup-remote-status-v1", "observed_at": utc_now(),
                              "verification_failed": error is not None}
    if result is not None and type(result.get("verified_snapshots")) is int and result["verified_snapshots"] >= 0:
        report["verified_snapshots"] = result["verified_snapshots"]
    if type(incomplete_multipart) is int and incomplete_multipart >= 0:
        report["incomplete_multipart"] = incomplete_multipart
    if error is not None:
        message = str(error).lower() if isinstance(error, BackupError) else ""
        if "sha256" in message or "checksum" in message:
            failure = "checksum"
        elif "multipart cleanup" in message:
            failure = "multipart_cleanup"
        elif any(word in message for word in ("local", "credentials", "ready", "input")):
            failure = "local_input"
        else:
            failure = "protocol" if isinstance(error, BackupError) else "remote_io"
        report["verification_failure_class"] = failure
    _atomic_json(path, report)


def _conditional_multipart_headers(request: Any, **_kwargs: Any) -> None:
    # YC checks its mandatory-header policy on multipart initiation and parts.
    # Their SDK models lack IfNoneMatch: add the header before SigV4 signing.
    # PutObject and completion retain their explicit conditional arguments.
    request.headers["If-None-Match"] = "*"


def create_client(config: RemoteConfig, credentials_file: str | Path) -> Any:
    """Read explicit per-role credentials without falling back to ambient IAM."""
    credentials = _private_json(Path(credentials_file))
    allowed = {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"}
    if set(credentials) - allowed or not all(isinstance(credentials.get(k), str) and credentials[k]
                                            for k in ("aws_access_key_id", "aws_secret_access_key")):
        raise BackupError("Invalid S3 credentials file")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise BackupError("Install the pinned boto3 dependency before using S3") from None
    sdk_options = {"signature_version": "s3v4", "connect_timeout": 15, "read_timeout": 120,
                   "retries": {"max_attempts": 2, "mode": "standard"}, "s3": {"addressing_style": "path"}}
    # Avoid optional AWS streaming CRC trailers on an S3-compatible endpoint.
    # Full streamed SHA256 read-back remains mandatory regardless of SDK checks.
    for option in ("request_checksum_calculation", "response_checksum_validation"):
        if option in Config.OPTION_DEFAULTS:
            sdk_options[option] = "when_required"
    client = boto3.client("s3", endpoint_url=config.endpoint_url, region_name=config.region_name,
                          config=Config(**sdk_options), **credentials)
    for operation in ("PutObject", "CompleteMultipartUpload"):
        if "IfNoneMatch" not in client.meta.service_model.operation_model(operation).input_shape.members:
            raise BackupError("Installed S3 SDK is too old for mandatory conditional writes")
    for operation in ("CreateMultipartUpload", "UploadPart"):
        client.meta.events.register("before-sign.s3." + operation, _conditional_multipart_headers)
    return client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON object containing RemoteConfig fields")
    parser.add_argument("--credentials-file", required=True, help="Private JSON credentials for this role")
    sub = parser.add_subparsers(dest="command", required=True)
    upload = sub.add_parser("upload")
    upload.add_argument("--snapshot-id", required=True)
    upload.add_argument("--captured-at", required=True)
    upload.add_argument("--payload", required=True)
    upload.add_argument("--manifest", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--snapshot-id", required=True)
    restore.add_argument("--destination", required=True)
    retain = sub.add_parser("retain")
    retain.add_argument("--enable-retention", action="store_true")
    retain.add_argument("--attestation", required=True)
    retain.add_argument("--journal", required=True)
    args = parser.parse_args(argv)
    try:
        config = RemoteConfig(**_private_json(Path(args.config)))
        s3 = create_client(config, args.credentials_file)
        if args.command == "upload":
            result = upload_snapshot(s3, config, snapshot_id=args.snapshot_id, captured_at=args.captured_at,
                                     payload_path=args.payload, manifest_path=args.manifest)
            output = {"snapshot_id": result["snapshot_id"], "committed": True,
                      "payload_size": result["payload"]["size"], "commit_sha256": commit_sha256(result)}
        elif args.command == "restore":
            result = restore_snapshot(s3, config, snapshot_id=args.snapshot_id, destination=args.destination)
            output = {"snapshot_id": result["snapshot_id"], "download_verified": True,
                      "commit_sha256": commit_sha256(result), "runtime_drill_required": True}
        else:
            output = retain_snapshots(s3, config, attestation_path=args.attestation,
                                      journal_path=args.journal, enabled=args.enable_retention)
            write_remote_status(Path(args.journal).parent / "remote-status.json", result=output)
        print(json.dumps(output, sort_keys=True))
        return 0
    except BackupError as error:
        if args.command == "retain":
            write_remote_status(Path(args.journal).parent / "remote-status.json", error=error)
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        if args.command == "retain":
            write_remote_status(Path(args.journal).parent / "remote-status.json", error=error)
        # SDK exceptions can include signed URLs and endpoint response bodies.
        print("Backup operation failed (" + type(error).__name__ + "); no retention is authorized", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
