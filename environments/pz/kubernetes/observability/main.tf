locals {
  namespace = "observability"
  name      = "otel-collector"
  labels = {
    "app.kubernetes.io/name"       = local.name
    "app.kubernetes.io/managed-by" = "terraform"
  }
  collector_config = file(var.collector_config_path == null ? "${path.module}/otel-collector.yaml" : var.collector_config_path)
}

# Platform owns the namespace, its budget, PSS exception and default-deny/DNS
# policies. This root only reads namespace metadata, never Secret contents.
data "kubernetes_namespace_v1" "observability" {
  metadata {
    name = local.namespace
  }
}

resource "kubernetes_service_account_v1" "collector" {
  metadata {
    name      = local.name
    namespace = data.kubernetes_namespace_v1.observability.metadata[0].name
    labels    = local.labels
  }
  automount_service_account_token = true
}

# The summary endpoint needs nodes/stats only. No cluster-admin, secrets,
# nodes/proxy or workload mutation access is granted.
resource "kubernetes_cluster_role_v1" "collector" {
  metadata {
    name   = "${local.namespace}-${local.name}"
    labels = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["nodes/stats"]
    verbs      = ["get"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "collector" {
  metadata {
    name   = "${local.namespace}-${local.name}"
    labels = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.collector.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.collector.metadata[0].name
    namespace = local.namespace
  }
}

resource "kubernetes_config_map_v1" "collector" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }
  data = { "config.yaml" = local.collector_config }
}

resource "kubernetes_deployment_v1" "collector" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    replicas = 1
    # Exactly one reader owns the existing file offsets and persistent queue.
    strategy { type = "Recreate" }
    selector { match_labels = { "app.kubernetes.io/name" = local.name } }
    template {
      metadata {
        labels = local.labels
        annotations = {
          "checksum/config" = sha256(local.collector_config)
        }
      }
      spec {
        service_account_name             = kubernetes_service_account_v1.collector.metadata[0].name
        automount_service_account_token  = true
        node_selector                    = { "kubernetes.io/hostname" = var.node_name }
        termination_grace_period_seconds = 60
        host_network                     = false
        host_pid                         = false
        host_ipc                         = false
        security_context {
          run_as_user = 0
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = local.name
          image             = var.collector_image
          image_pull_policy = "IfNotPresent"
          args              = ["--config=/etc/otelcol/config.yaml"]
          env {
            name  = "GOMEMLIMIT"
            value = "320MiB"
          }
          env {
            name = "K8S_NODE_IP"
            value_from {
              field_ref { field_path = "status.hostIP" }
            }
          }
          env {
            name = "K8S_NODE_NAME"
            value_from {
              field_ref { field_path = "spec.nodeName" }
            }
          }
          dynamic "env_from" {
            for_each = var.monium_secret_name == null ? [] : [var.monium_secret_name]
            content {
              secret_ref { name = env_from.value }
            }
          }
          resources {
            requests = {
              cpu                 = "100m"
              memory              = "256Mi"
              "ephemeral-storage" = "128Mi"
            }
            limits = {
              cpu                 = "200m"
              memory              = "512Mi"
              "ephemeral-storage" = "512Mi"
            }
          }
          security_context {
            privileged                 = false
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          port {
            name           = "health"
            container_port = 13133
          }
          port {
            name           = "metrics"
            container_port = 8888
          }
          readiness_probe {
            http_get {
              path = "/"
              port = "health"
            }
            period_seconds    = 10
            timeout_seconds   = 5
            failure_threshold = 3
          }
          liveness_probe {
            http_get {
              path = "/"
              port = "health"
            }
            initial_delay_seconds = 30
            period_seconds        = 30
            timeout_seconds       = 5
            failure_threshold     = 5
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/otelcol"
            read_only  = true
          }
          volume_mount {
            name       = "hostfs"
            mount_path = "/hostfs"
            read_only  = true
          }
          volume_mount {
            name       = "game-logs"
            mount_path = "/var/log/pz"
            read_only  = true
          }
          volume_mount {
            name       = "panel-logs"
            mount_path = "/var/log/panel"
            read_only  = true
          }
          volume_mount {
            name       = "offsets"
            mount_path = "/var/lib/otelcol"
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "config"
          config_map { name = kubernetes_config_map_v1.collector.metadata[0].name }
        }
        volume {
          name = "hostfs"
          host_path {
            path = "/"
            type = "Directory"
          }
        }
        volume {
          name = "game-logs"
          host_path {
            path = "${var.stack_path}/data/zomboid"
            type = "Directory"
          }
        }
        volume {
          name = "panel-logs"
          host_path {
            path = "${var.stack_path}/data/panel-logs"
            type = "Directory"
          }
        }
        volume {
          name = "offsets"
          host_path {
            path = "${var.stack_path}/data/otelcol"
            type = "Directory"
          }
        }
        volume {
          name = "tmp"
          empty_dir {
            medium     = "Memory"
            size_limit = "16Mi"
          }
        }
      }
    }
  }
  timeouts {
    create = "10m"
    update = "10m"
  }
  depends_on = [
    kubernetes_cluster_role_binding_v1.collector,
    kubernetes_network_policy_v1.collector,
  ]
}

resource "kubernetes_service_v1" "collector" {
  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }
  spec {
    type     = "ClusterIP"
    selector = { "app.kubernetes.io/name" = local.name }
    port {
      name        = "metrics"
      port        = 8888
      target_port = "metrics"
    }
  }
}
