resource "kubernetes_stateful_set_v1" "game" {
  metadata {
    name      = "zomboid"
    namespace = "zomboid"
    labels    = local.game_labels
  }
  wait_for_rollout = false
  spec {
    replicas               = 1
    service_name           = kubernetes_service_v1.game.metadata[0].name
    pod_management_policy  = "OrderedReady"
    revision_history_limit = 3
    # Applying an image/config change cannot restart a live world automatically.
    # Save and stop explicitly, then delete the old pod without force to update.
    update_strategy {
      type = "OnDelete"
    }
    selector {
      match_labels = local.game_labels
    }
    template {
      metadata {
        labels = local.game_labels
      }
      spec {
        automount_service_account_token  = false
        termination_grace_period_seconds = 300
        node_selector                    = { "kubernetes.io/hostname" = var.node_name }
        security_context {
          run_as_non_root = true
          run_as_user     = 1000
          run_as_group    = 1000
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        container {
          name              = "zomboid"
          image             = var.game_image
          image_pull_policy = "Never"
          # Python PID 1 handles SIGTERM: RCON save, quit, await process exit.
          command = ["python3", "/usr/local/bin/pz-game"]
          args    = ["run"]
          env_from {
            secret_ref {
              name = "pz-runtime"
            }
          }
          env {
            name  = "RCON_HOST"
            value = "127.0.0.1"
          }
          env {
            name  = "PZ_XMS"
            value = "4g"
          }
          env {
            name  = "PZ_XMX"
            value = "8g"
          }
          resources {
            # PZ's parallel startup ZIP backup stages compressed data in /tmp.
            # Existing backups reach 1.4 GiB; 1 GiB caused eviction before startup.
            requests = { cpu = "1500m", memory = "8Gi", "ephemeral-storage" = "1536Mi" }
            limits   = { cpu = "2200m", memory = "10Gi", "ephemeral-storage" = "3Gi" }
          }
          security_context {
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }
          port {
            name           = "game"
            container_port = 16261
            protocol       = "UDP"
          }
          port {
            name           = "direct"
            container_port = 16262
            protocol       = "UDP"
          }
          port {
            name           = "rcon"
            container_port = 27015
            protocol       = "TCP"
          }
          port {
            name           = "metrics"
            container_port = 9090
            protocol       = "TCP"
          }
          startup_probe {
            exec {
              command = ["python3", "/usr/local/bin/pz-game", "health"]
            }
            period_seconds  = 30
            timeout_seconds = 20
            # The large existing world starts under a 2.2 CPU limit.
            failure_threshold = 60
          }
          readiness_probe {
            exec {
              command = ["python3", "/usr/local/bin/pz-game", "health"]
            }
            period_seconds    = 30
            timeout_seconds   = 20
            failure_threshold = 5
          }
          # No liveness restart: a slow save must not cause an automatic kill.
          dynamic "volume_mount" {
            for_each = { pz-server = "/pz-server", zomboid = "/zomboid", steam = "/home/steam/Steam" }
            content {
              name       = volume_mount.key
              mount_path = volume_mount.value
            }
          }
        }
        dynamic "volume" {
          for_each = toset(["pz-server", "zomboid", "steam"])
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
  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_service_v1" "game" {
  metadata {
    name      = "zomboid"
    namespace = "zomboid"
  }
  spec {
    selector   = local.game_labels
    type       = "ClusterIP"
    cluster_ip = "None"
    port {
      name        = "rcon"
      port        = 27015
      target_port = "rcon"
    }
    port {
      name        = "metrics"
      port        = 9090
      target_port = "metrics"
    }
  }
}

resource "kubernetes_service_v1" "game_public" {
  # Local ServiceLB announces this node only after the game endpoint is Ready.
  # World startup is checked separately with the full 30-minute startup budget.
  wait_for_load_balancer = false
  metadata {
    name      = "zomboid-public"
    namespace = "zomboid"
  }
  spec {
    type                    = "LoadBalancer"
    selector                = local.game_labels
    external_traffic_policy = "Local"
    # k3s ServiceLB Local mode forwards to nodeIP:NodePort, not ClusterIP.
    # Disabling allocation here would set its destination port to zero.
    allocate_load_balancer_node_ports = true
    port {
      name        = "game"
      protocol    = "UDP"
      port        = 16261
      target_port = "game"
    }
    port {
      name        = "direct"
      protocol    = "UDP"
      port        = 16262
      target_port = "direct"
    }
  }
}
