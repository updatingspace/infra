locals {
  dashboard_files = fileset("${path.module}/../observability/dashboards", "*.json")
}

# Transitional adapter for existing custom-project dashboards. The native Yandex
# resource cannot round-trip multi_source_chart widgets yet. No destroy provisioner.
# UI drift is checked separately by scripts/dashboard.py check.
resource "terraform_data" "dashboard" {
  for_each = local.dashboard_files
  triggers_replace = [
    filesha256("${path.module}/../observability/dashboards/${each.value}"),
    filesha256("${path.module}/../scripts/dashboard.py")
  ]
  provisioner "local-exec" {
    command = "python3 \"$PZ_DASHBOARD_SCRIPT\" apply \"$PZ_DASHBOARD_FILE\""
    environment = {
      PZ_DASHBOARD_SCRIPT = abspath("${path.module}/../scripts/dashboard.py")
      PZ_DASHBOARD_FILE   = abspath("${path.module}/../observability/dashboards/${each.value}")
    }
  }
}
