output "backup" {
  description = "Non-secret IDs for retained S3 backup resources after local migration."
  value = {
    bucket              = yandex_storage_bucket.backup.bucket
    prefix              = local.prefix
    endpoint            = "https://storage.yandexcloud.net"
    region              = "ru-central1"
    storage_class       = var.storage_class
    service_account_ids = { for name, account in yandex_iam_service_account.actor : name => account.id }
    zone                = var.zone
  }
}

output "bucket_policy" {
  description = "Reviewable least-privilege policy; contains account IDs but no credentials."
  value       = local.policy
}
