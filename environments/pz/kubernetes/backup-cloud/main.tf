locals {
  prefix     = "pz/${var.environment}/"
  bucket_arn = "arn:aws:s3:::${var.bucket_name}"
  object_arn = "${local.bucket_arn}/${local.prefix}*"
  actors     = toset(["uploader", "retainer", "restore"])
  # These accounts have no folder role, VM identity or Kubernetes permission.
  # Bucket IAM provides baseline access; policy restricts actions and prefix.
  actor_roles = {
    uploader = "storage.uploader"
    retainer = "storage.editor"
    restore  = "storage.viewer"
  }
  actor_actions = {
    uploader = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    retainer = ["s3:GetObject", "s3:DeleteObject"]
    restore  = ["s3:GetObject"]
  }
  policy = {
    Version = "2012-10-17"
    Statement = concat(
      [for actor in sort(tolist(local.actors)) : {
        Sid       = "${actor}Objects"
        Effect    = "Allow"
        Principal = { CanonicalUser = yandex_iam_service_account.actor[actor].id }
        Action    = local.actor_actions[actor]
        Resource  = [local.object_arn]
      }],
      [for actor in sort(tolist(local.actors)) : {
        Sid       = "${actor}List"
        Effect    = "Allow"
        Principal = { CanonicalUser = yandex_iam_service_account.actor[actor].id }
        Action    = ["s3:ListBucket"]
        Resource  = [local.bucket_arn]
        Condition = { StringLike = { "s3:prefix" = ["${local.prefix}*"] } }
      }],
      [for actor in sort(tolist(local.actors)) : {
        Sid       = "${actor}VersioningCheck"
        Effect    = "Allow"
        Principal = { CanonicalUser = yandex_iam_service_account.actor[actor].id }
        Action    = ["s3:GetBucketVersioning"]
        Resource  = [local.bucket_arn]
      }],
      [{
        # YC omits the s3:prefix context for multipart inventory. Only upload
        # metadata is listed across this dedicated bucket; object permissions
        # and ordinary listing remain restricted to the environment prefix.
        Sid       = "UploaderMultipartInventory"
        Effect    = "Allow"
        Principal = { CanonicalUser = yandex_iam_service_account.actor["uploader"].id }
        Action    = ["s3:ListBucketMultipartUploads"]
        Resource  = [local.bucket_arn]
        }, {
        Sid       = "ProvisionerBucketAdministration"
        Effect    = "Allow"
        Principal = { CanonicalUser = var.provisioner_principal_id }
        Action = [
          "s3:GetBucketAcl", "s3:GetBucketCORS", "s3:GetBucketLocation",
          "s3:GetBucketVersioning", "s3:GetBucketWebsite",
          "s3:GetBucketLogging", "s3:GetBucketTagging",
          "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration",
          "s3:ListBucket", "s3:PutLifecycleConfiguration"
        ]
        Resource = [local.bucket_arn]
        }, {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:*"]
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
        }, {
        # Require If-None-Match on PUT and CompleteMultipartUpload. Unique IDs
        # alone do not prevent overwriting an already committed object.
        Sid       = "DenyUnconditionalUpload"
        Effect    = "Deny"
        Principal = { CanonicalUser = yandex_iam_service_account.actor["uploader"].id }
        Action    = ["s3:PutObject"]
        Resource  = [local.object_arn]
        Condition = { Null = { "s3:if-none-match" = "true" } }
        }, {
        Sid       = "DenyUploaderDeletion"
        Effect    = "Deny"
        Principal = { CanonicalUser = yandex_iam_service_account.actor["uploader"].id }
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = ["${local.bucket_arn}/*"]
      }]
    )
  }
}

resource "yandex_iam_service_account" "actor" {
  for_each    = local.actors
  folder_id   = var.folder_id
  name        = "pz-${var.environment}-backup-${each.key}"
  description = "PZ ${var.environment} backup ${each.key}; bucket-prefix only, keys issued out of band"
  lifecycle {
    prevent_destroy = true
  }
}

resource "yandex_storage_bucket_iam_binding" "actor" {
  for_each = local.actor_roles
  bucket   = yandex_storage_bucket.backup.bucket
  role     = each.value
  members  = ["serviceAccount:${yandex_iam_service_account.actor[each.key].id}"]
}

resource "yandex_storage_bucket" "backup" {
  bucket                = var.bucket_name
  folder_id             = var.folder_id
  acl                   = "private"
  default_storage_class = var.storage_class
  max_size              = var.bucket_capacity_gib * 1024 * 1024 * 1024
  force_destroy         = false
  policy                = jsonencode(local.policy)

  anonymous_access_flags {
    read        = false
    list        = false
    config_read = false
  }

  # New buckets default to unversioned. Do not issue PutBucketVersioning(false):
  # with a user IAM token this API can be forbidden even for the bucket creator.
  # The runtime independently rejects enabled/suspended versioning. Do not import
  # a previously version-enabled bucket without adapting retention.

  lifecycle_rule {
    id      = "abort-incomplete-backup-multipart"
    enabled = true
    filter {
      prefix = local.prefix
    }
    abort_incomplete_multipart_upload_days = 7
    # Never age-expire completed objects. Only the verified retainer deletes them.
  }

  dynamic "server_side_encryption_configuration" {
    for_each = var.kms_key_id == null ? [] : [var.kms_key_id]
    content {
      rule {
        apply_server_side_encryption_by_default {
          kms_master_key_id = server_side_encryption_configuration.value
          sse_algorithm     = "aws:kms"
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = alltrue([for actor in yandex_iam_service_account.actor : actor.id != var.provisioner_principal_id])
      error_message = "The bucket provisioner must be distinct from all backup runtime principals."
    }
  }
}

# Cloud game disks retired after verified local migration on 2026-10-09.
# Deletion is an explicit operator action; this root retains only S3 resources.
removed {
  from = yandex_compute_disk.data
  lifecycle {
    destroy = false
  }
}

removed {
  from = yandex_compute_disk.spool
  lifecycle {
    destroy = false
  }
}
