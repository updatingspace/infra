# Plans use a mock provider: no cluster access, credentials, or Monium writes.
mock_provider "kubernetes" {}

variables {
  kubeconfig_context = "test-zomboid"
}

run "single_queue_owner_and_retained_host_data" {
  command = plan

  assert {
    condition = (
      tonumber(kubernetes_deployment_v1.collector.spec[0].replicas) == 1 &&
      kubernetes_deployment_v1.collector.spec[0].strategy[0].type == "Recreate" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].node_selector["kubernetes.io/hostname"] == var.node_name
    )
    error_message = "Only one collector may own the existing offsets and disk queue on their original node."
  }

  assert {
    condition = alltrue([
      for volume in kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].volume :
      alltrue([for host in volume.host_path : host.type == "Directory" && !strcontains(host.path, "docker.sock")])
    ])
    error_message = "Host mounts must exist already and must never expose the Docker socket."
  }

  assert {
    condition = one([
      for volume in kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].volume :
      volume.host_path[0].path if volume.name == "offsets"
    ]) == "${var.stack_path}/data/otelcol"
    error_message = "Keep the existing persistent queue and log offsets instead of silently switching to empty storage."
  }

  assert {
    condition = alltrue([
      for mount in kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].volume_mount :
      mount.read_only if contains(["hostfs", "game-logs", "panel-logs", "config"], mount.name)
    ])
    error_message = "Host metrics and game/panel logs must be mounted read-only."
  }
}

run "minimum_node_trust" {
  command = plan

  assert {
    condition = (
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].host_network == false &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].host_pid == false &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].host_ipc == false &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].security_context[0].privileged == false &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].security_context[0].allow_privilege_escalation == false &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].security_context[0].read_only_root_filesystem == true &&
      contains(kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].security_context[0].capabilities[0].drop, "ALL") &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].security_context[0].seccomp_profile[0].type == "RuntimeDefault"
    )
    error_message = "HostPath exception must not become privilege escalation, writable rootfs or extra capabilities."
  }

  assert {
    condition = (
      length(kubernetes_cluster_role_v1.collector.rule) == 1 &&
      toset(kubernetes_cluster_role_v1.collector.rule[0].resources) == toset(["nodes/stats"]) &&
      toset(kubernetes_cluster_role_v1.collector.rule[0].verbs) == toset(["get"]) &&
      toset(kubernetes_cluster_role_v1.collector.rule[0].api_groups) == toset([""])
    )
    error_message = "Kubelet summary collection only needs get nodes/stats, never secrets, nodes/proxy or mutation permissions."
  }

  assert {
    condition = (
      yamldecode(local.collector_config).receivers.kubelet_stats.auth_type == "serviceAccount" &&
      yamldecode(local.collector_config).receivers.kubelet_stats.insecure_skip_verify == false &&
      startswith(yamldecode(local.collector_config).receivers.kubelet_stats.endpoint, "https://") &&
      !contains(keys(yamldecode(local.collector_config).receivers), "docker_stats")
    )
    error_message = "Use authenticated, CA-verified kubelet HTTPS and remove obsolete Docker socket collection."
  }

  assert {
    condition = (
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].env_from[0].secret_ref[0].name == var.monium_secret_name &&
      yamldecode(local.collector_config).exporters["otlp_http/monium"].headers.Authorization == "Api-Key $${env:MONIUM_API_KEY}" &&
      yamldecode(local.collector_config).exporters["otlp_http/monium_logs"].headers.Authorization == "Api-Key $${env:MONIUM_LOGS_API_KEY}"
    )
    error_message = "Monium keys must remain runtime references to the pre-existing Secret."
  }
}

run "budget_and_serving_probes" {
  command = plan

  assert {
    condition = (
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].resources[0].requests["cpu"] == "100m" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].resources[0].limits["cpu"] == "200m" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].resources[0].requests["memory"] == "256Mi" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "512Mi" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].resources[0].requests["ephemeral-storage"] == "128Mi" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].resources[0].limits["ephemeral-storage"] == "512Mi"
    )
    error_message = "Collector requests/limits must fit the reserved observability namespace budget."
  }

  assert {
    condition = (
      yamldecode(local.collector_config).processors.memory_limiter.limit_mib < 512 &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].readiness_probe[0].http_get[0].port == "health" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].liveness_probe[0].http_get[0].port == "health" &&
      kubernetes_service_v1.collector.spec[0].type == "ClusterIP"
    )
    error_message = "Collector needs real health probes, limiter below the pod cap and no public telemetry Service."
  }

  assert {
    condition = (
      yamldecode(local.collector_config).extensions.file_storage.directory == "/var/lib/otelcol" &&
      yamldecode(local.collector_config).exporters["otlp_http/monium_logs"].sending_queue.storage == "file_storage" &&
      yamldecode(local.collector_config).receivers["filelog/game"].storage == "file_storage" &&
      yamldecode(local.collector_config).receivers["filelog/panel"].storage == "file_storage"
    )
    error_message = "File offsets and log delivery must retain the same durable storage extension."
  }
}

