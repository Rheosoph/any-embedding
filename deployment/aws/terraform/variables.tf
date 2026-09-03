variable "aws_region" {
  description = "AWS Region for the stack. Lambda MicroVM images and their S3 build artifacts are Region-bound."
  type        = string
  default     = "eu-west-1"

  validation {
    condition     = var.aws_region == "eu-west-1"
    error_message = "This deployment is pinned to eu-west-1 because its MicroVM images and S3 artifacts are Region-bound."
  }
}

variable "name_prefix" {
  description = "Prefix used for all named resources."
  type        = string
  default     = "any-embedding"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,31}$", var.name_prefix))
    error_message = "name_prefix must be 2-32 lowercase letters, numbers, or hyphens and start with a letter or number."
  }
}

variable "environment" {
  description = "Short environment suffix used in resource names and tags."
  type        = string
  default     = "ireland"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,15}$", var.environment))
    error_message = "environment must be 1-16 lowercase letters, numbers, or hyphens."
  }
}

variable "gateway_lambda_artifact_path" {
  description = "Path to the bundled gateway Lambda zip. Null resolves to deployment/aws/terraform/build/gateway.zip."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.gateway_lambda_artifact_path == null ? true : trimspace(var.gateway_lambda_artifact_path) != ""
    error_message = "gateway_lambda_artifact_path must be null or a non-empty path."
  }
}

variable "lifecycle_lambda_artifact_path" {
  description = "Path to the bundled lifecycle Lambda zip. Null resolves to deployment/aws/terraform/build/lifecycle.zip."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.lifecycle_lambda_artifact_path == null ? true : trimspace(var.lifecycle_lambda_artifact_path) != ""
    error_message = "lifecycle_lambda_artifact_path must be null or a non-empty path."
  }
}

variable "lambda_artifact_path" {
  description = "Deprecated compatibility path for a shared Lambda zip. Set neither new artifact variable when using it; migrate to gateway_lambda_artifact_path and lifecycle_lambda_artifact_path."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.lambda_artifact_path == null ? true : trimspace(var.lambda_artifact_path) != ""
    error_message = "lambda_artifact_path must be null or a non-empty path."
  }
}

variable "api_key_sha256" {
  description = "Lowercase SHA-256 digest of the API key enforced by the public Function URL handler."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.api_key_sha256))
    error_message = "api_key_sha256 must be a 64-character lowercase hexadecimal SHA-256 digest."
  }
}

variable "gateway_memory_mb" {
  description = "Memory for the small routing Lambda."
  type        = number
  default     = 256

  validation {
    condition     = var.gateway_memory_mb >= 128 && var.gateway_memory_mb <= 10240
    error_message = "gateway_memory_mb must be between 128 and 10240."
  }
}

variable "gateway_timeout_seconds" {
  description = "Gateway timeout. It includes MicroVM launch/resume and inference time."
  type        = number
  default     = 900

  validation {
    condition     = var.gateway_timeout_seconds >= 1 && var.gateway_timeout_seconds <= 900
    error_message = "gateway_timeout_seconds must be between 1 and Lambda's 900-second limit."
  }
}

variable "gateway_reserved_concurrency" {
  description = "Reserved concurrency for the Function URL gateway; each execution owns at most one MicroVM lease."
  type        = number
  default     = 50

  validation {
    condition     = var.gateway_reserved_concurrency >= 1
    error_message = "gateway_reserved_concurrency must be at least 1."
  }
}

variable "lifecycle_memory_mb" {
  description = "Memory for TTL cleanup and reconciliation Lambda invocations."
  type        = number
  default     = 256

  validation {
    condition     = var.lifecycle_memory_mb >= 128 && var.lifecycle_memory_mb <= 10240
    error_message = "lifecycle_memory_mb must be between 128 and 10240."
  }
}

variable "lifecycle_timeout_seconds" {
  description = "Timeout for a lifecycle/reconciliation invocation."
  type        = number
  default     = 120

  validation {
    condition     = var.lifecycle_timeout_seconds >= 1 && var.lifecycle_timeout_seconds <= 900
    error_message = "lifecycle_timeout_seconds must be between 1 and 900."
  }
}

