output "services" {
  value = {
    game_public = "16261/udp, 16262/udp on the node"
    rcon        = "zomboid.zomboid.svc.cluster.local:27015"
    metrics     = "zomboid.zomboid.svc.cluster.local:9090"
    panel       = "panel.zomboid.svc.cluster.local:3001"
    web_public  = "80/tcp, 443/tcp, 443/udp on the node"
  }
}

output "persistent_data" {
  value = { for name, namespace in local.volume_namespaces : name => {
    namespace = namespace
    path      = "${var.storage_root}/${name}"
    capacity  = "${var.volume_capacity_gi[name]}Gi"
  } }
}
