locals {
  stack_name = "${var.name_prefix}-${var.environment}"

  # The default catalog is derived from the active AWS identity and Region so
  # repository source never embeds an account-specific image ARN.
  default_models = tomap({
    gte-multilingual-base = {
      model_id   = "Alibaba-NLP/gte-multilingual-base"
      type       = "text"
      dimensions = 768
      max_tokens = 8192
      order      = 1
      tiers = tomap({
        small = {
          order               = 1
          image_arn           = "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:microvm-image:any-embedding-gte-multilingual-base"
          image_version       = "1.0"
          baseline_memory_mib = 2048
          max_instances       = 2
          max_item_tokens     = 512
          max_total_tokens    = 4096
          max_batch_items     = 32
          max_attention_score = 4194304
          max_response_bytes  = 6000000
          capacity            = 2
          baseline_vcpu       = 1
          peak_memory_mib     = 8192
          peak_vcpu           = 4
          torch_threads       = 4
          idle_seconds        = 485
          suspended_seconds   = 1315
        }
        medium = {
          order               = 2
          image_arn           = "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:microvm-image:any-embedding-gte-multilingual-base-4g"
          image_version       = "1.0"
          baseline_memory_mib = 4096
          max_instances       = 2
          max_item_tokens     = 4096
          max_total_tokens    = 32768
          max_batch_items     = 128
          max_attention_score = 134217728
          max_response_bytes  = 32000000
          capacity            = 4
          baseline_vcpu       = 2
          peak_memory_mib     = 16384
          peak_vcpu           = 8
          torch_threads       = 8
          idle_seconds        = 255
          suspended_seconds   = 1545
        }
        large = {
          order               = 3
          image_arn           = "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:microvm-image:any-embedding-gte-multilingual-base-8g"
          image_version       = "1.0"
          baseline_memory_mib = 8192
          max_instances       = 2
          max_item_tokens     = 8192
          max_total_tokens    = 262144
          max_batch_items     = 2048
          max_attention_score = 17179869184
          max_response_bytes  = 200000000
          capacity            = 4
          baseline_vcpu       = 4
          peak_memory_mib     = 32768
          peak_vcpu           = 16
          torch_threads       = 16
          idle_seconds        = 145
          suspended_seconds   = 1655
        }
      })
    }
  })

  models = length(var.models) == 0 ? local.default_models : var.models

  gateway_lambda_artifact_path = (
    var.gateway_lambda_artifact_path != null
    ? abspath(var.gateway_lambda_artifact_path)
    : var.lambda_artifact_path != null
    ? abspath(var.lambda_artifact_path)
    : "${path.module}/build/gateway.zip"
  )
  lifecycle_lambda_artifact_path = (
    var.lifecycle_lambda_artifact_path != null
    ? abspath(var.lifecycle_lambda_artifact_path)
    : var.lambda_artifact_path != null
    ? abspath(var.lambda_artifact_path)
    : "${path.module}/build/lifecycle.zip"
  )

  common_tags = merge({
    Application = "any-embedding"
    Environment = var.environment
    ManagedBy   = "terraform"
    Provider    = "aws"
  }, var.tags)

  ingress_network_connector_arn = coalesce(
    var.ingress_network_connector_arn,
    "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:aws:network-connector:aws-network-connector:ALL_INGRESS"
  )

  egress_network_connector_arn = coalesce(
    var.egress_network_connector_arn,
    "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
  )

  tier_records = merge([
    for model_name, model in local.models : {
      for tier_name, tier in model.tiers : "${model_name}/${tier_name}" => merge(tier, {
        model = model_name
        name  = tier_name
      })
    }
  ]...)

  ordered_model_tiers = {
    for model_name, model in local.models : model_name => [
      for tier_sort_key in sort([
        for tier_name, tier in model.tiers : format("%09d|%s", tier.order, tier_name)
        ]) : merge(
        model.tiers[split("|", tier_sort_key)[1]],
        { name = split("|", tier_sort_key)[1] }
      )
    ]
  }

  image_arns = distinct([
    for tier in values(local.tier_records) : tier.image_arn
  ])

  lambda_environment_common = {
    STATE_TABLE_NAME              = aws_dynamodb_table.state.name
    MICROVM_MAX_DURATION_SECONDS  = tostring(var.microvm_max_duration_seconds)
    MICROVM_EXECUTION_ROLE_ARN    = aws_iam_role.microvm_execution.arn
    MICROVM_LOG_GROUP             = aws_cloudwatch_log_group.microvm.name
    MICROVM_PORT                  = tostring(var.microvm_port)
    MICROVM_READY_TIMEOUT_SECONDS = tostring(var.microvm_ready_timeout_seconds)
    ORPHAN_GRACE_SECONDS          = tostring(var.orphan_grace_seconds)
    MICROVM_INGRESS_CONNECTOR_ARN = local.ingress_network_connector_arn
    MICROVM_EGRESS_CONNECTOR_ARN  = local.egress_network_connector_arn
    SOFT_TTL_SECONDS              = tostring(var.soft_ttl_seconds)
    LEASE_SECONDS                 = tostring(var.lease_seconds)
    LAUNCH_LOCK_SECONDS           = tostring(var.launch_lock_seconds)
    TOKEN_MINUTES                 = tostring(var.token_minutes)
    TOKEN_REFRESH_MINUTES         = tostring(var.token_refresh_minutes)
    HARD_EXPIRY_HEADROOM_SECONDS  = tostring(var.hard_expiry_headroom_seconds)
  }
}