variable "lifecycle_reserved_concurrency" {
  description = "Reserved concurrency for lifecycle work. Keep this below the regional TerminateMicrovm TPS quota."
  type        = number
  default     = 2

  validation {
    condition     = var.lifecycle_reserved_concurrency >= 1 && var.lifecycle_reserved_concurrency <= 10
    error_message = "lifecycle_reserved_concurrency must be between 1 and 10."
  }
}

variable "log_retention_days" {
  description = "Retention for gateway, lifecycle, and MicroVM runtime logs."
  type        = number
  default     = 30
}

variable "microvm_max_duration_seconds" {
  description = "Hard lifetime for every MicroVM, including running and suspended time. Seven hours leaves headroom below the 8-hour service maximum."
  type        = number
  default     = 25200

  validation {
    condition     = var.microvm_max_duration_seconds >= 1 && var.microvm_max_duration_seconds <= 28800
    error_message = "microvm_max_duration_seconds must be between 1 and 28800."
  }
}

variable "soft_ttl_seconds" {
  description = "Logical inactivity expiry written to ttl_at. DynamoDB physical TTL deletion is only a delayed fallback."
  type        = number
  default     = 1800

  validation {
    condition     = var.soft_ttl_seconds >= 60
    error_message = "soft_ttl_seconds must be at least 60."
  }
}

variable "lease_seconds" {
  description = "Maximum occupancy window for a request-owned BUSY lease; expiry lets the reconciler recover timed-out requests."
  type        = number
  default     = 900

  validation {
    condition     = var.lease_seconds >= 30 && var.lease_seconds <= 900
    error_message = "lease_seconds must be between 30 and 900."
  }
}

variable "launch_lock_seconds" {
  description = "Expiry for a per-pool launch lock. RunMicrovm also uses an idempotent client token."
  type        = number
  default     = 180

  validation {
    condition     = var.launch_lock_seconds >= 30 && var.launch_lock_seconds <= 900
    error_message = "launch_lock_seconds must be between 30 and 900."
  }
}

variable "token_minutes" {
  description = "MicroVM endpoint JWE lifetime. Sixty minutes is the service maximum."
  type        = number
  default     = 60

  validation {
    condition     = var.token_minutes >= 1 && var.token_minutes <= 60
    error_message = "token_minutes must be between 1 and 60."
  }
}

variable "token_refresh_minutes" {
  description = "Refresh cached endpoint tokens this many minutes after issue, before token expiry."
  type        = number
  default     = 55

  validation {
    condition     = var.token_refresh_minutes >= 1 && var.token_refresh_minutes <= 59
    error_message = "token_refresh_minutes must be between 1 and 59."
  }
}

variable "hard_expiry_headroom_seconds" {
  description = "Do not lease a MicroVM this close to its hard expiry; launch a replacement instead."
  type        = number
  default     = 900

  validation {
    condition     = var.hard_expiry_headroom_seconds >= 60 && var.hard_expiry_headroom_seconds < 28800
    error_message = "hard_expiry_headroom_seconds must be between 60 and 28799."
  }
}

variable "microvm_port" {
  description = "Worker HTTP port allowed by the MicroVM auth token."
  type        = number
  default     = 8080

  validation {
    condition     = var.microvm_port >= 1 && var.microvm_port <= 65535
    error_message = "microvm_port must be between 1 and 65535."
  }
}

variable "microvm_ready_timeout_seconds" {
  description = "Maximum time the gateway waits for a launched MicroVM endpoint to become authoritative-ready."
  type        = number
  default     = 120

  validation {
    condition     = var.microvm_ready_timeout_seconds >= 10 && var.microvm_ready_timeout_seconds <= 300
    error_message = "microvm_ready_timeout_seconds must be between 10 and 300."
  }
}

variable "orphan_grace_seconds" {
  description = "Age before the reconciler may terminate an unregistered MicroVM owned by this stack's execution role."
  type        = number
  default     = 600

  validation {
    condition     = var.orphan_grace_seconds >= 300 && var.orphan_grace_seconds <= 3600
    error_message = "orphan_grace_seconds must be between 300 and 3600."
  }
}

variable "ingress_network_connector_arn" {
  description = "Override for the Lambda-managed MicroVM ingress connector. Null uses the regional ALL_INGRESS connector."
  type        = string
  default     = null
  nullable    = true
}

