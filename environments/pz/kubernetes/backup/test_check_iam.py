"""Offline endpoint/policy canaries; no cloud calls or credentials."""

import copy
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

import remote

SPEC = importlib.util.spec_from_file_location("check_iam", Path(__file__).with_name("check-iam.py"))
iam = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(iam)


def policy_fixture(config):
    bucket = "arn:aws:s3:::" + config.bucket
    own = bucket + "/" + config.object_prefix + "*"
    statements = []
    actions = {"uploader": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
               "retainer": ["s3:GetObject", "s3:DeleteObject"], "restore": ["s3:GetObject"]}
    for role in iam.ROLES:
        principal = {"CanonicalUser": role + "-principal"}
        statements += [
            {"Sid": role + "Objects", "Effect": "Allow", "Principal": principal,
             "Action": actions[role], "Resource": [own]},
            {"Sid": role + "List", "Effect": "Allow", "Principal": principal,
             "Action": ["s3:ListBucket"], "Resource": [bucket],
             "Condition": {"StringLike": {"s3:prefix": [config.object_prefix + "*"]}}},
            {"Sid": role + "VersioningCheck", "Effect": "Allow", "Principal": principal,
             "Action": ["s3:GetBucketVersioning"], "Resource": [bucket]},
        ]
    statements += [
        {"Sid": "UploaderMultipartInventory", "Effect": "Allow", "Principal": {"CanonicalUser": "uploader-principal"},
         "Action": ["s3:ListBucketMultipartUploads"], "Resource": [bucket]},
        {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": ["s3:*"],
         "Resource": [bucket, bucket + "/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyUnconditionalUpload", "Effect": "Deny", "Principal": {"CanonicalUser": "uploader-principal"},
         "Action": ["s3:PutObject"], "Resource": [own], "Condition": {"Null": {"s3:if-none-match": "true"}}},
        {"Sid": "DenyUploaderDeletion", "Effect": "Deny", "Principal": {"CanonicalUser": "uploader-principal"},
         "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"], "Resource": [bucket + "/*"]},
    ]
    return {"Version": "2012-10-17", "Statement": statements}


class Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class Store:
    def __init__(self):
        self.objects, self.uploads, self.calls = {}, {}, []
        self.allow_restore_put = False
        self.allow_outside_get = False
        self.fail_multipart = False
        self.fail_abort = False
        self.fail_delete = False
        self.corrupt_reads = False
        self.versioned = False


