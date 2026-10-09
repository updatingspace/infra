mock_provider "yandex" {}

override_resource {
  target = yandex_iam_service_account.actor["uploader"]
  values = { id = "uploader-id" }
}
override_resource {
  target = yandex_iam_service_account.actor["retainer"]
  values = { id = "retainer-id" }
}
override_resource {
  target = yandex_iam_service_account.actor["restore"]
  values = { id = "restore-id" }
}

variables {
  folder_id                = "b1gidr45ifb2c25bco2d"
  provisioner_principal_id = "aje12345678901234567"
  zone                     = "ru-central1-d"
  environment              = "fixture"
  bucket_name              = "pz-backup-fixture-not-live"
  bucket_capacity_gib      = 150
  storage_class            = "COLD"
}

run "private_immutable_prefix_and_distinct_roles" {
  command = apply

  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement :
      s.Effect != "Allow" || try(s.Principal.CanonicalUser != "*", false)
    ])
    error_message = "Allow rules must name a private principal."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement :
      s.Effect != "Allow" || !endswith(s.Sid, "Objects") || s.Resource == ["arn:aws:s3:::pz-backup-fixture-not-live/pz/fixture/*"]
    ])
    error_message = "Object permissions must stay inside the environment prefix."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement :
      !endswith(s.Sid, "List") || try(s.Condition.StringLike["s3:prefix"] == ["pz/fixture/*"], false)
    ])
    error_message = "Listing requires the environment prefix."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement : s.Sid != "UploaderMultipartInventory" || (
        s.Action == ["s3:ListBucketMultipartUploads"] &&
        s.Resource == ["arn:aws:s3:::pz-backup-fixture-not-live"] &&
        s.Principal.CanonicalUser == yandex_iam_service_account.actor["uploader"].id && !can(s.Condition)
      )
    ])
    error_message = "Multipart metadata listing may allow only the uploader and exact dedicated bucket."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement :
      s.Sid != "uploaderObjects" || (
        contains(s.Action, "s3:GetObject") && contains(s.Action, "s3:PutObject") &&
        contains(s.Action, "s3:AbortMultipartUpload") && !contains(s.Action, "s3:DeleteObject")
      )
    ])
    error_message = "Uploader must support upload/readback/multipart abort and cannot delete snapshots."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement :
      s.Sid != "DenyUnconditionalUpload" || try(s.Condition.Null["s3:if-none-match"] == "true", false)
    ])
    error_message = "Uploader must be required to use conditional writes."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement :
      s.Sid != "restoreObjects" || s.Action == ["s3:GetObject"]
    ])
    error_message = "Restore principal must have read-only object access."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement :
      s.Effect != "Allow" || alltrue([for a in s.Action : !contains(["s3:*", "s3:DeleteBucket", "s3:PutBucketPolicy", "s3:PutBucketAcl"], a)])
    ])
    error_message = "Backup actors cannot administer buckets."
  }
  assert {
    condition = (
      !yandex_storage_bucket.backup.force_destroy &&
      length(yandex_storage_bucket.backup.versioning) == 0 &&
      !one(yandex_storage_bucket.backup.anonymous_access_flags).read &&
      !one(yandex_storage_bucket.backup.anonymous_access_flags).list &&
      !one(yandex_storage_bucket.backup.anonymous_access_flags).config_read &&
      length(yandex_storage_bucket.backup.lifecycle_rule[0].expiration) == 0 &&
      yandex_storage_bucket.backup.lifecycle_rule[0].abort_incomplete_multipart_upload_days == 7
    )
    error_message = "Bucket must be private, unversioned, protected, and age-clean only multipart uploads."
  }
  assert {
    condition = alltrue([
      for s in output.bucket_policy.Statement : s.Sid != "ProvisionerBucketAdministration" ||
      (s.Resource == ["arn:aws:s3:::pz-backup-fixture-not-live"] && s.Principal.CanonicalUser == var.provisioner_principal_id &&
      alltrue([for action in s.Action : startswith(action, "s3:Get") || contains(["s3:ListBucket", "s3:PutLifecycleConfiguration"], action)]))
    ])
    error_message = "Provisioner must be explicit and can administer only the named bucket."
  }
  assert {
    condition = (
      yandex_storage_bucket_iam_binding.actor["uploader"].role == "storage.uploader" &&
      yandex_storage_bucket_iam_binding.actor["retainer"].role == "storage.editor" &&
      yandex_storage_bucket_iam_binding.actor["restore"].role == "storage.viewer" &&
      alltrue([for binding in yandex_storage_bucket_iam_binding.actor : binding.bucket == "pz-backup-fixture-not-live"])
    )
    error_message = "Baseline IAM roles must be scoped to the one backup bucket."
  }
}

run "reject_wildcard_environment" {
  command = plan
  variables { environment = "production/*" }
  expect_failures = [var.environment]
}

run "reject_ice_storage" {
  command = plan
  variables { storage_class = "ICE" }
  expect_failures = [var.storage_class]
}
