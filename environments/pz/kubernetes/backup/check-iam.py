#!/usr/bin/env python3
"""Exercise S3 backup credentials on disposable, journaled canary objects.

No DeleteBucket/PutBucketPolicy request is ever sent. Administrative permissions
are checked only in a supplied bucket-policy fixture; folder/account IAM roles
must also be checked by the operator. This probe requires an unversioned bucket.
Canary keys are outside the snapshot protocol and block retention until cleaned.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
from typing import Any, Callable

import remote


ROLES = ("uploader", "retainer", "restore")
NAMES = ("single", "multipart", "COMMITTED.json", "restore-put", "retainer-put")
JOURNAL_FORMAT = "pz-backup-iam-canary-v1"


def _values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise remote.BackupError("Unsupported policy fixture value")


def _conditions(conditions: dict[str, Any], context: dict[str, str]) -> bool:
    for operation, mapping in conditions.items():
        if operation not in ("Null", "Bool", "StringEquals", "StringLike") or not isinstance(mapping, dict):
            raise remote.BackupError("Unsupported policy fixture condition")
        for key, values in mapping.items():
            matches = []
            for value in _values(values):
                if operation == "Null":
                    matches.append((key not in context) == (value.lower() == "true"))
                elif operation == "Bool":
                    matches.append(key in context and context[key].lower() == value.lower())
                elif operation == "StringEquals":
                    matches.append(context.get(key) == value)
                else:
                    matches.append(key in context and fnmatch.fnmatchcase(context[key], value))
            if not any(matches):
                return False
    return True


def _allowed(policy: dict[str, Any], principal: str, action: str, resource: str,
             context: dict[str, str]) -> bool:
    allowed = False
    for statement in policy["Statement"]:
        if set(statement) - {"Sid", "Effect", "Principal", "Action", "Resource", "Condition"}:
            raise remote.BackupError("Unsupported bucket-policy fixture statement")
        identity = statement.get("Principal")
        if identity != "*":
            if not isinstance(identity, dict) or set(identity) != {"CanonicalUser"}:
                raise remote.BackupError("Unsupported policy principal")
            if principal not in _values(identity["CanonicalUser"]):
                continue
        if not any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in _values(statement.get("Action"))):
            continue
        if not any(fnmatch.fnmatchcase(resource, pattern) for pattern in _values(statement.get("Resource"))):
            continue
        if not _conditions(statement.get("Condition", {}), context):
            continue
        if statement.get("Effect") == "Deny":
            return False
        if statement.get("Effect") != "Allow":
            raise remote.BackupError("Invalid bucket-policy effect")
        allowed = True
    return allowed


def check_policy_fixture(policy: dict[str, Any], config: remote.RemoteConfig) -> dict[str, bool]:
    """Evaluate the deliberately small fixture dialect used by backup-cloud."""
    if not isinstance(policy.get("Statement"), list):
        raise remote.BackupError("Invalid bucket-policy fixture")
    principals = {}
    for role in ROLES:
        entries = [statement for statement in policy["Statement"] if statement.get("Sid") == role + "Objects"]
        if len(entries) != 1 or not isinstance(entries[0].get("Principal"), dict):
            raise remote.BackupError("Policy fixture must identify each role with its Objects Sid")
        principal = entries[0]["Principal"].get("CanonicalUser")
        if not isinstance(principal, str) or not principal:
            raise remote.BackupError("Policy fixture principal is invalid")
        principals[role] = principal
    if len(set(principals.values())) != 3:
        raise remote.BackupError("Backup roles must use three distinct principals")
    bucket = "arn:aws:s3:::" + config.bucket
    own = bucket + "/" + config.object_prefix + "fixture"
    outside = bucket + "/_pz-iam-outside/fixture"
    base = {"aws:SecureTransport": "true", "s3:ObjectCreationOperation": "true"}
    conditional = {**base, "s3:if-none-match": "*"}
    results = {}
    for role, principal in principals.items():
        allow = lambda action, resource=own, context=base: _allowed(policy, principal, action, resource, context)
        results[role + "_get_allowed"] = allow("s3:GetObject")
        results[role + "_list_scoped"] = allow("s3:ListBucket", bucket, {**base, "s3:prefix": config.object_prefix})
        results[role + "_outside_get_denied"] = not allow("s3:GetObject", outside)
        results[role + "_outside_put_denied"] = not allow("s3:PutObject", outside, conditional)
        results[role + "_outside_delete_denied"] = not allow("s3:DeleteObject", outside)
        results[role + "_outside_list_denied"] = not allow("s3:ListBucket", bucket, {**base, "s3:prefix": "_pz-iam-outside/"})
        results[role + "_delete_bucket_denied_fixture"] = not allow("s3:DeleteBucket", bucket)
        results[role + "_put_bucket_policy_denied_fixture"] = not allow("s3:PutBucketPolicy", bucket)
        results[role + "_http_denied"] = not allow("s3:GetObject", own, {"aws:SecureTransport": "false"})
        results[role + "_versioning_read_allowed"] = allow("s3:GetBucketVersioning", bucket)
        results[role + "_put_contract"] = allow("s3:PutObject", own, conditional) == (role == "uploader")
        results[role + "_delete_contract"] = allow("s3:DeleteObject") == (role == "retainer")
    results["uploader_unconditional_put_denied"] = not _allowed(policy, principals["uploader"], "s3:PutObject", own, base)
    results["uploader_multipart_abort_allowed"] = _allowed(policy, principals["uploader"], "s3:AbortMultipartUpload", own, base)
    results["uploader_multipart_inventory_allowed"] = _allowed(policy, principals["uploader"], "s3:ListBucketMultipartUploads", bucket, base)
    results["unconfigured_principal_get_denied"] = not _allowed(policy, "_unconfigured", "s3:GetObject", own, base)
    results["unconfigured_principal_put_denied"] = not _allowed(policy, "_unconfigured", "s3:PutObject", own, conditional)
    return results


def _denied(call: Callable[[], Any], codes: tuple[str, ...] = ("AccessDenied",)) -> bool:
    try:
        result = call()
        if isinstance(result, dict) and hasattr(result.get("Body"), "close"):
            result["Body"].close()
        return False
    except Exception as error:
        response = getattr(error, "response", {})
        return response.get("Error", {}).get("Code") in codes


def _digest(client: Any, config: remote.RemoteConfig, key: str, expected: bytes) -> bool:
    result = remote._get(client, config, key)
    return result is not None and result[0]["size"] == len(expected) and result[0]["sha256"] == hashlib.sha256(expected).hexdigest()


def _validate_journal(config: remote.RemoteConfig, journal: dict[str, Any]) -> dict[str, str]:
    identifier = journal.get("canary_id")
    if journal.get("format") != JOURNAL_FORMAT or journal.get("scope") != config.scope or not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise remote.BackupError("Invalid canary cleanup journal")
    keys = {name: config.object_prefix + "_iam-check/" + identifier + "/" + name for name in NAMES}
    if journal.get("objects") != list(keys.values()) or journal.get("phase") not in ("testing", "cleaning", "complete"):
        raise remote.BackupError("Unsafe canary cleanup journal")
    return keys


def cleanup_canary(clients: dict[str, Any], config: remote.RemoteConfig, journal_path: str | Path) -> bool:
    """Caller holds remote lock. Delete only the five exact journal-owned keys."""
    path = Path(journal_path)
    journal = remote._private_json(path)
    keys = _validate_journal(config, journal)
    journal["phase"] = "cleaning"
    remote._atomic_json(path, journal)
    uploader, retainer = clients["uploader"], clients["retainer"]
    if retainer.get_bucket_versioning(Bucket=config.bucket).get("Status"):
        raise remote.BackupError("Canary cleanup requires the original unversioned bucket")
    prefix = config.object_prefix + "_iam-check/" + journal["canary_id"] + "/"
    markers, seen, uploads = {}, set(), []
    while True:
        page = uploader.list_multipart_uploads(Bucket=config.bucket, Prefix=prefix, **markers)
        if type(page.get("IsTruncated")) is not bool:
            raise remote.BackupError("Incomplete canary multipart listing")
        for upload in page.get("Uploads", []):
            if upload.get("Key") not in keys.values() or not isinstance(upload.get("UploadId"), str):
                raise remote.BackupError("Unknown multipart in canary prefix")
            uploads.append(upload)
        if not page["IsTruncated"]:
            break
        next_marker = (page.get("NextKeyMarker"), page.get("NextUploadIdMarker"))
        if next_marker in seen or not all(isinstance(item, str) and item for item in next_marker):
            raise remote.BackupError("Ambiguous canary multipart pagination")
        seen.add(next_marker)
        markers = {"KeyMarker": next_marker[0], "UploadIdMarker": next_marker[1]}
    for upload in uploads:
        try:
            uploader.abort_multipart_upload(Bucket=config.bucket, Key=upload["Key"], UploadId=upload["UploadId"])
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "NoSuchUpload":
                raise remote.BackupError("Canary multipart cleanup failed") from None
    remaining = uploader.list_multipart_uploads(Bucket=config.bucket, Prefix=prefix)
    if remaining.get("IsTruncated") is not False or remaining.get("Uploads"):
        raise remote.BackupError("Canary multipart cleanup is incomplete")
    for key in keys.values():
        if remote._get(retainer, config, key, missing_ok=True) is None:
            continue
        try:
            retainer.delete_object(Bucket=config.bucket, Key=key)
        except Exception:
            pass
        if remote._get(retainer, config, key, missing_ok=True) is not None:
            raise remote.BackupError("Canary object cleanup failed")
    remaining_objects = retainer.list_objects_v2(Bucket=config.bucket, Prefix=prefix)
    if remaining_objects.get("IsTruncated") is not False or remaining_objects.get("Contents"):
        raise remote.BackupError("Canary prefix is not empty after exact cleanup")
    journal["phase"] = "complete"
    journal["completed_at"] = remote.utc_now()
    remote._atomic_json(path, journal)
    return True


def run_canary(clients: dict[str, Any], config: remote.RemoteConfig, *, policy: dict[str, Any],
               report_path: str | Path, journal_path: str | Path) -> dict[str, Any]:
    """Run read/write canaries only on five newly chosen disposable object keys."""
    fixture = check_policy_fixture(policy, config)
    report: dict[str, Any] = {"format": "pz-backup-iam-report-v1", "started_at": remote.utc_now(),
                             "policy_sha256": hashlib.sha256(remote.canonical_json(policy)).hexdigest(),
                             "policy_fixture": fixture, "endpoint_checks": {}, "passed": False,
                             "cleanup_complete": False,
                             "not_executed_live": ["DeleteBucket", "PutBucketPolicy", "outside-prefix PutObject", "outside-prefix DeleteObject", "existing outside-prefix GetObject"],
                             "outside_get_note": "Missing-key GET may return NoSuchKey before authorization; no-data result is not proof of denied access to an existing object. Existing-object isolation is checked by policy fixture only.",
                             "operator_checks_required": ["supplied policy matches deployed policy", "no broad folder/account IAM grants"]}
    report_path, journal_path = Path(report_path), Path(journal_path)
    remote._atomic_json(report_path, report)
    if not all(fixture.values()):
        report["failure"] = "bucket_policy_fixture_contract_failed"
        remote._atomic_json(report_path, report)
        return report
    checks = report["endpoint_checks"]

    def check(name: str, operation: Callable[[], bool]) -> None:
        try:
            checks[name] = operation() is True
        except Exception as error:
            checks[name] = False
            report.setdefault("failure_types", {})[name] = type(error).__name__
        remote._atomic_json(report_path, report)

    with remote._lock(config):
        if journal_path.exists() and remote._private_json(journal_path).get("phase") != "complete":
            raise remote.BackupError("Prior canary requires cleanup before a new run")
        # No versioned canaries: ordinary deletion would leave charged versions.
        for role in ROLES:
            check(role + "_bucket_unversioned", lambda role=role: not clients[role].get_bucket_versioning(Bucket=config.bucket).get("Status"))
        if not all(checks.values()):
            report["failure"] = "unversioned_bucket_precondition_failed"
            remote._atomic_json(report_path, report)
            return report
        identifier = uuid.uuid4().hex
        keys = {name: config.object_prefix + "_iam-check/" + identifier + "/" + name for name in NAMES}
        journal = {"format": JOURNAL_FORMAT, "scope": config.scope, "canary_id": identifier,
                   "phase": "testing", "objects": list(keys.values()), "created_at": remote.utc_now()}
        remote._atomic_json(journal_path, journal)
        report["canary_id"] = identifier
        single = b"pz-backup-safe-iam-canary-v1\n" + identifier.encode("ascii")
        committed = remote.canonical_json({"canary": True, "id": identifier})
        uploader, retainer, reader = (clients[role] for role in ROLES)

        def put(key: str, data: bytes) -> bool:
            try:
                uploader.put_object(Bucket=config.bucket, Key=key, Body=data, ContentLength=len(data), IfNoneMatch="*")
            except Exception:
                # Reconcile only this journal-owned key after ambiguous upload.
                return _digest(uploader, config, key, data)
            return _digest(uploader, config, key, data)

        def multipart() -> bool:
            key = keys["multipart"]
            upload = uploader.create_multipart_upload(Bucket=config.bucket, Key=key)
            part1, part2 = b"p" * (5 * 1024 * 1024), single
            parts = []
            for number, data in enumerate((part1, part2), 1):
                part = uploader.upload_part(Bucket=config.bucket, Key=key, UploadId=upload["UploadId"], PartNumber=number, Body=data)
                parts.append({"PartNumber": number, "ETag": part["ETag"]})
            try:
                uploader.complete_multipart_upload(Bucket=config.bucket, Key=key, UploadId=upload["UploadId"],
                                                   MultipartUpload={"Parts": parts}, IfNoneMatch="*")
            except Exception:
                pass
            return all(_digest(clients[role], config, key, part1 + part2) for role in ROLES)

        try:
            check("conditional_single_put_full_get", lambda: put(keys["single"], single))
            check("committed_canary_put_full_get", lambda: put(keys["COMMITTED.json"], committed))
            for role in ROLES:
                check(role + "_full_get", lambda role=role: _digest(clients[role], config, keys["single"], single))
                check(role + "_scoped_list", lambda role=role: isinstance(clients[role].list_objects_v2(
                    Bucket=config.bucket, Prefix=config.object_prefix + "_iam-check/" + identifier + "/").get("IsTruncated"), bool))
            check("conditional_multipart_full_get_all_roles", multipart)
            check("conditional_overwrite_rejected", lambda: _denied(lambda: uploader.put_object(
                Bucket=config.bucket, Key=keys["single"], Body=b"overwrite", IfNoneMatch="*"), ("PreconditionFailed", "412")))
            check("unconditional_write_denied", lambda: _denied(lambda: uploader.put_object(
                Bucket=config.bucket, Key=keys["single"], Body=b"unconditional")))
            check("uploader_committed_delete_denied", lambda: _denied(lambda: uploader.delete_object(
                Bucket=config.bucket, Key=keys["COMMITTED.json"])))
            check("restore_committed_delete_denied", lambda: _denied(lambda: reader.delete_object(
                Bucket=config.bucket, Key=keys["COMMITTED.json"])))
            for role in ("restore", "retainer"):
                check(role + "_put_denied", lambda role=role: _denied(lambda: clients[role].put_object(
                    Bucket=config.bucket, Key=keys[role + "-put"], Body=single, IfNoneMatch="*")))
            outside_prefix = "_pz-iam-outside/" + identifier + "/"
            for role in ROLES:
                check(role + "_outside_missing_get_no_data", lambda role=role: _denied(lambda: clients[role].get_object(
                    Bucket=config.bucket, Key=outside_prefix + "never-created"), ("AccessDenied", "NoSuchKey", "404")))
                check(role + "_outside_list_denied", lambda role=role: _denied(lambda: clients[role].list_objects_v2(
                    Bucket=config.bucket, Prefix=outside_prefix)))
            check("single_unchanged_after_negative_checks", lambda: _digest(reader, config, keys["single"], single))
            check("commit_unchanged_after_negative_checks", lambda: _digest(reader, config, keys["COMMITTED.json"], committed))
        finally:
            try:
                report["cleanup_complete"] = cleanup_canary(clients, config, journal_path)
            except Exception as error:
                report["cleanup_failure_type"] = type(error).__name__
            report["completed_at"] = remote.utc_now()
            report["passed"] = all(checks.values()) and report["cleanup_complete"]
            remote._atomic_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    for role in ROLES:
        parser.add_argument("--" + role + "-credentials", required=True)
    parser.add_argument("--policy-file", required=True, help="Private JSON export of deployed bucket policy")
    parser.add_argument("--report", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = remote.RemoteConfig(**remote._private_json(Path(args.config)))
        clients = {role: remote.create_client(config, getattr(args, role + "_credentials")) for role in ROLES}
        if args.cleanup_only:
            with remote._lock(config):
                cleanup_canary(clients, config, args.journal)
            print('{"cleanup_complete":true}')
            return 0
        report = run_canary(clients, config, policy=remote._private_json(Path(args.policy_file)),
                             report_path=args.report, journal_path=args.journal)
        print(json.dumps({"passed": report["passed"], "cleanup_complete": report["cleanup_complete"],
                          "operator_checks_required": report["operator_checks_required"]}, sort_keys=True))
        return 0 if report["passed"] else 1
    except remote.BackupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print("IAM canary failed (" + type(error).__name__ + "); inspect private report/journal", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