variable "egress_network_connector_arn" {
  description = "Optional MicroVM VPC egress connector ARN. Null explicitly selects Lambda's INTERNET_EGRESS service default; an empty list does not disable egress."
  type        = string
  default     = null
  nullable    = true
}

variable "enable_point_in_time_recovery" {
  description = "Enable DynamoDB point-in-time recovery."
  type        = bool
  default     = false
}

variable "function_url_allowed_origins" {
  description = "CORS origins for the Function URL. The default permits browser clients; API-key validation still occurs in the handler when configured."
  type        = list(string)
  default     = ["*"]
}

variable "alarm_actions" {
  description = "SNS topic ARNs notified by CloudWatch alarms. Empty creates alarms without notifications."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags merged into every supported resource."
  type        = map(string)
  default     = {}
}

variable "models" {
  description = "Model API metadata and ordered MicroVM routing tiers. Image resources are referenced, not created by this stack."
  type = map(object({
    model_id   = string
    type       = string
    dimensions = number
    max_tokens = number
    order      = number
    tiers = map(object({
      order               = number
      image_arn           = string
      image_version       = string
      baseline_memory_mib = number
      max_instances       = number
      max_item_tokens     = number
      max_total_tokens    = number
      max_batch_items     = number
      max_attention_score = number
      max_response_bytes  = number
      capacity            = number
      baseline_vcpu       = number
      peak_memory_mib     = number
      peak_vcpu           = number
      torch_threads       = number
      idle_seconds        = number
      suspended_seconds   = number
    }))
  }))

  # An empty map selects the account-neutral built-in catalog in locals.tf.
  # Supply a non-empty map to replace that catalog completely.
  default = {}

  validation {
    condition = length(var.models) == 0 || alltrue([
      for model_name, model in var.models :
      length(trimspace(model_name)) > 0 &&
      length(trimspace(model.model_id)) > 0 &&
      length(trimspace(model.type)) > 0 &&
      model.dimensions > 0 && model.dimensions == floor(model.dimensions) &&
      model.max_tokens > 0 && model.max_tokens == floor(model.max_tokens) &&
      model.order > 0 && model.order == floor(model.order) &&
      length(model.tiers) > 0 &&
      alltrue([
        for tier_name, tier in model.tiers :
        length(trimspace(tier_name)) > 0 &&
        can(regex("^arn:aws[a-z-]*:lambda:[a-z0-9-]+:[0-9]{12}:microvm-image:[A-Za-z0-9_-]+$", tier.image_arn)) &&
        length(trimspace(tier.image_version)) > 0 &&
        tier.order > 0 && tier.order == floor(tier.order) &&
        contains([512, 1024, 2048, 4096, 8192], tier.baseline_memory_mib) &&
        tier.max_instances > 0 && tier.max_instances == floor(tier.max_instances) &&
        tier.max_item_tokens > 0 && tier.max_item_tokens == floor(tier.max_item_tokens) &&
        tier.max_total_tokens > 0 && tier.max_total_tokens == floor(tier.max_total_tokens) &&
        tier.max_batch_items > 0 && tier.max_batch_items == floor(tier.max_batch_items) &&
        tier.max_attention_score > 0 && tier.max_attention_score == floor(tier.max_attention_score) &&
        tier.max_response_bytes > 0 && tier.max_response_bytes == floor(tier.max_response_bytes) &&
        tier.capacity > 0 && tier.capacity == floor(tier.capacity) &&
        tier.baseline_vcpu > 0 &&
        tier.peak_memory_mib > 0 && tier.peak_memory_mib == floor(tier.peak_memory_mib) &&
        tier.peak_vcpu > 0 &&
        tier.torch_threads > 0 && tier.torch_threads == floor(tier.torch_threads) &&
        tier.idle_seconds >= 60 && tier.idle_seconds == floor(tier.idle_seconds) &&
        tier.suspended_seconds >= 0 && tier.suspended_seconds == floor(tier.suspended_seconds) &&
        tier.max_item_tokens <= model.max_tokens
      ])
    ])
    error_message = "models may be empty to use the built-in catalog; otherwise every model needs at least one tier, names and image versions must be non-empty, dimensions/token/count/size limits/capacity/memory/thread counts/ordering must be positive integers, CPU values must be positive, durations must be whole seconds, baseline memory must be supported, and max_item_tokens cannot exceed model max_tokens."
  }
}
