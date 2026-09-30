output "namespaces" {
  description = "Environment names created by this Terraform root."
  value       = sort(keys(var.environments))
}

output "budget" {
  description = "Declared allocations only; compare these with actual node allocatable and unmanaged usage before applying. Storage reservations are PVC requests, not enforced physical disk partitions."
  value = {
    available_cpu_m       = var.allocatable.cpu_m
    available_memory_mi   = var.allocatable.memory_mi
    cpu_requests_m        = local.total_cpu_requests_m
    cpu_limits_m          = local.total_cpu_limits_m
    memory_requests_mi    = local.total_memory_requests_mi
    memory_limits_mi      = local.total_memory_limits_mi
    unallocated_cpu_m     = var.allocatable.cpu_m - local.total_cpu_limits_m
    unallocated_memory_mi = var.allocatable.memory_mi - local.total_memory_limits_mi
    pvc_requested_gi      = sum(concat([0], [for env in values(var.environments) : env.quota.storage_gi]))
  }
}
