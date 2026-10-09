output "capacity" {
  value = {
    instance_id   = yandex_compute_instance.server.id
    boot_disk_id  = yandex_compute_disk.boot.id
    cores         = var.vm_cores
    memory_gib    = var.vm_memory_gib
    boot_disk_gib = var.boot_disk_gib
  }
}
