# Read model definitions from config.yaml
locals {
  config = yamldecode(file(var.config_path))
  models = { for m in local.config.models : m.name => m }
  worker_service_names = {
    for name, model in local.models :
    name => (
      length("ae-w-${replace(name, ".", "-")}") < 50
      ? "ae-w-${replace(name, ".", "-")}"
      : "ae-w-${substr(replace(name, ".", "-"), 0, 37)}-${substr(md5(name), 0, 6)}"
    )
  }
}

resource "google_project_service" "required" {
  for_each = toset([
    "cloudkms.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --- Service account for internal communication ---

resource "google_service_account" "gateway" {
  account_id   = "any-embedding-gateway"
  display_name = "any-embedding gateway"
}

resource "google_service_account" "worker" {
  account_id   = "any-embedding-worker"
  display_name = "any-embedding worker"
}

# --- Encryption (CMEK) -------------------------------------------------------

resource "google_kms_key_ring" "any_embedding" {
  name     = "any-embedding"
  location = var.region

  depends_on = [google_project_service.required["cloudkms.googleapis.com"]]
}

resource "google_kms_crypto_key" "any_embedding" {
  name            = "any-embedding-key"
  key_ring        = google_kms_key_ring.any_embedding.id
  rotation_period = "7776000s" # 90 days
  purpose         = "ENCRYPT_DECRYPT"
}

resource "google_project_service_identity" "secretmanager" {
  provider = google-beta
  project  = var.project_id
  service  = "secretmanager.googleapis.com"

  depends_on = [google_project_service.required["secretmanager.googleapis.com"]]
}

# Secret Manager needs permission to use the key
resource "google_kms_crypto_key_iam_member" "secret_manager_encrypt" {
  crypto_key_id = google_kms_crypto_key.any_embedding.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-secretmanager.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.secretmanager]
}

# GCS service agent needs permission to use the key for audit log bucket
resource "google_kms_crypto_key_iam_member" "gcs_encrypt" {
  crypto_key_id = google_kms_crypto_key.any_embedding.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current.number}@gs-project-accounts.iam.gserviceaccount.com"
}

data "google_project" "current" {
  project_id = var.project_id

  depends_on = [google_project_service.required]
}

# --- Secrets -----------------------------------------------------------------

resource "google_secret_manager_secret" "api_key" {
  secret_id = "any-embedding-api-key"
  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.any_embedding.id
        }
      }
    }
  }
  depends_on = [
    google_project_service.required["secretmanager.googleapis.com"],
    google_kms_crypto_key_iam_member.secret_manager_encrypt,
  ]
}

resource "google_secret_manager_secret_version" "api_key" {
  secret      = google_secret_manager_secret.api_key.id
  secret_data = var.api_key
}

resource "google_secret_manager_secret" "hf_token" {
  count     = var.hf_token != "" ? 1 : 0
  secret_id = "any-embedding-hf-token"
  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.any_embedding.id
        }
      }
    }
  }
  depends_on = [
    google_project_service.required["secretmanager.googleapis.com"],
    google_kms_crypto_key_iam_member.secret_manager_encrypt,
  ]
}

resource "google_secret_manager_secret_version" "hf_token" {
  count       = var.hf_token != "" ? 1 : 0
  secret      = google_secret_manager_secret.hf_token[0].id
  secret_data = var.hf_token
}

resource "google_secret_manager_secret_iam_member" "gateway_reads_api_key" {
  secret_id = google_secret_manager_secret.api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_reads_hf_token" {
  count     = var.hf_token != "" ? 1 : 0
  secret_id = google_secret_manager_secret.hf_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# --- Worker services (one per model, each with its own baked-in image) ---

resource "google_cloud_run_v2_service" "worker" {
  provider = google-beta
  for_each = local.models

  name         = local.worker_service_names[each.key]
  location     = var.region
  ingress      = "INGRESS_TRAFFIC_ALL"
  launch_stage = try(each.value.gpu, false) ? "BETA" : "GA"

  deletion_protection = var.cloud_run_deletion_protection

  depends_on = [
    google_project_service.required["run.googleapis.com"],
    google_secret_manager_secret_version.hf_token,
    google_secret_manager_secret_iam_member.worker_reads_hf_token,
  ]

  lifecycle {
    ignore_changes = [
      launch_stage,
      scaling,
    ]
  }

  template {
    service_account               = google_service_account.worker.email
    gpu_zonal_redundancy_disabled = try(each.value.gpu, false)

    scaling {
      min_instance_count = try(each.value.min_instances, var.worker_min_instances)
      max_instance_count = try(each.value.max_instances, var.worker_max_instances)
    }

    # GPU support: attach an NVIDIA L4 when gpu=true in config
    dynamic "node_selector" {
      for_each = try(each.value.gpu, false) ? [1] : []
      content {
        accelerator = "nvidia-l4"
      }
    }

    containers {
      # Each model has its own image with weights baked in:
      # <registry>-<model-name>:latest
      image = "${var.image_registry}-${replace(each.key, ".", "-")}:latest"

      resources {
        limits = merge(
          {
            cpu    = try(each.value.cpu, var.worker_cpu)
            memory = try(each.value.memory, var.worker_memory)
          },
          try(each.value.gpu, false) ? { "nvidia.com/gpu" = "1" } : {}
        )
        # GPU workers: instance-based billing (CPU always allocated, required for GPU)
        # CPU-only workers: request-based billing (CPU only during requests → scale-to-zero saves)
        cpu_idle          = !try(each.value.gpu, false)
        startup_cpu_boost = true
      }

      env {
        name  = "MODEL_NAME"
        value = each.value.model
      }
      env {
        name  = "MODEL_TYPE"
        value = try(each.value.type, "text")
      }
      env {
        name  = "HF_HOME"
        value = "/tmp/hf-home"
      }
      env {
        name  = "HF_HUB_OFFLINE"
        value = "0"
      }
      env {
        name  = "TRANSFORMERS_OFFLINE"
        value = "0"
      }

      dynamic "env" {
        for_each = var.hf_token != "" ? [1] : []
        content {
          name = "HF_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.hf_token[0].secret_id
              version = "latest"
            }
          }
        }
      }

      dynamic "env" {
        for_each = var.hf_token != "" ? [1] : []
        content {
          name = "HUGGINGFACE_HUB_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.hf_token[0].secret_id
              version = "latest"
            }
          }
        }
      }

      dynamic "env" {
        for_each = var.hf_token != "" ? [1] : []
        content {
          name = "HUGGING_FACE_HUB_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.hf_token[0].secret_id
              version = "latest"
            }
          }
        }
      }

      ports {
        container_port = 8080
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 24
        timeout_seconds       = 3
      }
    }

    timeout = "300s"
  }
}

