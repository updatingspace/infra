mock_provider "kubernetes" {}

variables {
  kubeconfig_context = "test-only"
  game_image         = "docker.io/local/pz-server:migration-fixture"
  panel_image        = "docker.io/local/pz-panel:migration-fixture"
  caddy_image        = "docker.io/library/caddy:migration-fixture"
}

run "preserve_world_and_single_writers" {
  command = plan

  assert {
    condition = (
      tonumber(kubernetes_stateful_set_v1.game.spec[0].replicas) == 1 &&
      kubernetes_stateful_set_v1.game.spec[0].update_strategy[0].type == "OnDelete" &&
      kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].termination_grace_period_seconds == 300 &&
      kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].command == tolist(["python3", "/usr/local/bin/pz-game"]) &&
      length(kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].liveness_probe) == 0 &&
      kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].startup_probe[0].failure_threshold * kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].startup_probe[0].period_seconds >= 1800
    )
    error_message = "Game migration must preserve SIGTERM save/quit, a 5-minute shutdown, >=30-minute startup and operator-controlled single-pod replacement."
  }
  assert {
    condition = (
      tonumber(kubernetes_deployment_v1.panel.spec[0].replicas) == 1 &&
      kubernetes_deployment_v1.panel.spec[0].strategy[0].type == "Recreate" &&
      tonumber(kubernetes_deployment_v1.caddy.spec[0].replicas) == 1 &&
      kubernetes_deployment_v1.caddy.spec[0].strategy[0].type == "Recreate"
    )
    error_message = "Database and TLS data must never gain a concurrent replica during an update."
  }
  assert {
    condition = alltrue([for pv in values(kubernetes_persistent_volume_v1.data) :
      pv.spec[0].persistent_volume_reclaim_policy == "Retain" &&
      length(pv.spec[0].persistent_volume_source[0].local) == 1 &&
      toset(pv.spec[0].node_affinity[0].required[0].node_selector_term[0].match_expressions[0].values) == toset([var.node_name])
    ])
    error_message = "Every data volume must remain on the original node with retained local storage."
  }
  assert {
    condition = (
      kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "10Gi" &&
      kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].resources[0].limits["cpu"] == "2200m" &&
      kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "768Mi" &&
      kubernetes_deployment_v1.caddy.spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "256Mi"
    )
    error_message = "The workloads must fit the approved current-VM memory and CPU budget."
  }
  assert {
    condition = (
      alltrue([for mount in kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].container[0].volume_mount : mount.read_only if mount.name == "pz-server"]) &&
      kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].container[0].env_from[0].secret_ref[0].name == "pz-panel" &&
      kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].env_from[0].secret_ref[0].name == "pz-runtime"
    )
    error_message = "Panel binaries must be read-only and runtime credentials must come from preexisting secrets."
  }
}

run "restrict_exposure" {
  command = plan
  assert {
    condition = (
      kubernetes_service_v1.game_public.spec[0].external_traffic_policy == "Local" &&
      kubernetes_service_v1.game_public.spec[0].allocate_load_balancer_node_ports == true &&
      kubernetes_service_v1.caddy.spec[0].external_traffic_policy == "Local" &&
      kubernetes_service_v1.caddy.spec[0].allocate_load_balancer_node_ports == true &&
      alltrue([for p in kubernetes_service_v1.game_public.spec[0].port : p.protocol == "UDP" && contains([16261, 16262], p.port)]) &&
      kubernetes_service_v1.panel.spec[0].type == "ClusterIP" &&
      contains(kubernetes_network_policy_v1.game_internet.spec[0].egress[0].to[0].ip_block[0].except, "169.254.0.0/16") &&
      contains(kubernetes_network_policy_v1.public_web_egress["panel"].spec[0].egress[0].to[0].ip_block[0].except, "10.0.0.0/8")
    )
    error_message = "Public ports must exclude RCON/panel, Local ServiceLB requires NodePorts, and public egress must exclude private/metadata networks."
  }
  assert {
    condition = (
      kubernetes_network_policy_v1.panel_from_caddy.spec[0].ingress[0].from[0].namespace_selector[0].match_labels["kubernetes.io/metadata.name"] == "edge" &&
      kubernetes_network_policy_v1.panel_from_caddy.spec[0].ingress[0].from[0].pod_selector[0].match_labels["app.kubernetes.io/name"] == "caddy"
    )
    error_message = "Only Caddy pods in edge may use the cross-namespace panel route."
  }
}

