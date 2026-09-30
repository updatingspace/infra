terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "0.228.0"
    }
  }

  # Initialize with a private, absolute state path; see README. Local backend
  # locking is effective only when every operator uses the same host/path.
  backend "local" {}
}

provider "yandex" {
  folder_id = var.folder_id
  zone      = var.zone
}
