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

variable "worker_images" {
  description = "Per-model immutable image URIs resolved by deploy.py; overrides the registry :latest fallback"
  type        = map(string)
  default     = {}
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

variable "gateway_min_instances" {
  description = "Minimum warm gateway instances (1 avoids scale-from-zero on the routing hop)"
  type        = number
  default     = 0

  validation {
    condition     = var.gateway_min_instances >= 0 && var.gateway_min_instances <= 5 && floor(var.gateway_min_instances) == var.gateway_min_instances
    error_message = "gateway_min_instances must be an integer between 0 and 5."
  }
}

variable "worker_timeout_seconds" {
  description = "Gateway deadline for worker auth and inference; set below the consumer timeout with transport headroom"
  type        = number
  default     = 120

  validation {
    condition     = var.worker_timeout_seconds > 0 && var.worker_timeout_seconds < 300 && floor(var.worker_timeout_seconds) == var.worker_timeout_seconds
    error_message = "worker_timeout_seconds must be an integer between 1 and 299 (workers time out at 300s)."
  }
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
