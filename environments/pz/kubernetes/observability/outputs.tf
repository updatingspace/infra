output "collector" {
  value = {
    namespace = local.namespace
    workload  = kubernetes_deployment_v1.collector.metadata[0].name
    metrics   = "http://${local.name}.${local.namespace}.svc.cluster.local:8888/metrics"
    secret    = var.monium_secret_name
    node      = var.node_name
  }
}
