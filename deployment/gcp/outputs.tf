output "gateway_url" {
  description = "Public URL of the gateway service"
  value       = google_cloud_run_v2_service.gateway.uri
}

output "worker_urls" {
  description = "Internal URLs of worker services"
  value = {
    for name, svc in google_cloud_run_v2_service.worker : name => svc.uri
  }
}

output "audit_log_bucket" {
  description = "GCS bucket for long-term audit log retention"
  value       = google_storage_bucket.audit_logs.name
}
