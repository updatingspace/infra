terraform {
  required_version = ">= 1.7, < 2.0"
}

# Run this root on the VM, in a separate directory/state, after the owner has
# enlarged the cloud disk and the operator has enlarged /dev/vda1. No cloud API.
resource "terraform_data" "zomboid_50g" {
  input = {
    image                 = "/var/lib/pz-volumes/zomboid.ext4"
    filesystem_uuid       = "0bd0ee81-1d96-447e-868a-405226663cf6"
    target_gib            = 50
    required_disk_gib     = 100
    minimum_host_free_gib = 8
    script_sha256         = filesha256("${path.module}/grow.py")
  }
  triggers_replace = [filesha256("${path.module}/grow.py")]

  provisioner "local-exec" {
    command = "python3 \"$GROW_SCRIPT\" --apply"
    environment = {
      GROW_SCRIPT = abspath("${path.module}/grow.py")
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

output "zomboid_filesystem" {
  value = terraform_data.zomboid_50g.input
}
