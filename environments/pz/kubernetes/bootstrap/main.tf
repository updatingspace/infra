terraform {
  required_version = ">= 1.7, < 2.0"
}

variable "ssh_target" {
  type    = string
  default = "matveevmihail@51.250.40.253"
}

variable "ssh_key" {
  type    = string
  default = "~/.ssh/matveevmihail_yt"
}

locals {
  version          = "v1.36.4+k3s1"
  installer_sha256 = "46177d4c99440b4c0311b67233823a8e8a2fc09693f6c89af1a7161e152fbfad"
  config = yamlencode({
    "node-name"             = "compute-vm-2-6-60-ssd-1785610759198"
    "node-ip"               = "10.130.0.30"
    "advertise-address"     = "10.130.0.30"
    "bind-address"          = "10.130.0.30"
    "tls-san"               = ["127.0.0.1", "10.130.0.30"]
    "write-kubeconfig-mode" = "0600"
    "secrets-encryption"    = true
    "disable"               = ["traefik", "local-storage"]
    "cluster-cidr"          = "10.42.0.0/16"
    "service-cidr"          = "10.43.0.0/16"
    # Single-node routing does not need a public VXLAN listener.
    "flannel-backend" = "host-gw"
    "kubelet-arg" = [
      "address=10.130.0.30",
      "system-reserved=cpu=250m,memory=768Mi,ephemeral-storage=2Gi",
      "kube-reserved=cpu=500m,memory=1792Mi,ephemeral-storage=3Gi",
      "eviction-hard=memory.available<512Mi,nodefs.available<10%,imagefs.available<10%,nodefs.inodesFree<5%,imagefs.inodesFree<5%",
      "container-log-max-size=10Mi", "container-log-max-files=3",
      "pod-max-pids=2048"
    ]
  })
}

# Explicit bootstrap adapter. No destroy provisioner: removing Terraform state
# cannot uninstall Kubernetes or delete worlds. Drift is checked by the script
# on a requested replacement, not discovered by terraform_data refresh.
resource "terraform_data" "k3s" {
  triggers_replace = [local.version, local.installer_sha256, sha256(local.config), filesha256("${path.module}/bootstrap.py")]
  input            = { version = local.version, config_sha256 = sha256(local.config), target = var.ssh_target }
  provisioner "local-exec" {
    command = "python3 \"$BOOTSTRAP_SCRIPT\""
    environment = {
      BOOTSTRAP_SCRIPT = abspath("${path.module}/bootstrap.py")
      SSH_TARGET       = var.ssh_target
      SSH_KEY          = pathexpand(var.ssh_key)
      K3S_VERSION      = local.version
      INSTALLER_SHA256 = local.installer_sha256
      K3S_CONFIG_B64   = base64encode(local.config)
    }
  }
}

output "cluster" {
  value = { version = local.version, address = "https://10.130.0.30:6443", kubeconfig_on_server = "/etc/rancher/k3s/operator.yaml" }
}
