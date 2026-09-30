variable "kubeconfig_path" {
  description = "Kubeconfig credentials stay outside Terraform and the repository."
  type        = string
  default     = "~/.kube/config"
  nullable    = false
}

variable "kubeconfig_context" {
  description = "Explicit context of the intended existing k3s cluster."
  type        = string
  nullable    = false
  validation {
    condition     = length(trimspace(var.kubeconfig_context)) > 0
    error_message = "Set the context of the intended cluster."
  }
}

variable "node_name" {
  description = "The existing node holding /opt/pz-stack/data. This is a single-node collector."
  type        = string
  default     = "compute-vm-2-6-60-ssd-1785610759198"
  nullable    = false
}

variable "node_private_ip" {
  description = "Private node IP permitted by NetworkPolicy for the kubelet HTTPS endpoint."
  type        = string
  default     = "10.130.0.30"
  nullable    = false
  validation {
    condition     = can(cidrhost("${var.node_private_ip}/32", 0))
    error_message = "Set a valid private IPv4 node address."
  }
}

variable "monium_secret_name" {
  description = "Existing Secret in observability containing MONIUM_API_KEY and MONIUM_LOGS_API_KEY. Terraform never reads or manages its values."
  type        = string
  default     = "monium-env"
  nullable    = false
}

variable "collector_image" {
  description = "Pinned collector image. Override with the exact unique image tag imported into containerd for an offline migration."
  type        = string
  default     = "otel/opentelemetry-collector-contrib:0.161.0@sha256:fd328de2552466ad78385e1b1289c3f2402b1c45f265b252aab1955b42845ac1"
  nullable    = false
  validation {
    condition     = length(trimspace(var.collector_image)) > 0
    error_message = "Set a nonempty pinned digest or exact imported migration image tag."
  }
}

variable "stack_path" {
  description = "Existing host data path. Move data separately; these mounts never initialize a replacement world."
  type        = string
  default     = "/opt/pz-stack"
  nullable    = false
  validation {
    condition     = startswith(var.stack_path, "/") && var.stack_path != "/"
    error_message = "Use an explicit absolute stack directory."
  }
}
