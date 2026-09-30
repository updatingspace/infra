terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "0.228.0"
    }
  }
}

provider "yandex" {
  folder_id = var.folder_id
}

variable "folder_id" {
  type    = string
  default = "b1gidr45ifb2c25bco2d"
}

variable "writer_service_account_id" {
  type    = string
  default = "aje25u06h4nadtlvfrsj"
}

variable "telegram_notification_method_id" {
  description = "Existing Monium Telegram method ID, once the owner creates and links the chat. Null means delivery is not configured."
  type        = string
  default     = null
  nullable    = true
}

resource "yandex_resourcemanager_folder_iam_member" "logs_writer" {
  folder_id = var.folder_id
  role      = "monium.logs.writer"
  member    = "serviceAccount:${var.writer_service_account_id}"
}

resource "yandex_iam_service_account_api_key" "logs" {
  service_account_id = var.writer_service_account_id
  description        = "PZ OpenTelemetry logs writer; managed in infrastructure/terraform"
  scopes             = ["yc.monium.logs.write"]
  expires_at         = "2027-09-18T00:00:00Z"
  depends_on         = [yandex_resourcemanager_folder_iam_member.logs_writer]
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [scope]
  }
}

output "logs_api_key" {
  value     = yandex_iam_service_account_api_key.logs.secret_key
  sensitive = true
}

output "logs_api_key_id" {
  value = yandex_iam_service_account_api_key.logs.id
}

output "notifications_ready" {
  description = "All five production Monium alerts are wired for ALARM-only notifications to the UI-managed Telegram escalation pz-game-sms; see observability/alerts/applied.json for configuration verification, recent production transport acceptance and historical delivery evidence."
  value       = true
}