run "bounded_egress" {
  command = plan

  assert {
    condition = alltrue([
      for rule in kubernetes_network_policy_v1.collector.spec[0].egress :
      length(rule.to) > 0 && length(rule.ports) > 0 && alltrue([
        for port in rule.ports : port.protocol == "TCP" && contains(["9090", "3001", "10250", "9109", "443"], port.port)
      ])
    ])
    error_message = "Collector policy must not grow unrestricted egress destinations or ports. DNS is a separate platform policy."
  }

  assert {
    condition = one([
      for rule in kubernetes_network_policy_v1.collector.spec[0].egress :
      rule.to[0].ip_block[0].cidr if contains([for port in rule.ports : port.port], "10250")
    ]) == "${var.node_private_ip}/32"
    error_message = "Kubelet access must be restricted to the monitored node IP."
  }

  assert {
    condition = alltrue([
      for rule in kubernetes_network_policy_v1.collector.spec[0].egress :
      alltrue([
        for excluded in ["10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"] :
        contains(rule.to[0].ip_block[0].except, excluded)
      ]) if contains([for port in rule.ports : port.port], "443")
    ])
    error_message = "External HTTPS access must not open private, loopback or cloud metadata addresses."
  }
}

run "private_backup_monitoring" {
  command = plan
  assert {
    condition = (
      yamldecode(local.collector_config).receivers["prometheus/backup"].config.scrape_configs[0].static_configs[0].targets == ["$${env:K8S_NODE_IP}:9109"] &&
      yamldecode(local.collector_config).receivers["prometheus/backup"].config.scrape_configs[0].scrape_interval == "60s" &&
      yamldecode(local.collector_config).processors["resource/backup"].attributes[0].value == "pz-backup" &&
      yamldecode(local.collector_config).service.pipelines["metrics/backup"].processors == ["memory_limiter", "resource/common", "resource/backup"] &&
      yamldecode(local.collector_config).service.pipelines["metrics/backup"].exporters == ["otlp_http/monium"]
    )
    error_message = "Backup telemetry must retain an independent service identity and scrape only the private host exporter."
  }
  assert {
    condition = one([
      for rule in kubernetes_network_policy_v1.collector.spec[0].egress :
      rule.to[0].ip_block[0].cidr if contains([for port in rule.ports : port.port], "9109")
    ]) == "${var.node_private_ip}/32"
    error_message = "Backup scrape egress must target only this node, never a public or subnet-wide listener."
  }
}

run "reject_empty_cluster_context" {
  command = plan
  variables { kubeconfig_context = " " }
  expect_failures = [var.kubeconfig_context]
}

run "reject_root_as_stack_path" {
  command = plan
  variables { stack_path = "/" }
  expect_failures = [var.stack_path]
}

run "reject_invalid_node_ip" {
  command = plan
  variables { node_private_ip = "not-an-ip" }
  expect_failures = [var.node_private_ip]
}

run "default_collector_digest_is_pinned" {
  command = plan
  assert {
    condition = (
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].image == "otel/opentelemetry-collector-contrib:0.161.0@sha256:fd328de2552466ad78385e1b1289c3f2402b1c45f265b252aab1955b42845ac1" &&
      kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].image_pull_policy == "IfNotPresent"
    )
    error_message = "The default collector must retain the verified production digest and reuse an available local image."
  }
}

run "use_preimported_collector_image" {
  command = plan
  variables {
    collector_image = "docker.io/local/otel-collector:migration-fixture"
  }
  assert {
    condition     = kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].image == "docker.io/local/otel-collector:migration-fixture"
    error_message = "Offline migration must use the exact imported tag without substituting a remote registry digest."
  }
}

run "local_collector_without_cloud_credentials" {
  command = plan
  variables {
    node_name          = "updspace-home"
    node_private_ip    = "192.168.1.176"
    monium_secret_name = null
  }
  assert {
    condition     = length(kubernetes_deployment_v1.collector.spec[0].template[0].spec[0].container[0].env_from) == 0
    error_message = "Local-only telemetry must not require or mount Monium credentials."
  }
}
