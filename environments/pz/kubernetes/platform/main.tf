locals {
  total_cpu_limits_m       = sum(concat([0], [for env in values(var.environments) : env.quota.cpu_limit_m]))
  total_memory_limits_mi   = sum(concat([0], [for env in values(var.environments) : env.quota.memory_limit_mi]))
  total_cpu_requests_m     = sum(concat([0], [for env in values(var.environments) : env.quota.cpu_request_m]))
  total_memory_requests_mi = sum(concat([0], [for env in values(var.environments) : env.quota.memory_request_mi]))
}

# A failed resource precondition blocks plan/apply; a check block would only warn.
resource "terraform_data" "budget" {
  input = var.allocatable

  lifecycle {
    precondition {
      condition     = local.total_cpu_limits_m <= var.allocatable.cpu_m
      error_message = "Namespace CPU limits exceed the available CPU budget. Raise verified capacity or reduce allocations before applying."
    }
    precondition {
      condition     = local.total_memory_limits_mi <= var.allocatable.memory_mi
      error_message = "Namespace memory limits exceed the available memory budget. Raise verified capacity or reduce allocations before applying."
    }
  }
}

resource "kubernetes_namespace_v1" "environment" {
  for_each = var.environments

  metadata {
    name = each.key
    labels = {
      "app.kubernetes.io/managed-by"               = "terraform"
      "pod-security.kubernetes.io/enforce"         = each.value.pod_security_level
      "pod-security.kubernetes.io/enforce-version" = var.pod_security_version
      "pod-security.kubernetes.io/audit"           = each.value.pod_security_level
      "pod-security.kubernetes.io/audit-version"   = var.pod_security_version
      "pod-security.kubernetes.io/warn"            = each.value.pod_security_level
      "pod-security.kubernetes.io/warn-version"    = var.pod_security_version
    }
  }

  lifecycle {
    prevent_destroy = true
  }
  depends_on = [terraform_data.budget]
}

resource "kubernetes_resource_quota_v1" "environment" {
  for_each = var.environments

  metadata {
    name      = "environment-budget"
    namespace = kubernetes_namespace_v1.environment[each.key].metadata[0].name
  }
  spec {
    hard = {
      "requests.cpu"               = "${each.value.quota.cpu_request_m}m"
      "limits.cpu"                 = "${each.value.quota.cpu_limit_m}m"
      "requests.memory"            = "${each.value.quota.memory_request_mi}Mi"
      "limits.memory"              = "${each.value.quota.memory_limit_mi}Mi"
      "requests.ephemeral-storage" = "${each.value.quota.ephemeral_request_mi}Mi"
      "limits.ephemeral-storage"   = "${each.value.quota.ephemeral_limit_mi}Mi"
      "requests.storage"           = "${each.value.quota.storage_gi}Gi"
      "persistentvolumeclaims"     = tostring(each.value.quota.pvc_count)
      "pods"                       = tostring(each.value.quota.pod_count)
    }
  }
}

resource "kubernetes_limit_range_v1" "container" {
  for_each = var.environments

  metadata {
    name      = "container-defaults"
    namespace = kubernetes_namespace_v1.environment[each.key].metadata[0].name
  }
  spec {
    limit {
      type = "Container"
      default_request = {
        cpu                 = "${each.value.container_defaults.cpu_request_m}m"
        memory              = "${each.value.container_defaults.memory_request_mi}Mi"
        "ephemeral-storage" = "${each.value.container_defaults.ephemeral_request_mi}Mi"
      }
      default = {
        cpu                 = "${each.value.container_defaults.cpu_limit_m}m"
        memory              = "${each.value.container_defaults.memory_limit_mi}Mi"
        "ephemeral-storage" = "${each.value.container_defaults.ephemeral_limit_mi}Mi"
      }
      max = {
        cpu                 = "${each.value.quota.cpu_limit_m}m"
        memory              = "${each.value.quota.memory_limit_mi}Mi"
        "ephemeral-storage" = each.value.quota.ephemeral_limit_mi % 1024 == 0 ? "${each.value.quota.ephemeral_limit_mi / 1024}Gi" : "${each.value.quota.ephemeral_limit_mi}Mi"
      }
    }
  }
}

# This resource adopts only the automatically created default service account.
# Workloads needing the API should get a dedicated account and explicit RBAC.
resource "kubernetes_default_service_account_v1" "environment" {
  for_each = var.environments

  metadata {
    namespace = kubernetes_namespace_v1.environment[each.key].metadata[0].name
  }
  automount_service_account_token = false
}
