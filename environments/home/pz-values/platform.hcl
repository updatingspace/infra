# Budgets for PZ, edge and observability only; not total physical node capacity.
# PVC accounting does not size the existing loopback filesystems.
kubeconfig_path      = "/etc/rancher/k3s/k3s.yaml"
kubeconfig_context   = "default"
pod_security_version = "v1.36"
allocatable = {
  cpu_m     = 4000
  memory_mi = 16000
}

environments = {
  zomboid = {
    quota = {
      cpu_request_m        = 2000
      cpu_limit_m          = 2600
      memory_request_mi    = 8512
      memory_limit_mi      = 11136
      ephemeral_request_mi = 2048
      ephemeral_limit_mi   = 4096
      storage_gi           = 26
      pvc_count            = 8
      pod_count            = 6
    }
    container_defaults = {
      cpu_request_m        = 100
      cpu_limit_m          = 200
      memory_request_mi    = 128
      memory_limit_mi      = 256
      ephemeral_request_mi = 128
      ephemeral_limit_mi   = 512
    }
  }
  edge = {
    quota = {
      cpu_request_m        = 100
      cpu_limit_m          = 200
      memory_request_mi    = 128
      memory_limit_mi      = 256
      ephemeral_request_mi = 128
      ephemeral_limit_mi   = 512
      storage_gi           = 0.25
      pvc_count            = 2
      pod_count            = 2
    }
    container_defaults = {
      cpu_request_m        = 50
      cpu_limit_m          = 100
      memory_request_mi    = 64
      memory_limit_mi      = 128
      ephemeral_request_mi = 64
      ephemeral_limit_mi   = 128
    }
  }
  observability = {
    pod_security_level = "privileged"
    quota = {
      cpu_request_m        = 500
      cpu_limit_m          = 1100
      memory_request_mi    = 1024
      memory_limit_mi      = 3584
      ephemeral_request_mi = 1024
      ephemeral_limit_mi   = 4096
      storage_gi           = 0.5
      pvc_count            = 2
      pod_count            = 8
    }
    container_defaults = {
      cpu_request_m        = 50
      cpu_limit_m          = 100
      memory_request_mi    = 128
      memory_limit_mi      = 256
      ephemeral_request_mi = 128
      ephemeral_limit_mi   = 256
    }
  }
}
