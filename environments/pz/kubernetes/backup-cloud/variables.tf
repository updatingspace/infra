variable "folder_id" {
  description = "Explicit target folder. Use IAM-token provider authentication outside configuration."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9]{20}$", var.folder_id))
    error_message = "folder_id must be an explicit Yandex folder ID."
  }
}

variable "zone" {
  description = "Must match the existing VM zone; disks cannot cross zones."
  type        = string
  nullable    = false
  validation {
    condition     = contains(["ru-central1-a", "ru-central1-b", "ru-central1-d"], var.zone)
    error_message = "Select a supported Russia availability zone explicitly."
  }
}

variable "environment" {
  description = "Backup namespace; objects live under pz/<environment>/."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,29}$", var.environment))
    error_message = "environment must be a short lowercase identifier without slashes/wildcards."
  }
}

variable "provisioner_principal_id" {
  description = "Exact existing deployment user/service-account ID from yc iam whoami; bucket administration only, never a runtime actor."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9]{20}$", var.provisioner_principal_id))
    error_message = "provisioner_principal_id must name one explicit existing IAM subject."
  }
}

variable "bucket_name" {
  description = "Globally unique private backup bucket; existing buckets must first be imported."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be 3-63 lowercase letters, digits and hyphens."
  }
}

variable "bucket_capacity_gib" {
  description = "Storage cost guard; allow at least six worst-case payloads while retention runs. Multipart capacity is not a hard billing cap."
  type        = number
  nullable    = false
  validation {
    condition     = var.bucket_capacity_gib >= 1 && floor(var.bucket_capacity_gib) == var.bucket_capacity_gib
    error_message = "bucket_capacity_gib must be a positive integer chosen from inventory."
  }
}

variable "storage_class" {
  description = "Explicit payload storage class. ICE is excluded because rolling deletion incurs its 12-month minimum charge."
  type        = string
  nullable    = false
  validation {
    condition     = contains(["STANDARD", "COLD"], var.storage_class)
    error_message = "Select STANDARD or COLD; this rolling-backup policy is not suitable for ICE."
  }
}

variable "data_disk_gib" {
  description = "New independent world-data disk size; example 40 GiB within the current SSD quota, explicit decision required."
  type        = number
  nullable    = false
  validation {
    condition     = var.data_disk_gib >= 26 && floor(var.data_disk_gib) == var.data_disk_gib
    error_message = "data_disk_gib must be an integer >=26 and fit the measured source."
  }
}

variable "data_disk_type" {
  description = "Explicit disk performance/cost choice."
  type        = string
  nullable    = false
  validation {
    condition     = contains(["network-ssd", "network-hdd"], var.data_disk_type)
    error_message = "Select network-ssd or network-hdd."
  }
}

variable "spool_disk_gib" {
  description = "Independent spool capacity: staging + worst-case encrypted archive + reserve. Example 64 GiB."
  type        = number
  nullable    = false
  validation {
    condition     = var.spool_disk_gib >= 1 && floor(var.spool_disk_gib) == var.spool_disk_gib
    error_message = "spool_disk_gib must be a positive integer chosen from inventory."
  }
}

variable "spool_disk_type" {
  description = "Explicit spool disk performance/cost choice."
  type        = string
  nullable    = false
  validation {
    condition     = contains(["network-ssd", "network-hdd"], var.spool_disk_type)
    error_message = "Select network-ssd or network-hdd."
  }
}

variable "kms_key_id" {
  description = "Optional existing KMS key for an additional SSE-KMS layer. Its grants and recovery are managed separately; age encryption remains required."
  type        = string
  default     = null
  validation {
    condition     = var.kms_key_id == null ? true : can(regex("^[a-z0-9]{20}$", var.kms_key_id))
    error_message = "kms_key_id must be null or an existing Yandex symmetric key ID."
  }
}