class Client:
    def __init__(self, role, store, config, policy):
        self.role, self.store, self.config, self.policy = role, store, config, policy

    def check(self, action, key=None, prefix=None, conditional=None, multipart=False):
        self.store.calls.append((self.role, action, key))
        if self.role == "restore" and action == "s3:PutObject" and self.store.allow_restore_put:
            return
        if action == "s3:GetObject" and key.startswith("_pz-iam-outside/") and self.store.allow_outside_get:
            return
        context = {"aws:SecureTransport": "true", "s3:ObjectCreationOperation": "true"}
        if conditional is not None or multipart:
            context["s3:if-none-match"] = conditional or "*"
        if prefix is not None:
            context["s3:prefix"] = prefix
        resource = "arn:aws:s3:::" + self.config.bucket + ("/" + key if key is not None else "")
        if not iam._allowed(self.policy, self.role + "-principal", action, resource, context):
            raise Error("AccessDenied")

    def put_object(self, **kwargs):
        self.check("s3:PutObject", kwargs["Key"], conditional=kwargs.get("IfNoneMatch"))
        if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in self.store.objects:
            raise Error("PreconditionFailed")
        self.store.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def get_object(self, **kwargs):
        self.check("s3:GetObject", kwargs["Key"])
        if kwargs["Key"] not in self.store.objects:
            raise Error("NoSuchKey")
        content = self.store.objects[kwargs["Key"]]
        if self.store.corrupt_reads:
            content = b"corrupt" + content
        return {"Body": io.BytesIO(content), "ContentLength": len(content)}

    def get_bucket_versioning(self, **kwargs):
        self.check("s3:GetBucketVersioning")
        return {"Status": "Enabled"} if self.store.versioned else {}

    def list_objects_v2(self, **kwargs):
        self.check("s3:ListBucket", prefix=kwargs["Prefix"])
        return {"IsTruncated": False, "Contents": [{"Key": key, "Size": len(body)} for key, body in self.store.objects.items()
                                                     if key.startswith(kwargs["Prefix"])]}

    def delete_object(self, **kwargs):
        self.check("s3:DeleteObject", kwargs["Key"])
        if self.store.fail_delete:
            raise Error("AccessDenied")
        self.store.objects.pop(kwargs["Key"], None)
        return {}

    def create_multipart_upload(self, **kwargs):
        self.check("s3:PutObject", kwargs["Key"], multipart=True)
        if self.store.fail_multipart:
            raise Error("AccessDenied")
        upload_id = str(len(self.store.uploads) + 1)
        self.store.uploads[upload_id] = {"Key": kwargs["Key"], "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs):
        self.check("s3:PutObject", kwargs["Key"], multipart=True)
        self.store.uploads[kwargs["UploadId"]]["parts"][kwargs["PartNumber"]] = kwargs["Body"]
        return {"ETag": str(kwargs["PartNumber"])}

    def complete_multipart_upload(self, **kwargs):
        self.check("s3:PutObject", kwargs["Key"], conditional=kwargs.get("IfNoneMatch"))
        upload = self.store.uploads.pop(kwargs["UploadId"])
        self.store.objects[kwargs["Key"]] = b"".join(upload["parts"][index] for index in sorted(upload["parts"]))
        return {}

    def list_multipart_uploads(self, **kwargs):
        self.check("s3:ListBucketMultipartUploads")
        return {"IsTruncated": False, "Uploads": [{"Key": upload["Key"], "UploadId": uid}
                                                   for uid, upload in self.store.uploads.items()
                                                   if upload["Key"].startswith(kwargs["Prefix"])]}

    def abort_multipart_upload(self, **kwargs):
        self.check("s3:AbortMultipartUpload", kwargs["Key"])
        if self.store.fail_abort:
            raise Error("AccessDenied")
        self.store.uploads.pop(kwargs["UploadId"], None)
        return {}


class IAMTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = remote.RemoteConfig(bucket="pz-private", prefix="pz/production", lock_path=str(self.root / "lock"))
        self.policy = policy_fixture(self.config)
        self.store = Store()
        self.clients = {role: Client(role, self.store, self.config, self.policy) for role in iam.ROLES}
        self.report, self.journal = self.root / "report.json", self.root / "journal.json"

    def run_probe(self):
        return iam.run_canary(self.clients, self.config, policy=self.policy,
                              report_path=self.report, journal_path=self.journal)

    def test_valid_fixture_and_live_contracts_clean_every_canary(self):
        report = self.run_probe()
        self.assertTrue(report["passed"], report)
        self.assertTrue(report["cleanup_complete"])
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.store.uploads, {})
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(remote._private_json(self.journal)["phase"], "complete")
        self.assertFalse(any(action in ("s3:DeleteBucket", "s3:PutBucketPolicy") for _, action, _ in self.store.calls))
        self.assertFalse(any(key and key.startswith("_pz-iam-outside/") and action in ("s3:PutObject", "s3:DeleteObject")
                             for _, action, key in self.store.calls))

    def test_dangerous_admin_fixture_fails_without_live_calls(self):
        self.policy["Statement"].append({"Effect": "Allow", "Principal": {"CanonicalUser": "restore-principal"},
                                          "Action": ["s3:DeleteBucket", "s3:PutBucketPolicy"],
                                          "Resource": ["arn:aws:s3:::" + self.config.bucket]})
        report = self.run_probe()
        self.assertFalse(report["passed"])
        self.assertFalse(report["policy_fixture"]["restore_delete_bucket_denied_fixture"])
        self.assertEqual(self.store.calls, [])

    def test_restore_unexpected_put_is_detected_and_canary_removed(self):
        self.store.allow_restore_put = True
        report = self.run_probe()
        self.assertFalse(report["passed"])
        self.assertFalse(report["endpoint_checks"]["restore_put_denied"])
        self.assertTrue(report["cleanup_complete"])
        self.assertEqual(self.store.objects, {})

    def test_outside_missing_get_404_is_no_data_not_authorization_proof(self):
        self.store.allow_outside_get = True
        report = self.run_probe()
        self.assertTrue(report["passed"])
        self.assertTrue(report["endpoint_checks"]["uploader_outside_missing_get_no_data"])
        self.assertNotIn("uploader_outside_missing_get_denied", report["endpoint_checks"])
        self.assertIn("existing outside-prefix GetObject", report["not_executed_live"])
        self.assertTrue(report["cleanup_complete"])

    def test_multipart_policy_failure_is_not_hidden(self):
        self.store.fail_multipart = True
        report = self.run_probe()
        self.assertFalse(report["passed"])
        self.assertFalse(report["endpoint_checks"]["conditional_multipart_full_get_all_roles"])
        self.assertTrue(report["cleanup_complete"])

    def test_failed_cleanup_preserves_journal_for_exact_resume(self):
        self.store.fail_delete = True
        report = self.run_probe()
        self.assertFalse(report["passed"])
        self.assertFalse(report["cleanup_complete"])
        self.assertEqual(remote._private_json(self.journal)["phase"], "cleaning")
        self.store.fail_delete = False
        iam.cleanup_canary(self.clients, self.config, self.journal)
        self.assertEqual(self.store.objects, {})
        self.assertEqual(remote._private_json(self.journal)["phase"], "complete")

    def test_versioned_bucket_is_refused_before_any_object_write(self):
        self.store.versioned = True
        report = self.run_probe()
        self.assertFalse(report["passed"])
        self.assertEqual(self.store.objects, {})
        self.assertFalse(any(action == "s3:PutObject" for _, action, _ in self.store.calls))

    def test_corrupt_get_never_passes_byte_integrity(self):
        self.store.corrupt_reads = True
        report = self.run_probe()
        self.assertFalse(report["passed"])
        self.assertFalse(report["endpoint_checks"]["conditional_single_put_full_get"])
        self.assertTrue(report["cleanup_complete"])

    def test_cleanup_journal_cannot_delete_production_snapshot(self):
        self.run_probe()
        journal = remote._private_json(self.journal)
        journal["objects"][0] = "pz/production/a-real-snapshot/payload.enc"
        remote._atomic_json(self.journal, journal)
        with self.assertRaises(remote.BackupError):
            iam.cleanup_canary(self.clients, self.config, self.journal)

    def test_unknown_policy_operator_is_not_treated_as_pass(self):
        self.policy["Statement"][0]["Condition"] = {"MagicIfExists": {"invented": "yes"}}
        with self.assertRaises(remote.BackupError):
            iam.check_policy_fixture(self.policy, self.config)

    def test_shared_service_account_does_not_pass_role_separation(self):
        changed = copy.deepcopy(self.policy)
        for statement in changed["Statement"]:
            if statement.get("Sid") == "restoreObjects":
                statement["Principal"] = {"CanonicalUser": "uploader-principal"}
        with self.assertRaises(remote.BackupError):
            iam.check_policy_fixture(changed, self.config)


if __name__ == "__main__":
    unittest.main()
