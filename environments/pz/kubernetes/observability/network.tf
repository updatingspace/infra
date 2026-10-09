resource "kubernetes_network_policy_v1" "collector" {
  metadata {
    name      = "allow-collector-egress"
    namespace = data.kubernetes_namespace_v1.observability.metadata[0].name
  }
  spec {
    pod_selector { match_labels = { "app.kubernetes.io/name" = local.name } }
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "zomboid" }
        }
      }
      ports {
        protocol = "TCP"
        port     = "9090"
      }
      ports {
        protocol = "TCP"
        port     = "3001"
      }
    }
    egress {
      to {
        ip_block { cidr = "${var.node_private_ip}/32" }
      }
      ports {
        protocol = "TCP"
        port     = "10250"
      }
      ports {
        protocol = "TCP"
        port     = "9109"
      }
    }
    # Standard NetworkPolicy cannot filter destination DNS names. Restrict
    # external delivery to HTTPS, excluding local/private/metadata addresses.
    egress {
      to {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = ["0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"]
        }
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
  }
}
