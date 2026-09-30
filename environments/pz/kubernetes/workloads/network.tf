resource "kubernetes_network_policy_v1" "game_public" {
  metadata {
    name      = "game-public-udp"
    namespace = "zomboid"
  }
  spec {
    pod_selector {
      match_labels = local.game_labels
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        protocol = "UDP"
        port     = "16261"
      }
      ports {
        protocol = "UDP"
        port     = "16262"
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "caddy_public" {
  metadata {
    name      = "caddy-public-web"
    namespace = "edge"
  }
  spec {
    pod_selector {
      match_labels = local.caddy_labels
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        protocol = "TCP"
        port     = "80"
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
      ports {
        protocol = "UDP"
        port     = "443"
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "panel_from_caddy" {
  metadata {
    name      = "panel-from-caddy"
    namespace = "zomboid"
  }
  spec {
    pod_selector {
      match_labels = local.panel_labels
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "edge" }
        }
        pod_selector {
          match_labels = local.caddy_labels
        }
      }
      ports {
        protocol = "TCP"
        port     = "3001"
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "caddy_to_panel" {
  metadata {
    name      = "caddy-to-panel"
    namespace = "edge"
  }
  spec {
    pod_selector {
      match_labels = local.caddy_labels
    }
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "zomboid" }
        }
        pod_selector {
          match_labels = local.panel_labels
        }
      }
      ports {
        protocol = "TCP"
        port     = "3001"
      }
    }
  }
}

# Steam/Workshop peers use multiple ports and dynamic public IPs. This does not
# open private networks, cloud instance metadata, loopback, or multicast.
resource "kubernetes_network_policy_v1" "game_internet" {
  metadata {
    name      = "game-public-internet"
    namespace = "zomboid"
  }
  spec {
    pod_selector {
      match_labels = local.game_labels
    }
    policy_types = ["Egress"]
    egress {
      to {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = local.non_public_ipv4
        }
      }
    }
  }
}

locals {
  non_public_ipv4 = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3"
  ]
  web_egress = {
    panel = { namespace = "zomboid", labels = local.panel_labels }
    caddy = { namespace = "edge", labels = local.caddy_labels }
  }
  monitored = {
    game  = { labels = local.game_labels, port = "9090" }
    panel = { labels = local.panel_labels, port = "3001" }
  }
}

# The panel checks Steam Workshop versions; Caddy renews ACME certificates.
resource "kubernetes_network_policy_v1" "public_web_egress" {
  for_each = local.web_egress
  metadata {
    name      = "${each.key}-public-web-egress"
    namespace = each.value.namespace
  }
  spec {
    pod_selector {
      match_labels = each.value.labels
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
        port     = "80"
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "observability" {
  for_each = local.monitored
  metadata {
    name      = "${each.key}-from-observability"
    namespace = "zomboid"
  }
  spec {
    pod_selector {
      match_labels = each.value.labels
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "observability" }
        }
        pod_selector {
          match_labels = { "app.kubernetes.io/name" = "otel-collector" }
        }
      }
      ports {
        protocol = "TCP"
        port     = each.value.port
      }
    }
  }
}
