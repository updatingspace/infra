# Yandex public NAT reaches the guest's private IP: binding Kubernetes to that
# IP alone does not restrict public access. Attach only this explicit group;
# adding the default group would combine its permissions with these rules.
resource "yandex_vpc_security_group" "server" {
  name        = "pz-k3s-server"
  description = "Public SSH, web and PZ; Kubernetes API, kubelet and NodePorts stay closed"
  folder_id   = local.folder_id
  network_id  = "enp4te37hlkihro1alb1"

  ingress {
    description    = "SSH administration"
    protocol       = "TCP"
    port           = 22
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description    = "HTTP and ACME"
    protocol       = "TCP"
    port           = 80
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description    = "HTTPS"
    protocol       = "TCP"
    port           = 443
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description    = "HTTP3 QUIC"
    protocol       = "UDP"
    port           = 443
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description    = "Project Zomboid game traffic"
    protocol       = "UDP"
    from_port      = 16261
    to_port        = 16262
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description    = "ICMP diagnostics and path MTU"
    protocol       = "ICMP"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description    = "Outbound IPv4 including DNS, image downloads, Steam and Monium"
    protocol       = "ANY"
    from_port      = 0
    to_port        = 65535
    v4_cidr_blocks = ["0.0.0.0/0"]
  }

  # Current cluster traffic stays inside this single VM. Any future node needs
  # a separately reviewed inter-node rule; no broad self-group allowance yet.
  lifecycle {
    prevent_destroy = true
  }
}
