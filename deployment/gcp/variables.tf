variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run services"
  type        = string
  default     = "us-central1"
}

variable "api_key" {
  description = "Preshared API key for gateway authentication"
  type        = string
  sensitive   = true
}

variable "gateway_image" {
  description = "Container image for the gateway (e.g. gcr.io/PROJECT/any-embedding-gateway:latest)"
  type        = string
}

variable "image_registry" {
  description = "Container registry prefix for worker images (e.g. gcr.io/PROJECT/any-embedding-worker). Each model image is <registry>-<model-name>:latest"
  type        = string
}

variable "config_path" {
  description = "Path to config.yaml (relative to deployment/gcp dir)"
  type        = string
  default     = "../../config.yaml"
}

variable "worker_cpu" {
  description = "Default CPU allocation per worker (e.g. '2' or '4')"
  type        = string
  default     = "2"
}

variable "worker_memory" {
  description = "Default memory allocation per worker (e.g. '4Gi', '8Gi')"
  type        = string
  default     = "4Gi"
}

variable "worker_max_instances" {
  description = "Default max instances per worker service"
  type        = number
  default     = 3
}

variable "worker_min_instances" {
  description = "Default min instances per worker (0 = scale to zero)"
  type        = number
  default     = 0
}

variable "hf_token" {
  description = "HuggingFace token for gated model access (e.g. google/embeddinggemma-300m)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "gateway_cpu" {
  description = "CPU allocation for the gateway"
  type        = string
  default     = "1"
}

variable "gateway_memory" {
  description = "Memory allocation for the gateway"
  type        = string
  default     = "512Mi"
}

variable "cloud_run_deletion_protection" {
  description = "Whether Cloud Run services should be protected from Terraform destroy operations"
  type        = bool
  default     = false
}

variable "tfstate_bucket" {
  description = "GCS bucket name for Terraform remote state"
  type        = string
}

variable "alert_email" {
  description = "Email address for monitoring alert notifications"
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "Number of days to retain audit logs in GCS (TISAX/SOC 2: 90-365)"
  type        = number
  default     = 365
}
