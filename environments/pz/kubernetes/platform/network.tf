resource "kubernetes_network_policy_v1" "default_deny" {
  for_each = var.environments

  metadata {
    name      = "default-deny"
    namespace = kubernetes_namespace_v1.environment[each.key].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

# Service-to-service traffic is allowed inside an environment, not across them.
resource "kubernetes_network_policy_v1" "same_namespace" {
  for_each = var.environments

  metadata {
    name      = "allow-same-namespace"
    namespace = kubernetes_namespace_v1.environment[each.key].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
    ingress {
      from {
        pod_selector {}
      }
    }
    egress {
      to {
        pod_selector {}
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "dns" {
  for_each = var.environments

  metadata {
    name      = "allow-cluster-dns"
    namespace = kubernetes_namespace_v1.environment[each.key].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_labels = { "k8s-app" = "kube-dns" }
        }
      }
      ports {
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
    }
  }
}

# Add only reviewed external ranges. Broad CIDRs can also expose cluster/private
# addresses; NetworkPolicy is additive, so default-deny does not override this.
resource "kubernetes_network_policy_v1" "external_egress" {
  for_each = { for name, env in var.environments : name => env if length(env.egress_cidrs) > 0 }

  metadata {
    name      = "allow-external-egress"
    namespace = kubernetes_namespace_v1.environment[each.key].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      dynamic "to" {
        for_each = each.value.egress_cidrs
        content {
          ip_block {
            cidr = to.value
          }
        }
      }
    }
  }
}