run "confine_automatic_panel_updates" {
  command = plan
  assert {
    condition = (
      kubernetes_cron_job_v1.panel_updater.spec[0].suspend &&
      kubernetes_cron_job_v1.panel_updater.spec[0].concurrency_policy == "Forbid" &&
      kubernetes_cron_job_v1.panel_updater.spec[0].timezone == "Etc/UTC" &&
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].backoff_limit == 0 &&
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].active_deadline_seconds == 900 &&
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].restart_policy == "Never"
    )
    error_message = "Automatic panel changes require explicit unsuspension and cannot overlap or silently retry a failed database migration."
  }
  assert {
    condition = (
      !contains(keys(kubernetes_deployment_v1.panel.spec[0].selector[0].match_labels), "pz.updspace.com/release-slot") &&
      kubernetes_deployment_v1.panel.spec[0].template[0].metadata[0].labels["pz.updspace.com/release-slot"] == kubernetes_service_v1.panel.spec[0].selector["pz.updspace.com/release-slot"] &&
      kubernetes_deployment_v1.panel.spec[0].template[0].metadata[0].labels["pz.updspace.com/release-slot"] == "initial"
    )
    error_message = "Initial panel traffic must match the pod release, while future release changes must not replace the immutable Deployment selector."
  }
  assert {
    condition = (
      length(kubernetes_role_v1.panel_updater.rule) == 4 &&
      alltrue([for rule in kubernetes_role_v1.panel_updater.rule :
        length(rule.resources) == 1 && (
          (contains(["deployments", "services"], one(rule.resources)) && toset(rule.resource_names) == toset(["panel"]) && toset(rule.verbs) == toset(["get", "patch"])) ||
          (contains(["pods", "replicasets"], one(rule.resources)) && length(coalesce(rule.resource_names, toset([]))) == 0 && toset(rule.verbs) == toset(["get", "list"]))
        )
      ])
    )
    error_message = "Updater permissions must only mutate the named panel Deployment/Service, never Secrets, exec, game objects or pod deletion."
  }
  assert {
    condition = (
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].security_context[0].run_as_non_root &&
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].security_context[0].run_as_user == "1000" &&
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].read_only_root_filesystem &&
      !kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].allow_privilege_escalation &&
      toset(kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].capabilities[0].drop) == toset(["ALL"]) &&
      length(kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env_from) == 0 &&
      length([for volume in kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].volume : volume if length(volume.persistent_volume_claim) != 0]) == 1 &&
      alltrue([for volume in kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].volume :
        volume.persistent_volume_claim[0].claim_name == kubernetes_persistent_volume_claim_v1.data["panel"].metadata[0].name if length(volume.persistent_volume_claim) != 0
      ])
    )
    error_message = "Updater must run unprivileged, with no runtime secrets or data volumes except the panel database backup volume."
  }
  assert {
    condition = (
      sum([for value in [
        kubernetes_stateful_set_v1.game.spec[0].template[0].spec[0].container[0].resources[0].limits["cpu"],
        kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].container[0].resources[0].limits["cpu"],
        kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].resources[0].limits["cpu"]
      ] : tonumber(trimsuffix(value, "m"))]) <= 2600 &&
      kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].container[0].resources[0].limits["cpu"] == "300m" &&
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "128Mi" &&
      kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].resources[0].limits["ephemeral-storage"] == "64Mi"
    )
    error_message = "The updater must fit the unchanged 2600m namespace CPU limit and its approved small memory/disk budget."
  }
  assert {
    condition = (
      length(kubernetes_network_policy_v1.panel_updater.spec[0].egress) == 3 &&
      alltrue([for rule in kubernetes_network_policy_v1.panel_updater.spec[0].egress :
        length(rule.to) == 1 && length(rule.ports) == 1 && rule.ports[0].protocol == "TCP" &&
        contains(["0.0.0.0/0:443", "10.43.0.1/32:443", "10.130.0.30/32:6443"], "${rule.to[0].ip_block[0].cidr}:${rule.ports[0].port}")
      ]) &&
      alltrue([for rule in kubernetes_network_policy_v1.panel_updater.spec[0].egress :
        contains(rule.to[0].ip_block[0].except, "169.254.0.0/16") && contains(rule.to[0].ip_block[0].except, "10.0.0.0/8") if rule.to[0].ip_block[0].cidr == "0.0.0.0/0"
      ])
    )
    error_message = "Updater additional egress is public HTTPS without metadata/private destinations, plus only the exact cluster API addresses."
  }
}