# Allow the gateway service account to invoke worker services
resource "google_cloud_run_v2_service_iam_member" "gateway_invokes_worker" {
  for_each = local.models

  name     = google_cloud_run_v2_service.worker[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.gateway.email}"
}

# --- Gateway service ---

resource "google_cloud_run_v2_service" "gateway" {
  name     = "any-embedding-gateway"
  location = var.region

  deletion_protection = var.cloud_run_deletion_protection

  lifecycle {
    ignore_changes = [scaling]
  }

  depends_on = [google_project_service.required["run.googleapis.com"]]

  template {
    service_account = google_service_account.gateway.email

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = var.gateway_image

      resources {
        limits = {
          cpu    = var.gateway_cpu
          memory = var.gateway_memory
        }
        cpu_idle = true
      }

      env {
        name = "API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.api_key.secret_id
            version = "latest"
          }
        }
      }

      # Inject worker URLs as environment variables: WORKER_URL_<SANITIZED_NAME>
      dynamic "env" {
        for_each = local.models
        content {
          name  = "WORKER_URL_${replace(replace(upper(env.key), "-", "_"), ".", "_")}"
          value = google_cloud_run_v2_service.worker[env.key].uri
        }
      }

      ports {
        container_port = 8080
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 2
        period_seconds        = 3
        failure_threshold     = 5
        timeout_seconds       = 2
      }
    }
  }
}

# Make the gateway publicly accessible (clients authenticate via API key)
resource "google_cloud_run_v2_service_iam_member" "gateway_public" {
  name     = google_cloud_run_v2_service.gateway.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Cloud Audit Logs (data access) -----------------------------------------

resource "google_project_iam_audit_config" "cloud_run" {
  project = var.project_id
  service = "run.googleapis.com"

  depends_on = [google_project_service.required["logging.googleapis.com"]]

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# --- Log retention (GCS sink for long-term audit trail) ----------------------

resource "google_storage_bucket" "audit_logs" {
  name                        = "${var.project_id}-any-embedding-audit-logs"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  encryption {
    default_kms_key_name = google_kms_crypto_key.any_embedding.id
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = var.log_retention_days
    }
  }

  depends_on = [google_kms_crypto_key_iam_member.gcs_encrypt]
}

resource "google_logging_project_sink" "audit_sink" {
  name        = "any-embedding-audit-sink"
  destination = "storage.googleapis.com/${google_storage_bucket.audit_logs.name}"

  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name=~"^(any-embedding-gateway|ae-w-)"
  EOT

  unique_writer_identity = true

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_project_service_identity" "logging" {
  provider = google-beta
  project  = var.project_id
  service  = "logging.googleapis.com"

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_storage_bucket_iam_member" "audit_sink_writer" {
  bucket = google_storage_bucket.audit_logs.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.audit_sink.writer_identity

  depends_on = [
    google_logging_project_sink.audit_sink,
    google_project_service_identity.logging,
  ]
}

# --- Monitoring: uptime check + alerting ------------------------------------

resource "google_monitoring_uptime_check_config" "gateway" {
  display_name = "any-embedding-gateway-health"
  timeout      = "10s"
  period       = "60s"

  depends_on = [google_project_service.required["monitoring.googleapis.com"]]

  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = replace(google_cloud_run_v2_service.gateway.uri, "https://", "")
    }
  }
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email != "" ? 1 : 0
  display_name = "any-embedding-alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }

  depends_on = [google_project_service.required["monitoring.googleapis.com"]]
}

resource "google_monitoring_alert_policy" "gateway_uptime" {
  count        = var.alert_email != "" ? 1 : 0
  display_name = "any-embedding: gateway down"
  combiner     = "OR"

  depends_on = [google_project_service.required["monitoring.googleapis.com"]]

  conditions {
    display_name = "Gateway health check failing"
    condition_threshold {
      filter          = "resource.type = \"uptime_url\" AND metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id = \"${google_monitoring_uptime_check_config.gateway.uptime_check_id}\""
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.project_id"]
      }
    }
  }

  notification_channels = [
    google_monitoring_notification_channel.email[0].id,
  ]
}

resource "google_monitoring_alert_policy" "error_rate" {
  count        = var.alert_email != "" ? 1 : 0
  display_name = "any-embedding: high error rate"
  combiner     = "OR"

  depends_on = [google_project_service.required["monitoring.googleapis.com"]]

  conditions {
    display_name = "Gateway 5xx error rate > 5%"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"any-embedding-gateway\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [
    google_monitoring_notification_channel.email[0].id,
  ]
}
