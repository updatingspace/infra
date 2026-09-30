resource "kubernetes_deployment_v1" "panel" {
  metadata {
    name      = "panel"
    namespace = "zomboid"
    labels    = local.panel_labels
  }
  spec {
    replicas               = 1
    revision_history_limit = 3
    strategy {
      # A second writer must never open the panel database during an update.
      type = "Recreate"
    }
    selector {
      match_labels = local.panel_labels
    }
    template {
      metadata {
        # Release routing may change; the immutable Deployment selector above does not.
        labels = merge(local.panel_labels, { "pz.updspace.com/release-slot" = "initial" })
        annotations = var.panel_telemetry_enabled ? {
          "checksum/panel-telemetry" = sha256(jsonencode(local.panel_telemetry_sources))
        } : {}
      }
      spec {
        automount_service_account_token  = var.panel_telemetry_enabled
        service_account_name             = var.panel_telemetry_enabled ? kubernetes_service_account_v1.panel_telemetry.metadata[0].name : "default"
        termination_grace_period_seconds = 30
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
          name              = "panel"
          image             = var.panel_image
          image_pull_policy = "Never"
          env_from {
            secret_ref {
              name = "pz-panel"
            }
          }
          env {
            name  = "RCON_HOST"
            value = "zomboid.zomboid.svc.cluster.local"
          }
          env {
            name  = "NODE_OPTIONS"
            value = var.panel_telemetry_enabled ? "--max-old-space-size=512 --import=/opt/panel-telemetry/preload.mjs" : "--max-old-space-size=512"
          }
          env {
            name  = "PANEL_DOCKER_CONTROL_ENABLED"
            value = "false"
          }
          dynamic "env" {
            for_each = {
              PZ_TELEMETRY_NAMESPACE      = "zomboid"
              PZ_TELEMETRY_POD            = "zomboid-0"
              PZ_TELEMETRY_CONTAINER      = "zomboid"
              PZ_TELEMETRY_PROMETHEUS_URL = "http://zomboid:9090/metrics"
              PZ_TELEMETRY_SCOPE          = "game-container-v1"
              PZ_TELEMETRY_SERVER_ID      = var.panel_telemetry_server_id
            }
            content {
              name  = env.key
              value = env.value
            }
          }
          volume_mount {
            name       = "panel-telemetry"
            mount_path = "/opt/panel-telemetry"
            read_only  = true
          }
          resources {
            requests = { cpu = "100m", memory = "256Mi", "ephemeral-storage" = "128Mi" }
            limits   = { cpu = "300m", memory = "768Mi", "ephemeral-storage" = "512Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }
          port {
            name           = "http"
            container_port = 3001
          }
          startup_probe {
            http_get {
              path = "/api/health"
              port = "http"
            }
            period_seconds    = 10
            timeout_seconds   = 5
            failure_threshold = 30
          }
          readiness_probe {
            http_get {
              path = "/api/health"
              port = "http"
            }
            period_seconds    = 10
            timeout_seconds   = 5
            failure_threshold = 3
          }
          liveness_probe {
            http_get {
              path = "/api/health"
              port = "http"
            }
            period_seconds    = 30
            timeout_seconds   = 5
            failure_threshold = 5
          }
          dynamic "volume_mount" {
            for_each = {
              panel      = "/app/data"
              panel-logs = "/app/logs"
              pz-server  = "/pz-server"
              zomboid    = "/zomboid"
            }
            content {
              name       = volume_mount.key
              mount_path = volume_mount.value
              read_only  = volume_mount.key == "pz-server"
            }
          }
        }
        dynamic "volume" {
          for_each = toset(["panel", "panel-logs", "pz-server", "zomboid"])
          content {
            name = volume.value
            persistent_volume_claim {
              claim_name = kubernetes_persistent_volume_claim_v1.data[volume.value].metadata[0].name
              read_only  = volume.value == "pz-server"
            }
          }
        }
        volume {
          name = "panel-telemetry"
          config_map {
            name         = kubernetes_config_map_v1.panel_telemetry.metadata[0].name
            default_mode = "0444"
          }
        }
      }
    }
  }
  lifecycle {
    # The updater owns only release choice/routing. Replicas and all other
    # resources, credentials, probes and security settings remain declarative.
    ignore_changes = [
      spec[0].template[0].spec[0].container[0].image,
      spec[0].template[0].spec[0].container[0].image_pull_policy,
      spec[0].template[0].metadata[0].labels["pz.updspace.com/release-slot"],
    ]
  }
  depends_on = [
    kubernetes_role_binding_v1.panel_telemetry,
    kubernetes_network_policy_v1.panel_telemetry_api,
  ]
}

resource "kubernetes_service_v1" "panel" {
  metadata {
    name      = "panel"
    namespace = "zomboid"
  }
  spec {
    type     = "ClusterIP"
    selector = merge(local.panel_labels, { "pz.updspace.com/release-slot" = "initial" })
    port {
      name        = "http"
      port        = 3001
      target_port = "http"
    }
  }
  lifecycle {
    ignore_changes = [spec[0].selector["pz.updspace.com/release-slot"]]
  }
}
