#!/usr/bin/env python3
"""Decrypt and verify a downloaded PZ backup in a NEW isolated directory.

This restores archive contents only. It never starts a server, opens public
ports, modifies production paths, imports images, or grants retention approval.
The private age identity must be supplied independently of the game machine.
"""

from __future__ import annotations

import argparse
import base64
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
from typing import Any, BinaryIO

import remote


MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 2_000_000
FORBIDDEN = tuple(Path(path) for path in (
    "/srv/pz-storage", "/srv/pz-backup-spool", "/opt/pz-stack", "/var/lib/pz-volumes",
    "/var/lib/rancher", "/etc", "/usr", "/boot", "/dev", "/proc", "/sys",
))


class RestoreError(RuntimeError):
    """Fixed, safe-to-log validation failure."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RestoreError(reason)


def safe_name(value: Any) -> str:
    require(isinstance(value, str) and value and "\x00" not in value and "\\" not in value,
            "invalid_archive_path")
    require(not value.startswith("/") and all(part not in ("", ".", "..") for part in value.split("/")),
            "unsafe_archive_path")
    require(value.split("/")[0] in ("data", "recovery"), "unexpected_archive_root")
    return value


def _symlink_destination(name: str, target: str) -> str:
    require(isinstance(target, str) and target and not target.startswith("/") and "\x00" not in target
            and "\\" not in target, "unsafe_symlink_target")
    components = name.split("/")[:-1]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            require(bool(components), "escaping_symlink_rejected")
            components.pop()
        else:
            components.append(part)
    return "/".join(components)


def validate_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    require(manifest.get("format") == "pz-backup-v1", "unsupported_snapshot_format")
    require(isinstance(manifest.get("snapshot_id"), str)
            and remote.ID_PATTERN.fullmatch(manifest["snapshot_id"]), "invalid_snapshot_identity")
    remote._time(manifest.get("captured_at"))
    rows = manifest.get("files")
    require(isinstance(rows, list) and 2 <= len(rows) <= MAX_ENTRIES, "invalid_manifest_inventory")
    expected: dict[str, dict[str, Any]] = {}
    for item in rows:
        require(isinstance(item, dict), "invalid_manifest_record")
        name = safe_name(item.get("path"))
        require(name not in expected, "duplicate_manifest_path")
        require(item.get("type") in ("file", "dir", "symlink"), "special_file_rejected")
        require(all(type(item.get(field)) is int and 0 <= item[field] <= 2**32 - 1 for field in ("uid", "gid")),
                "invalid_numeric_ownership")
        require(type(item.get("mode")) is int and 0 <= item["mode"] <= 0o7777, "invalid_file_mode")
        require(type(item.get("mtime_ns")) is int, "invalid_file_mtime")
        attrs = item.get("xattrs")
        require(isinstance(attrs, dict), "invalid_extended_attributes")
        for key, value in attrs.items():
            require(isinstance(key, str) and key and "\x00" not in key and isinstance(value, str),
                    "invalid_extended_attributes")
            try:
                base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                raise RestoreError("invalid_extended_attributes") from None
        if item["type"] == "file":
            require(type(item.get("size")) is int and item["size"] >= 0, "invalid_file_size")
            require(isinstance(item.get("sha256"), str) and remote.SHA_PATTERN.fullmatch(item["sha256"]),
                    "invalid_file_digest")
            if "hardlink" in item:
                safe_name(item["hardlink"])
                require(item["hardlink"] != name, "self_hardlink_rejected")
        elif item["type"] == "symlink":
            _symlink_destination(name, item.get("target"))
            require("hardlink" not in item, "invalid_link_record")
        else:
            require("hardlink" not in item, "invalid_directory_record")
        expected[name] = item
    require(all(root in expected and expected[root]["type"] == "dir" for root in ("data", "recovery")),
            "manifest_roots_missing")
    for name, item in expected.items():
        for parent in PurePosixPath(name).parents:
            if str(parent) != ".":
                require(str(parent) in expected and expected[str(parent)]["type"] == "dir",
                        "non_directory_archive_ancestor")
        if "hardlink" in item:
            target = expected.get(item["hardlink"], {})
            require(target.get("type") == "file" and "hardlink" not in target, "invalid_hardlink_target")
            require(all(item[field] == target[field] for field in ("size", "sha256", "uid", "gid", "mode", "mtime_ns", "xattrs")),
                    "hardlink_metadata_mismatch")
        if item["type"] == "symlink":
            # Resolve through the manifest, with a bound against cycles. A link
            # cannot escape its original data/recovery tree even indirectly.
            target = _symlink_destination(name, item["target"])
            visited = {name}
            for _ in range(40):
                require(target.split("/")[0] == name.split("/")[0], "cross_root_symlink_rejected")
                require(target in expected and target not in visited, "dangling_or_cyclic_symlink")
                visited.add(target)
                replacement = None
                parts = target.split("/")
                for index in range(1, len(parts) + 1):
                    segment = "/".join(parts[:index])
                    if segment in expected and expected[segment]["type"] == "symlink":
                        resolved = _symlink_destination(segment, expected[segment]["target"])
                        replacement = "/".join([resolved, *parts[index:]])
                        break
                if replacement is None:
                    break
                target = replacement
            else:
                raise RestoreError("symlink_resolution_limit")
    return expected


def prepare_destination(destination: str | Path) -> Path:
    path = Path(destination).absolute()
    require(".." not in path.parts, "unsafe_restore_parent")
    require(path.name not in ("", ".", ".."), "invalid_restore_destination")
    require(not any(path == blocked or blocked in path.parents for blocked in FORBIDDEN),
            "production_or_system_restore_path_rejected")
    for parent in reversed(path.parents):
        require(parent.exists() and parent.is_dir() and not parent.is_symlink(), "unsafe_restore_parent")
    require(not path.exists() and not path.is_symlink(), "restore_destination_must_be_new")
    path.mkdir(mode=0o700)
    remote._fsync_dir(path.parent)
    return path


class BudgetReader:
    def __init__(self, source: BinaryIO, limit: int):
        self.source, self.limit, self.count = source, limit, 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self.limit - self.count + 1
        data = self.source.read(min(size, self.limit - self.count + 1))
        self.count += len(data)
        require(self.count <= self.limit, "archive_decompression_budget_exceeded")
        return data


def _metadata(path: Path, item: dict[str, Any]) -> None:
    info = path.lstat()
    if (info.st_uid, info.st_gid) != (item["uid"], item["gid"]):
        os.chown(path, item["uid"], item["gid"], follow_symlinks=False)
    if item["type"] != "symlink":
        os.chmod(path, item["mode"], follow_symlinks=False)
    current = set(os.listxattr(path, follow_symlinks=False))
    for key in current - set(item["xattrs"]):
        os.removexattr(path, key, follow_symlinks=False)
    for key, encoded in item["xattrs"].items():
        os.setxattr(path, key, base64.b64decode(encoded, validate=True), follow_symlinks=False)
    os.utime(path, ns=(item["mtime_ns"], item["mtime_ns"]), follow_symlinks=False)


def _verify_extracted(destination: Path, expected: dict[str, dict[str, Any]]) -> None:
    found = set()
    pending = [destination / "data", destination / "recovery"]
    root_device = destination.lstat().st_dev
    while pending:
        path = pending.pop()
        name = path.relative_to(destination).as_posix()
        require(name in expected and name not in found, "restored_inventory_mismatch")
        found.add(name)
        item, info = expected[name], path.lstat()
        require(info.st_dev == root_device, "nested_restore_mount_rejected")
        require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_mtime_ns) ==
                (item["uid"], item["gid"], item["mode"], item["mtime_ns"]), "restored_metadata_mismatch")
        attrs = {key: base64.b64encode(os.getxattr(path, key, follow_symlinks=False)).decode("ascii")
                 for key in os.listxattr(path, follow_symlinks=False)}
        require(attrs == item["xattrs"], "restored_extended_attributes_mismatch")
        if item["type"] == "dir":
            require(stat.S_ISDIR(info.st_mode), "restored_file_type_mismatch")
            pending.extend(path.iterdir())
        elif item["type"] == "symlink":
            require(stat.S_ISLNK(info.st_mode) and os.readlink(path) == item["target"], "restored_symlink_mismatch")
            try:
                resolved = path.resolve(strict=True)
                require((destination / name.split("/")[0]) in resolved.parents
                        or resolved == destination / name.split("/")[0], "restored_symlink_escape")
            except (OSError, RuntimeError):
                raise RestoreError("restored_symlink_resolution_failed") from None
        else:
            require(stat.S_ISREG(info.st_mode), "restored_file_type_mismatch")
            digest = remote._local_digest(path)
            require(all(digest[field] == item[field] for field in ("size", "sha256")), "restored_sha256_mismatch")
            if "hardlink" in item:
                target = (destination / item["hardlink"]).lstat()
                require((info.st_dev, info.st_ino) == (target.st_dev, target.st_ino), "restored_hardlink_mismatch")
    require(found == set(expected), "restored_inventory_mismatch")


def extract_verified(stream: BinaryIO, manifest: dict[str, Any], manifest_bytes: bytes,
                     destination: str | Path) -> dict[str, Any]:
    """Create destination and extract only manifest records; preserve metadata."""
    expected = validate_manifest(manifest)
    destination = prepare_destination(destination)
    logical_size = sum(item.get("size", 0) for item in expected.values())
    available = os.statvfs(destination)
    require(available.f_bavail * available.f_frsize >= logical_size + len(manifest_bytes) + 64 * 1024 * 1024,
            "insufficient_restore_space")
    require(available.f_favail >= len(expected) + 128, "insufficient_restore_inodes")
    stream = BudgetReader(stream, logical_size + len(manifest_bytes) * 8 + (len(expected) + 1) * 8192 + 1024 * 1024)
    seen, deferred_links = set(), []
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = member.name.rstrip("/") if member.isdir() else member.name
            require(name not in seen, "archive_duplicate_member")
            seen.add(name)
            if name == "manifest.json":
                require(member.isfile() and member.size == len(manifest_bytes), "archive_manifest_type_or_size_mismatch")
                require(archive.extractfile(member).read() == manifest_bytes, "archive_manifest_mismatch")
                continue
            safe_name(name)
            require(name in expected, "archive_unexpected_member")
            item, path = expected[name], destination / name
            require((member.uid, member.gid, member.mode) == (item["uid"], item["gid"], item["mode"]),
                    "archive_metadata_mismatch")
            try:
                mtime_ns = int(Decimal(member.pax_headers.get("mtime", str(member.mtime))) * 10**9)
            except (InvalidOperation, ValueError, OverflowError):
                raise RestoreError("archive_invalid_mtime") from None
            require(mtime_ns == item["mtime_ns"], "archive_mtime_mismatch")
            attrs = {key.removeprefix("SCHILY.xattr."): base64.b64encode(value.encode("utf-8", "surrogateescape")).decode("ascii")
                     for key, value in member.pax_headers.items() if key.startswith("SCHILY.xattr.")}
            # GNU tar writes inode xattrs only on the first regular member;
            # hardlinks inherit those attributes from their verified target.
            require(attrs == item["xattrs"] or (member.islnk() and "hardlink" in item and not attrs),
                    "archive_extended_attributes_mismatch")
            require(path.parent == destination or (path.parent.is_dir() and not path.parent.is_symlink()),
                    "archive_parent_missing_or_unsafe")
            if item["type"] == "dir":
                require(member.isdir(), "archive_directory_type_mismatch")
                path.mkdir(mode=0o700)
            elif item["type"] == "symlink":
                require(member.issym() and member.linkname == item["target"], "archive_symlink_mismatch")
                deferred_links.append((path, item))
            elif member.islnk():
                require(item.get("hardlink") == member.linkname and member.linkname in seen,
                        "archive_hardlink_mismatch")
                target = destination / safe_name(member.linkname)
                require(target.is_file() and not target.is_symlink(), "archive_hardlink_target_invalid")
                os.link(target, path, follow_symlinks=False)
            else:
                require(member.isfile() and member.size == item["size"], "archive_file_type_or_size_mismatch")
                content, digest, size = archive.extractfile(member), hashlib.sha256(), 0
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := content.read(remote.CHUNK):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                require(size == item["size"] and digest.hexdigest() == item["sha256"], "archive_sha256_mismatch")
                if "hardlink" in item:
                    target = destination / item["hardlink"]
                    require(item["hardlink"] in seen and target.is_file() and not target.is_symlink(),
                            "archive_hardlink_target_invalid")
                    path.unlink()
                    os.link(target, path, follow_symlinks=False)
    require(seen == set(expected) | {"manifest.json"}, "archive_inventory_mismatch")
    # Drain the encrypted/compressed pipeline so a late age authentication or
    # zstd checksum failure cannot be mistaken for a successful restore.
    while stream.read(remote.CHUNK):
        pass
    for path, item in deferred_links:
        os.symlink(item["target"], path)
    # Keep directories private and writable until their children are complete.
    for name in sorted(expected, key=lambda value: (value.count("/"), value), reverse=True):
        _metadata(destination / name, expected[name])
    _verify_extracted(destination, expected)
    remote._atomic_json(destination / "manifest.json", manifest)
    for name, item in expected.items():
        if item["type"] == "dir":
            remote._fsync_dir(destination / name)
    remote._fsync_dir(destination)
    return {"snapshot_id": manifest["snapshot_id"], "archive_verified": True,
            "verified_entries": len(expected), "logical_bytes": logical_size,
            "runtime_drill_required": True}


def _decrypt_manifest(source: Path, identity: Path) -> bytes:
    process = subprocess.Popen(["age", "--decrypt", "--identity", str(identity), str(source)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        data = process.stdout.read(MAX_MANIFEST_BYTES + 1)
        require(len(data) <= MAX_MANIFEST_BYTES, "decrypted_manifest_exceeds_limit")
        require(process.wait(timeout=30) == 0, "manifest_decryption_failed")
        return data
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def restore_download(download_dir: str | Path, identity_path: str | Path,
                     destination: str | Path) -> dict[str, Any]:
    source, identity = Path(download_dir), Path(identity_path)
    require(source.is_dir() and not source.is_symlink(), "invalid_download_directory")
    identity_info = identity.lstat()
    require(stat.S_ISREG(identity_info.st_mode) and identity_info.st_mode & 0o077 == 0,
            "age_identity_must_be_private_regular_file")
    require(identity_info.st_uid in (0, os.geteuid()), "age_identity_owner_invalid")
    marker = remote._private_json(source / "COMMITTED.json")
    require(marker.get("format") == remote.FORMAT and marker.get("snapshot_format") == "pz-backup-v1",
            "unsupported_commit_format")
    remote._check_id(marker.get("snapshot_id"))
    for kind in ("payload", "manifest"):
        require(isinstance(marker.get(kind), dict), "invalid_commit_object")
        digest = remote._local_digest(source / (kind + ".enc"))
        require(all(digest[field] == marker[kind].get(field) for field in ("size", "sha256")),
                "downloaded_ciphertext_sha256_mismatch")
    plain = _decrypt_manifest(source / "manifest.enc", identity)
    manifest = remote._read_json(plain)
    require(manifest.get("snapshot_id") == marker["snapshot_id"] and manifest.get("captured_at") == marker.get("captured_at"),
            "encrypted_manifest_commit_identity_mismatch")
    # Validate inventory before starting the potentially large payload pipeline.
    validate_manifest(manifest)
    age = subprocess.Popen(["age", "--decrypt", "--identity", str(identity), str(source / "payload.enc")],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    zstd = None
    try:
        zstd = subprocess.Popen(["zstd", "--decompress", "--stdout", "--quiet"], stdin=age.stdout,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        age.stdout.close()
        report = extract_verified(zstd.stdout, manifest, plain, destination)
        require(zstd.wait(timeout=30) == 0 and age.wait(timeout=30) == 0, "payload_decryption_or_decompression_failed")
        report["commit_sha256"] = remote.commit_sha256(marker)
        report["verified_at"] = remote.utc_now()
        remote._atomic_json(Path(destination) / "restored.json", report)
        return report
    finally:
        for process in (zstd, age):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(restore_download(args.download_dir, args.identity, args.destination), sort_keys=True))
        return 0
    except (RestoreError, remote.BackupError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print("restore_failed_" + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
