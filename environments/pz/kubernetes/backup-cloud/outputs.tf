output "backup" {
  description = "Non-secret IDs. Disk attachment belongs in the existing cloud root with auto_delete=false."
  value = {
    bucket              = yandex_storage_bucket.backup.bucket
    prefix              = local.prefix
    endpoint            = "https://storage.yandexcloud.net"
    region              = "ru-central1"
    storage_class       = var.storage_class
    service_account_ids = { for name, account in yandex_iam_service_account.actor : name => account.id }
    data_disk_id        = yandex_compute_disk.data.id
    spool_disk_id       = yandex_compute_disk.spool.id
    zone                = var.zone
  }
}

output "bucket_policy" {
  description = "Reviewable least-privilege policy; contains account IDs but no credentials."
  value       = local.policy
}
