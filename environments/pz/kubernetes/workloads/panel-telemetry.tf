locals {
  panel_telemetry_sources = {
    for name in ["preload.mjs", "provider.mjs", "server-transform.mjs", "client-overlay.mjs"] :
    name => file("${path.module}/../panel-telemetry/${name}")
  }
}

resource "kubernetes_config_map_v1" "panel_telemetry" {
  metadata {
    name      = "panel-telemetry"
    namespace = "zomboid"
  }
  data = local.panel_telemetry_sources
  lifecycle {
    precondition {
      condition     = alltrue([for name in ["preload.mjs", "provider.mjs", "server-transform.mjs", "client-overlay.mjs"] : contains(keys(local.panel_telemetry_sources), name)])
      error_message = "The complete reviewed panel telemetry adapter must be present before planning."
    }
  }
}

resource "kubernetes_service_account_v1" "panel_telemetry" {
  metadata {
    name      = "panel-telemetry"
    namespace = "zomboid"
  }
  automount_service_account_token = false
}

resource "kubernetes_role_v1" "panel_telemetry" {
  metadata {
    name      = "panel-telemetry"
    namespace = "zomboid"
  }
  rule {
    api_groups     = [""]
    resources      = ["pods"]
    resource_names = ["zomboid-0"]
    verbs          = ["get"]
  }
  rule {
    api_groups     = ["metrics.k8s.io"]
    resources      = ["pods"]
    resource_names = ["zomboid-0"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "panel_telemetry" {
  metadata {
    name      = "panel-telemetry"
    namespace = "zomboid"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.panel_telemetry.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.panel_telemetry.metadata[0].name
    namespace = "zomboid"
  }
}

resource "kubernetes_network_policy_v1" "panel_telemetry_api" {
  metadata {
    name      = "panel-telemetry-api"
    namespace = "zomboid"
  }
  spec {
    pod_selector {
      match_labels = local.panel_labels
    }
    policy_types = ["Egress"]
    # Cover service routing before and after DNAT without allowing the node's
    # other ports or other private destinations. Existing DNS policy remains.
    dynamic "egress" {
      for_each = { "10.43.0.1/32" = "443", "10.130.0.30/32" = "6443" }
      content {
        to {
          ip_block {
            cidr = egress.key
          }
        }
        ports {
          protocol = "TCP"
          port     = egress.value
        }
      }
    }
  }
}
