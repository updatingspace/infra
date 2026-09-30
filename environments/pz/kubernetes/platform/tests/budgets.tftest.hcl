# No cluster is contacted: native Terraform mock providers require >=1.7.
mock_provider "kubernetes" {}

variables {
  kubeconfig_context = "unit-test"
  allocatable = {
    cpu_m     = 3000
    memory_mi = 12000
  }
  environments = {
    test = {
      quota = {
        cpu_request_m        = 2000
        cpu_limit_m          = 2600
        memory_request_mi    = 8512
        memory_limit_mi      = 11136
        ephemeral_request_mi = 128
        ephemeral_limit_mi   = 512
        storage_gi           = 2
        pvc_count            = 2
        pod_count            = 4
      }
      container_defaults = {
        cpu_request_m        = 100
        cpu_limit_m          = 200
        memory_request_mi    = 128
        memory_limit_mi      = 256
        ephemeral_request_mi = 64
        ephemeral_limit_mi   = 128
      }
    }
    # Remaining namespaces make overcommit tests exercise the aggregate, not
    # just an individual quota. The updater leaves only 96Mi of RAM headroom.
    neighbors = {
      quota = {
        cpu_request_m        = 200
        cpu_limit_m          = 400
        memory_request_mi    = 384
        memory_limit_mi      = 768
        ephemeral_request_mi = 384
        ephemeral_limit_mi   = 1536
        storage_gi           = 0.75
        pvc_count            = 4
        pod_count            = 4
      }
      container_defaults = {
        cpu_request_m        = 50
        cpu_limit_m          = 100
        memory_request_mi    = 64
        memory_limit_mi      = 128
        ephemeral_request_mi = 64
        ephemeral_limit_mi   = 128
      }
    }
  }
}

run "valid_budget_and_isolation" {
  command = plan

  assert {
    condition     = output.budget.cpu_limits_m == 3000 && output.budget.memory_limits_mi == 11904
    error_message = "The budget report must match the namespace allocations."
  }
  assert {
    condition = (
      kubernetes_resource_quota_v1.environment["test"].spec[0].hard["limits.cpu"] == "2600m" &&
      kubernetes_resource_quota_v1.environment["test"].spec[0].hard["limits.memory"] == "11136Mi" &&
      kubernetes_resource_quota_v1.environment["test"].spec[0].hard["requests.storage"] == "2Gi"
    )
    error_message = "Numeric inputs must become correct Kubernetes resource quantities."
  }
  assert {
    condition     = length(kubernetes_network_policy_v1.default_deny["test"].spec[0].ingress) == 0 && length(kubernetes_network_policy_v1.default_deny["test"].spec[0].egress) == 0
    error_message = "Default deny must not contain permissive ingress or egress rules."
  }
  assert {
    condition = (
      kubernetes_network_policy_v1.dns["test"].spec[0].egress[0].to[0].namespace_selector[0].match_labels["kubernetes.io/metadata.name"] == "kube-system" &&
      kubernetes_network_policy_v1.dns["test"].spec[0].egress[0].to[0].pod_selector[0].match_labels["k8s-app"] == "kube-dns" &&
      length(kubernetes_network_policy_v1.dns["test"].spec[0].egress[0].ports) == 2
    )
    error_message = "DNS must target only the cluster DNS pods with TCP and UDP rules."
  }
  assert {
    condition     = length(kubernetes_network_policy_v1.external_egress) == 0 && !kubernetes_default_service_account_v1.environment["test"].automount_service_account_token
    error_message = "External egress and automatic default service account tokens must be disabled by default."
  }
}

run "accept_exact_aggregate_budget" {
  command = plan
  variables {
    allocatable = { cpu_m = 3000, memory_mi = 11904 }
  }
}

run "reject_cpu_overcommit" {
  command = plan
  variables {
    allocatable = { cpu_m = 2999, memory_mi = 12000 }
  }
  expect_failures = [terraform_data.budget]
}

run "reject_memory_overcommit" {
  command = plan
  variables {
    allocatable = { cpu_m = 3000, memory_mi = 11903 }
  }
  expect_failures = [terraform_data.budget]
}

run "reject_requests_above_limits" {
  command = plan
  variables {
    environments = {
      test = {
        quota = {
          cpu_request_m        = 501
          cpu_limit_m          = 500
          memory_request_mi    = 512
          memory_limit_mi      = 1024
          ephemeral_request_mi = 128
          ephemeral_limit_mi   = 512
          storage_gi           = 2
          pvc_count            = 2
          pod_count            = 4
        }
        container_defaults = {
          cpu_request_m        = 100
          cpu_limit_m          = 200
          memory_request_mi    = 128
          memory_limit_mi      = 256
          ephemeral_request_mi = 64
          ephemeral_limit_mi   = 128
        }
      }
    }
  }
  expect_failures = [var.environments]
}

run "reject_empty_environment_map" {
  command = plan
  variables {
    environments = {}
  }
  expect_failures = [var.environments]
}

run "accept_fractional_storage_gib" {
  command = plan
  variables {
    environments = {
      test = {
        quota = {
          cpu_request_m        = 250
          cpu_limit_m          = 500
          memory_request_mi    = 512
          memory_limit_mi      = 1024
          ephemeral_request_mi = 128
          ephemeral_limit_mi   = 512
          storage_gi           = 0.25
          pvc_count            = 2
          pod_count            = 4
        }
        container_defaults = {
          cpu_request_m        = 100
          cpu_limit_m          = 200
          memory_request_mi    = 128
          memory_limit_mi      = 256
          ephemeral_request_mi = 64
          ephemeral_limit_mi   = 128
        }
      }
    }
  }
  assert {
    condition     = kubernetes_resource_quota_v1.environment["test"].spec[0].hard["requests.storage"] == "0.25Gi"
    error_message = "A 256Mi backing filesystem must be expressible as a 0.25Gi PVC request quota."
  }
}
