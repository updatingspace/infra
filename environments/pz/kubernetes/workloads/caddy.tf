resource "kubernetes_config_map_v1" "caddy" {
  metadata {
    name      = "caddy-config"
    namespace = "edge"
  }
  data = { Caddyfile = file("${path.module}/Caddyfile") }
}

resource "kubernetes_deployment_v1" "caddy" {
  metadata {
    name      = "caddy"
    namespace = "edge"
    labels    = local.caddy_labels
  }
  spec {
    replicas               = 1
    revision_history_limit = 3
    strategy {
      type = "Recreate"
    }
    selector {
      match_labels = local.caddy_labels
    }
    template {
      metadata {
        labels      = local.caddy_labels
        annotations = { "checksum/config" = sha256(kubernetes_config_map_v1.caddy.data["Caddyfile"]) }
      }
      spec {
        automount_service_account_token  = false
        termination_grace_period_seconds = 30
        node_selector                    = { "kubernetes.io/hostname" = var.node_name }
        security_context {
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        container {
          name              = "caddy"
          image             = var.caddy_image
          image_pull_policy = "Never"
          env_from {
            secret_ref {
              name = "caddy-runtime"
            }
          }
          resources {
            requests = { cpu = "50m", memory = "64Mi", "ephemeral-storage" = "64Mi" }
            limits   = { cpu = "200m", memory = "256Mi", "ephemeral-storage" = "128Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
              add  = ["NET_BIND_SERVICE"]
            }
          }
          port {
            name           = "http"
            container_port = 80
          }
          port {
            name           = "https"
            container_port = 443
          }
          port {
            name           = "quic"
            container_port = 443
            protocol       = "UDP"
          }
          startup_probe {
            tcp_socket {
              port = "https"
            }
            period_seconds    = 5
            failure_threshold = 60
          }
          readiness_probe {
            tcp_socket {
              port = "https"
            }
            period_seconds = 10
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/caddy"
            read_only  = true
          }
          dynamic "volume_mount" {
            for_each = { caddy-data = "/data", caddy-config = "/config" }
            content {
              name       = volume_mount.key
              mount_path = volume_mount.value
            }
          }
        }
        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map_v1.caddy.metadata[0].name
          }
        }
        dynamic "volume" {
          for_each = toset(["caddy-data", "caddy-config"])
          content {
            name = volume.value
            persistent_volume_claim {
              claim_name = kubernetes_persistent_volume_claim_v1.data[volume.value].metadata[0].name
            }
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "caddy" {
  metadata {
    name      = "caddy"
    namespace = "edge"
  }
  spec {
    type                    = "LoadBalancer"
    selector                = local.caddy_labels
    external_traffic_policy = "Local"
    # Required by k3s ServiceLB when preserving client IPs with Local policy.
    allocate_load_balancer_node_ports = true
    port {
      name        = "http"
      port        = 80
      target_port = "http"
    }
    port {
      name        = "https"
      port        = 443
      target_port = "https"
    }
    port {
      name        = "quic"
      protocol    = "UDP"
      port        = 443
      target_port = "quic"
    }
  }
}
