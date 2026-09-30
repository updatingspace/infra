variable "kubeconfig_path" {
  type    = string
  default = "~/.kube/config"
}

variable "kubeconfig_context" {
  type = string
  validation {
    condition     = length(trimspace(var.kubeconfig_context)) > 0
    error_message = "An explicit Kubernetes context is required."
  }
}

variable "node_name" {
  description = "Exact kubernetes.io/hostname label of the node holding retained data."
  type        = string
  default     = "compute-vm-2-6-60-ssd-1785610759198"
}

variable "storage_root" {
  description = "Existing data root; all seven directories must already exist and retain their original owners."
  type        = string
  default     = "/opt/pz-stack/data"
  validation {
    condition     = startswith(var.storage_root, "/") && var.storage_root != "/" && !endswith(var.storage_root, "/")
    error_message = "Provide an absolute existing data root without a trailing slash."
  }
}

variable "volume_capacity_gi" {
  description = "Declared local PV capacities; filesystem quotas must be configured separately to enforce disk limits."
  type        = map(number)
  default = {
    pz-server    = 11
    zomboid      = 12
    steam        = 1
    panel        = 1
    panel-logs   = 1
    caddy-data   = 0.125
    caddy-config = 0.125
  }
  validation {
    condition = (
      toset(keys(var.volume_capacity_gi)) == toset(["pz-server", "zomboid", "steam", "panel", "panel-logs", "caddy-data", "caddy-config"]) &&
      alltrue([for size in values(var.volume_capacity_gi) : size > 0 && floor(size * 1024) == size * 1024]) &&
      sum([for name, size in var.volume_capacity_gi : size if !startswith(name, "caddy-")]) <= 26 &&
      sum([for name, size in var.volume_capacity_gi : size if startswith(name, "caddy-")]) <= 0.25
    )
    error_message = "Supply all seven directories with positive capacities in whole MiB, within the bounded filesystem budgets: zomboid 26 GiB, edge 0.25 GiB."
  }
}

variable "game_image" {
  description = "Exact pre-imported game image reference, preferably an immutable digest or unique migration tag."
  type        = string
}

variable "panel_image" {
  description = "Exact pre-imported panel image reference."
  type        = string
}

variable "panel_updater_suspended" {
  description = "Keep scheduled panel updates suspended until the first reviewed manual update and independence check succeed."
  type        = bool
  default     = true
}

variable "panel_telemetry_enabled" {
  description = "Show the named game container's metrics instead of host-wide dashboard statistics. Disable only for a reviewed adapter rollback."
  type        = bool
  default     = true
}

variable "panel_telemetry_server_id" {
  description = "Non-secret panel profile ID associated with zomboid-0; prevents attributing its usage to another selected server."
  type        = string
  default     = "806c8584-0e2b-4c53-a255-3c244cde82ba"
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.panel_telemetry_server_id))
    error_message = "Use the exact UUID of the existing panel game profile."
  }
}

variable "caddy_image" {
  description = "Exact pre-imported Caddy image reference."
  type        = string
}
