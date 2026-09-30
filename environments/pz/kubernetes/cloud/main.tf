terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "0.228.0"
    }
  }
}

locals {
  folder_id = "b1gidr45ifb2c25bco2d"
  zone      = "ru-central1-d"
}

provider "yandex" {
  folder_id = local.folder_id
  zone      = local.zone
}

resource "yandex_compute_disk" "boot" {
  name       = "disk-ubuntu-24-04-lts-1785610761156"
  folder_id  = local.folder_id
  zone       = local.zone
  type       = "network-ssd"
  size       = var.boot_disk_gib
  block_size = 4096
  image_id   = "fd8020c5t6gei8d1rpi1"

  lifecycle {
    prevent_destroy = true
  }
}

resource "yandex_compute_instance" "server" {
  name                      = "compute-vm-2-6-60-ssd-1785610759198"
  hostname                  = "compute-vm-2-6-60-ssd-1785610759198"
  folder_id                 = local.folder_id
  zone                      = local.zone
  platform_id               = "standard-v3"
  allow_stopping_for_update = var.allow_stopping_for_update
  network_acceleration_type = "standard"

  resources {
    cores         = var.vm_cores
    memory        = var.vm_memory_gib
    core_fraction = 100
  }

  boot_disk {
    disk_id     = yandex_compute_disk.boot.id
    device_name = "fv4m82qqrtj4rq7ppj00"
    mode        = "READ_WRITE"
    # Preserve the existing attachment without changing cloud deletion behavior.
    # Terraform prevents deletion of both VM and disk while their blocks remain.
    auto_delete = true
  }

  network_interface {
    index              = 0
    subnet_id          = "fl85dfa0vicscrh220cv"
    ip_address         = "10.130.0.30"
    ipv4               = true
    ipv6               = false
    nat                = true
    nat_ip_address     = "51.250.40.253"
    security_group_ids = [yandex_vpc_security_group.server.id]
  }

  # Preserve existing metadata endpoint policy explicitly (1=enabled, 2=disabled).
  metadata_options {
    aws_v1_http_endpoint = 1
    aws_v1_http_token    = 2
    aws_v2_http_endpoint = 1
    aws_v2_http_token    = 1
    gce_http_endpoint    = 1
    gce_http_token       = 1
  }

  scheduling_policy {
    preemptible = false
  }

  lifecycle {
    prevent_destroy = true
    # Existing SSH/OS Login metadata is managed outside this root. Import still
    # records it in private state; do not print or commit state/plan JSON.
    ignore_changes = [metadata]
  }
}
