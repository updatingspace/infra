locals {
  volume_namespaces = {
    pz-server    = "zomboid"
    zomboid      = "zomboid"
    steam        = "zomboid"
    panel        = "zomboid"
    panel-logs   = "zomboid"
    caddy-data   = "edge"
    caddy-config = "edge"
  }
  game_labels  = { "app.kubernetes.io/name" = "zomboid", "app.kubernetes.io/managed-by" = "terraform" }
  panel_labels = { "app.kubernetes.io/name" = "panel", "app.kubernetes.io/managed-by" = "terraform" }
  caddy_labels = { "app.kubernetes.io/name" = "caddy", "app.kubernetes.io/managed-by" = "terraform" }
}

resource "kubernetes_storage_class_v1" "retained_local" {
  metadata {
    name = "pz-retained-local"
  }
  storage_provisioner    = "kubernetes.io/no-provisioner"
  reclaim_policy         = "Retain"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_persistent_volume_v1" "data" {
  for_each = local.volume_namespaces
  metadata {
    name   = "pz-${each.key}"
    labels = { "app.kubernetes.io/managed-by" = "terraform" }
  }
  spec {
    capacity                         = { storage = "${var.volume_capacity_gi[each.key]}Gi" }
    access_modes                     = ["ReadWriteOnce"]
    storage_class_name               = kubernetes_storage_class_v1.retained_local.metadata[0].name
    persistent_volume_reclaim_policy = "Retain"
    volume_mode                      = "Filesystem"
    persistent_volume_source {
      local {
        path = "${startswith(each.key, "caddy-") && var.edge_storage_root != null ? var.edge_storage_root : var.storage_root}/${each.key}"
      }
    }
    claim_ref {
      name      = each.key
      namespace = each.value
    }
    node_affinity {
      required {
        node_selector_term {
          match_expressions {
            key      = "kubernetes.io/hostname"
            operator = "In"
            values   = [var.node_name]
          }
        }
      }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_persistent_volume_claim_v1" "data" {
  for_each = local.volume_namespaces
  metadata {
    name      = each.key
    namespace = each.value
  }
  wait_until_bound = false
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = kubernetes_storage_class_v1.retained_local.metadata[0].name
    volume_name        = kubernetes_persistent_volume_v1.data[each.key].metadata[0].name
    resources {
      requests = { storage = "${var.volume_capacity_gi[each.key]}Gi" }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}
