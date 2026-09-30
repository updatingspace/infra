locals {
  panel_updater_labels = {
    "app.kubernetes.io/name"       = "panel-auto-update"
    "app.kubernetes.io/managed-by" = "terraform"
  }
  panel_updater_script = file("${path.module}/../panel-updater/updater.py")
  panel_updater_image  = "docker.io/library/python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26"
}

resource "kubernetes_config_map_v1" "panel_updater" {
  metadata {
    name      = "panel-auto-update"
    namespace = "zomboid"
    labels    = local.panel_updater_labels
  }
  data = { "updater.py" = local.panel_updater_script }
}

resource "kubernetes_service_account_v1" "panel_updater" {
  metadata {
    name      = "panel-auto-update"
    namespace = "zomboid"
    labels    = local.panel_updater_labels
  }
  automount_service_account_token = false
}

resource "kubernetes_role_v1" "panel_updater" {
  metadata {
    name      = "panel-auto-update"
    namespace = "zomboid"
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = ["panel"]
    verbs          = ["get", "patch"]
  }
  rule {
    api_groups     = [""]
    resources      = ["services"]
    resource_names = ["panel"]
    verbs          = ["get", "patch"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_role_binding_v1" "panel_updater" {
  metadata {
    name      = "panel-auto-update"
    namespace = "zomboid"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.panel_updater.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.panel_updater.metadata[0].name
    namespace = "zomboid"
  }
}

resource "kubernetes_cron_job_v1" "panel_updater" {
  metadata {
    name      = "panel-auto-update"
    namespace = "zomboid"
    labels    = local.panel_updater_labels
  }
  spec {
    schedule                      = "17 * * * *"
    timezone                      = "Etc/UTC"
    suspend                       = var.panel_updater_suspended
    concurrency_policy            = "Forbid"
    starting_deadline_seconds     = 600
    successful_jobs_history_limit = 2
    failed_jobs_history_limit     = 3
    job_template {
      metadata {
        labels = local.panel_updater_labels
      }
      spec {
        backoff_limit              = 0
        active_deadline_seconds    = 900
        ttl_seconds_after_finished = 86400
        template {
          metadata {
            labels      = local.panel_updater_labels
            annotations = { "checksum/updater" = sha256(local.panel_updater_script) }
          }
          spec {
            node_selector                    = { "kubernetes.io/hostname" = var.node_name }
            service_account_name             = kubernetes_service_account_v1.panel_updater.metadata[0].name
            automount_service_account_token  = true
            restart_policy                   = "Never"
            termination_grace_period_seconds = 30
            security_context {
              run_as_non_root = true
              run_as_user     = 1000
              run_as_group    = 1000
              seccomp_profile {
                type = "RuntimeDefault"
              }
            }
            container {
              name              = "updater"
              image             = local.panel_updater_image
              image_pull_policy = "IfNotPresent"
              command           = ["python3", "/opt/updater/updater.py"]
              args              = ["--once"]
              env {
                name  = "POD_NAMESPACE"
                value = "zomboid"
              }
              env {
                name  = "INITIAL_VERSION"
                value = "1.3.7"
              }
              env {
                name  = "UPDATE_TIMEOUT_SECONDS"
                value = "600"
              }
              env {
                name  = "ROLLBACK_TIMEOUT_SECONDS"
                value = "240"
              }
              env {
                name  = "PANEL_TELEMETRY_REQUIRED"
                value = tostring(var.panel_telemetry_enabled)
              }
              env {
                name  = "PYTHONDONTWRITEBYTECODE"
                value = "1"
              }
              env {
                name  = "PYTHONUNBUFFERED"
                value = "1"
              }
              resources {
                requests = { cpu = "25m", memory = "64Mi", "ephemeral-storage" = "16Mi" }
                limits   = { cpu = "100m", memory = "128Mi", "ephemeral-storage" = "64Mi" }
              }
              security_context {
                allow_privilege_escalation = false
                read_only_root_filesystem  = true
                capabilities {
                  drop = ["ALL"]
                }
              }
              volume_mount {
                name       = "script"
                mount_path = "/opt/updater"
                read_only  = true
              }
              volume_mount {
                name       = "panel-data"
                mount_path = "/panel-data"
              }
              volume_mount {
                name       = "tmp"
                mount_path = "/tmp"
              }
            }
            volume {
              name = "script"
              config_map {
                name         = kubernetes_config_map_v1.panel_updater.metadata[0].name
                default_mode = "0444"
              }
            }
            volume {
              name = "panel-data"
              persistent_volume_claim {
                claim_name = kubernetes_persistent_volume_claim_v1.data["panel"].metadata[0].name
              }
            }
            volume {
              name = "tmp"
              empty_dir {
                size_limit = "64Mi"
              }
            }
          }
        }
      }
    }
  }
  # Resume scheduling only after all Terraform-owned panel changes complete.
  depends_on = [
    kubernetes_role_binding_v1.panel_updater,
    kubernetes_deployment_v1.panel,
    kubernetes_service_v1.panel,
  ]
}

# DNS and same-namespace access come from platform. Standard NetworkPolicy can
# allow public HTTPS destinations, not constrain them to GitHub/GHCR hostnames.
resource "kubernetes_network_policy_v1" "panel_updater" {
  metadata {
    name      = "panel-auto-update-egress"
    namespace = "zomboid"
  }
  spec {
    pod_selector {
      match_labels = local.panel_updater_labels
    }
    policy_types = ["Egress"]
    egress {
      to {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = local.non_public_ipv4
        }
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
    egress {
      to {
        ip_block {
          cidr = "10.43.0.1/32"
        }
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
    egress {
      to {
        ip_block {
          cidr = "10.130.0.30/32"
        }
      }
      ports {
        protocol = "TCP"
        port     = "6443"
      }
    }
  }
}
