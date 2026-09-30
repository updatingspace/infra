variable "kubeconfig_path" {
  description = "Local kubeconfig for the existing cluster. Keep credentials outside this repository."
  type        = string
  default     = "~/.kube/config"
  nullable    = false
}

variable "kubeconfig_context" {
  description = "Explicit context to prevent accidentally targeting a different cluster."
  type        = string
  nullable    = false

  validation {
    condition     = length(trimspace(var.kubeconfig_context)) > 0
    error_message = "Set kubeconfig_context to the intended cluster context."
  }
}

variable "pod_security_version" {
  description = "Pin admission policy semantics to a Kubernetes minor version supported by the cluster. Baseline disallows hostNetwork, hostPID, hostIPC and privileged containers."
  type        = string
  default     = "v1.36"
  nullable    = false

  validation {
    condition     = can(regex("^v1\\.[0-9]+$", var.pod_security_version))
    error_message = "Use an explicit Kubernetes minor, for example v1.36; do not use latest."
  }
}

variable "allocatable" {
  description = "Verified budget AVAILABLE TO THESE namespaces after OS, Kubernetes, system pods and unmanaged workloads have been deducted. This is an explicit planning input, not discovered node capacity."
  type = object({
    cpu_m     = number
    memory_mi = number
  })
  nullable = false

  validation {
    condition     = alltrue([for amount in values(var.allocatable) : amount > 0 && floor(amount) == amount])
    error_message = "Allocatable CPU (millicores) and memory (MiB) must be positive integers."
  }
}

variable "environments" {
  description = "Namespace allocations. CPU is in millicores; memory and ephemeral storage in MiB; PVC requests in GiB. Storage quota limits declared PVC requests, not physical disk usage or provisioner capacity. Container max is the namespace limit. Egress CIDRs opt into all ports/protocols to those addresses; keep empty until reviewed."
  type = map(object({
    quota = object({
      cpu_request_m        = number
      cpu_limit_m          = number
      memory_request_mi    = number
      memory_limit_mi      = number
      ephemeral_request_mi = number
      ephemeral_limit_mi   = number
      storage_gi           = number
      pvc_count            = number
      pod_count            = number
    })
    container_defaults = object({
      cpu_request_m        = number
      cpu_limit_m          = number
      memory_request_mi    = number
      memory_limit_mi      = number
      ephemeral_request_mi = number
      ephemeral_limit_mi   = number
    })
    egress_cidrs       = optional(set(string), [])
    pod_security_level = optional(string, "baseline")
  }))
  nullable = false

  validation {
    condition     = length(var.environments) > 0
    error_message = "Define at least one environment; an empty map must not silently remove every namespace."
  }

  validation {
    condition = alltrue([for name in keys(var.environments) :
      length(name) <= 63 && can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name)) &&
      name != "default" && !startswith(name, "kube-")
    ])
    error_message = "Environment names must be Kubernetes DNS labels, at most 63 characters, and cannot be default or start with kube-."
  }

  validation {
    condition = alltrue(flatten([for env in values(var.environments) : concat(
      [for name, amount in env.quota : amount > 0 && (name == "storage_gi" || floor(amount) == amount)],
      [for amount in values(env.container_defaults) : amount > 0 && floor(amount) == amount]
    )]))
    error_message = "All quotas, counts and container defaults must be positive integers, except storage_gi which may be a positive fraction of GiB."
  }

  validation {
    condition = alltrue([for env in values(var.environments) :
      env.quota.cpu_request_m <= env.quota.cpu_limit_m &&
      env.quota.memory_request_mi <= env.quota.memory_limit_mi &&
      env.quota.ephemeral_request_mi <= env.quota.ephemeral_limit_mi
    ])
    error_message = "Each namespace request quota must be no greater than its matching limit quota."
  }

  validation {
    condition = alltrue([for env in values(var.environments) :
      env.container_defaults.cpu_request_m <= env.container_defaults.cpu_limit_m &&
      env.container_defaults.memory_request_mi <= env.container_defaults.memory_limit_mi &&
      env.container_defaults.ephemeral_request_mi <= env.container_defaults.ephemeral_limit_mi &&
      env.container_defaults.cpu_request_m <= env.quota.cpu_request_m &&
      env.container_defaults.memory_request_mi <= env.quota.memory_request_mi &&
      env.container_defaults.ephemeral_request_mi <= env.quota.ephemeral_request_mi &&
      env.container_defaults.cpu_limit_m <= env.quota.cpu_limit_m &&
      env.container_defaults.memory_limit_mi <= env.quota.memory_limit_mi &&
      env.container_defaults.ephemeral_limit_mi <= env.quota.ephemeral_limit_mi
    ])
    error_message = "Container default requests must fit their default limits and namespace requests; default limits must fit namespace maxima."
  }

  validation {
    condition     = alltrue([for env in values(var.environments) : contains(["baseline", "restricted", "privileged"], env.pod_security_level)])
    error_message = "pod_security_level must be baseline, restricted or an explicitly reviewed privileged exception."
  }

  validation {
    condition     = alltrue(flatten([for env in values(var.environments) : [for cidr in env.egress_cidrs : can(cidrhost(cidr, 0))]]))
    error_message = "Every egress_cidrs entry must be a valid IPv4 or IPv6 CIDR."
  }
}
