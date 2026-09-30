variable "vm_cores" {
  description = "Desired vCPU count; current VM has 4. Review plan and downtime before changing."
  type        = number
  default     = 4
  nullable    = false

  validation {
    condition     = var.vm_cores > 0 && floor(var.vm_cores) == var.vm_cores
    error_message = "vm_cores must be a positive integer."
  }
}

variable "vm_memory_gib" {
  description = "Desired VM RAM in GiB; current VM has 16. Application quotas are configured separately."
  type        = number
  default     = 16
  nullable    = false

  validation {
    condition     = var.vm_memory_gib > 0
    error_message = "vm_memory_gib must be positive."
  }
}

variable "boot_disk_gib" {
  description = "Desired boot disk capacity in GiB; existing disk is 60 GiB and cannot shrink. Grow the filesystem separately after any approved disk expansion."
  type        = number
  default     = 60
  nullable    = false

  validation {
    condition     = var.boot_disk_gib >= 60 && floor(var.boot_disk_gib) == var.boot_disk_gib
    error_message = "boot_disk_gib must be an integer >=60; disk shrinking is unsupported."
  }
}

variable "allow_stopping_for_update" {
  description = "Allow provider-initiated stop for capacity changes only during an approved maintenance window."
  type        = bool
  default     = false
  nullable    = false
}
