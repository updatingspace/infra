terraform {
  required_version = ">= 1.7, < 2.0"
}

resource "terraform_data" "game_updater" {
  triggers_replace = {
    for name in ["controller.py", "install.py", "pz-game-update.service", "pz-game-update.timer", "../backup/metrics.py"] :
    name => filesha256("${path.module}/${name}")
  }
  provisioner "local-exec" {
    command     = "python3 install.py"
    working_dir = path.module
  }
}
