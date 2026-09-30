terraform {
  required_version = ">= 1.7, < 2.0"
}

variable "ssh_target" {
  type    = string
  default = "matveevmihail@51.250.40.253"
}

variable "ssh_key" {
  type    = string
  default = "~/.ssh/matveevmihail_yt"
}

variable "release_verified_sources" {
  description = "Remove each retired source only after verified backup plus checksum-identical copy. Required to fit the current disk."
  type        = bool
  default     = false
}

locals {
  filesystems = {
    zomboid       = { size_mib = 26624, directories = ["pz-server", "zomboid", "steam", "panel", "panel-logs"] }
    edge          = { size_mib = 256, directories = ["caddy-data", "caddy-config"] }
    observability = { size_mib = 512, directories = ["otelcol"] }
  }
  specification = {
    expected_host            = "compute-vm-2-6-60-ssd-1785610759198"
    source_root              = "/opt/pz-stack/data"
    image_root               = "/var/lib/pz-volumes"
    mount_root               = "/srv/pz-storage"
    backup_manifest          = "/var/backups/pz-k3s-20260930/verified.json"
    minimum_host_free_mib    = 2048
    filesystems              = local.filesystems
    release_verified_sources = var.release_verified_sources
  }
}

# No destroy action exists. Replacing or removing this resource is deliberately
# blocked; a reviewed, separate maintenance flow is required for future growth.
resource "terraform_data" "storage" {
  input            = local.specification
  triggers_replace = [sha256(jsonencode(local.filesystems))]
  provisioner "local-exec" {
    command = "python3 \"$STORAGE_SCRIPT\""
    environment = {
      STORAGE_SCRIPT = abspath("${path.module}/storage.py")
      STORAGE_SPEC   = jsonencode(local.specification)
      SSH_TARGET     = var.ssh_target
      SSH_KEY        = pathexpand(var.ssh_key)
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

output "bounded_filesystems" {
  description = "Historical layout created by the initial loop migration, not an inventory of current mounts. Dedicated data-disk cutover is recorded separately."
  value = { for name, fs in local.filesystems : name => {
    image    = "${local.specification.image_root}/${name}.ext4"
    mount    = "${local.specification.mount_root}/${name}"
    size_mib = fs.size_mib
  } }
}

output "storage_properties" {
  description = "Historical properties of the initial loop migration. Do not use this output to infer the active data device after a dedicated-disk cutover."
  value       = "Separate bounded ext4 filesystems on one physical SSD; 26.75 GiB total logical capacity. Sparse files do not reserve host disk space. Host exhaustion can affect every filesystem. This provides neither replication nor high availability."
}