run "read_only_game_telemetry" {
  command = plan
  assert {
    condition = (
      length(kubernetes_role_v1.panel_telemetry.rule) == 2 &&
      alltrue([for rule in kubernetes_role_v1.panel_telemetry.rule :
        toset(rule.resources) == toset(["pods"]) &&
        toset(rule.resource_names) == toset(["zomboid-0"]) &&
        toset(rule.verbs) == toset(["get"]) &&
        contains(["", "metrics.k8s.io"], one(rule.api_groups))
      ]) &&
      !kubernetes_service_account_v1.panel_telemetry.automount_service_account_token &&
      kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].automount_service_account_token &&
      kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].service_account_name == kubernetes_service_account_v1.panel_telemetry.metadata[0].name
    )
    error_message = "Telemetry may only read the named game Pod and its metrics, never other pods, Secrets or control operations."
  }
  assert {
    condition = (
      length(kubernetes_network_policy_v1.panel_telemetry_api.spec[0].egress) == 2 &&
      alltrue([for rule in kubernetes_network_policy_v1.panel_telemetry_api.spec[0].egress :
        length(rule.to) == 1 && length(rule.ports) == 1 && rule.ports[0].protocol == "TCP" &&
        contains(["10.43.0.1/32:443", "10.130.0.30/32:6443"], "${rule.to[0].ip_block[0].cidr}:${rule.ports[0].port}")
      ]) &&
      alltrue([for mount in kubernetes_deployment_v1.panel.spec[0].template[0].spec[0].container[0].volume_mount :
        mount.read_only && mount.mount_path == "/opt/panel-telemetry" if mount.name == "panel-telemetry"
      ]) &&
      one([for env in kubernetes_cron_job_v1.panel_updater.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env : env.value if env.name == "PANEL_TELEMETRY_REQUIRED"]) == "true"
    )
    error_message = "Telemetry must add only exact API access, use a read-only adapter mount, and preserve the adapter on future image updates."
  }
}

run "reject_incomplete_storage_mapping" {
  command = plan
  variables {
    volume_capacity_gi = { zomboid = 12 }
  }
  expect_failures = [var.volume_capacity_gi]
}

run "reject_storage_requests_exceeding_physical_environment" {
  command = plan
  variables {
    volume_capacity_gi = {
      pz-server    = 12
      zomboid      = 12
      steam        = 1
      panel        = 1
      panel-logs   = 1
      caddy-data   = 0.125
      caddy-config = 0.125
    }
  }
  expect_failures = [var.volume_capacity_gi]
}

run "local_node_api_policies" {
  command = plan
  variables {
    node_name = "updspace-home"
    node_ipv4 = "192.168.1.176"
  }
  assert {
    condition = alltrue([for policy in [kubernetes_network_policy_v1.panel_telemetry_api, kubernetes_network_policy_v1.panel_updater] :
      length([for rule in policy.spec[0].egress : rule if rule.to[0].ip_block[0].cidr == "192.168.1.176/32" && rule.ports[0].port == "6443"]) == 1 &&
      alltrue([for rule in policy.spec[0].egress : rule.to[0].ip_block[0].cidr != "10.130.0.30/32"])
    ])
    error_message = "Local API access must target only the local node on port 6443, not the cloud node."
  }
}

run "shared_local_edge" {
  command = plan
  variables {
    caddy_config_path = "../local-edge/Caddyfile"
    edge_storage_root = "/srv/edge-caddy"
  }
  assert {
    condition     = strcontains(kubernetes_config_map_v1.caddy.data.Caddyfile, "panel.zomboid.svc.cluster.local:3001") && strcontains(kubernetes_config_map_v1.caddy.data.Caddyfile, "uptime-kuma.uptime-kuma.svc.cluster.local:3001")
    error_message = "The shared local edge must retain both host routes."
  }
  assert {
    condition     = kubernetes_persistent_volume_v1.data["caddy-data"].spec[0].persistent_volume_source[0].local[0].path == "/srv/edge-caddy/caddy-data" && kubernetes_persistent_volume_v1.data["zomboid"].spec[0].persistent_volume_source[0].local[0].path == "${var.storage_root}/zomboid"
    error_message = "Shared edge storage must be independent without changing the game path."
  }
}

run "preserve_local_monitoring_password_gate" {
  command = plan
  variables {
    node_name                   = "updspace-home"
    monitoring_auth_secret_name = "observability-edge-auth"
  }
  assert {
    condition = (
      kubernetes_deployment_v1.caddy.spec[0].template[0].spec[0].container[0].env[0].name == "MONITORING_AUTH_HASH" &&
      kubernetes_deployment_v1.caddy.spec[0].template[0].spec[0].container[0].env[0].value_from[0].secret_key_ref[0].name == "observability-edge-auth" &&
      kubernetes_deployment_v1.caddy.spec[0].template[0].spec[0].container[0].env[0].value_from[0].secret_key_ref[0].key == "password-hash"
    )
    error_message = "Terraform must preserve the shared edge authentication secret without storing its value."
  }
}

run "preserve_closed_id_trial_tls" {
  command = plan
  variables {
    id_origin_tls_secret_name = "id-origin-tls"
  }
  assert {
    condition = (
      length([for v in kubernetes_deployment_v1.caddy.spec[0].template[0].spec[0].volume : v if v.name == "id-origin-tls" && try(v.secret[0].secret_name, "") == "id-origin-tls"]) == 1 &&
      length([for v in kubernetes_deployment_v1.caddy.spec[0].template[0].spec[0].container[0].volume_mount : v if v.name == "id-origin-tls" && v.mount_path == "/etc/id-origin-tls" && v.read_only]) == 1
    )
    error_message = "The adopted edge must retain the private ID TLS Secret mount."
  }
}
