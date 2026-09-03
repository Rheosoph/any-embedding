output "function_url" {
  description = "Public response-streaming endpoint. Authentication is enforced by the application using API_KEY_SHA256."
  value       = aws_lambda_function_url.gateway.function_url
}

output "state_table_name" {
  description = "DynamoDB pool/configuration table."
  value       = aws_dynamodb_table.state.name
}

output "gateway_function_name" {
  value = aws_lambda_function.gateway.function_name
}

output "lifecycle_function_name" {
  value = aws_lambda_function.lifecycle.function_name
}

output "referenced_microvm_images" {
  description = "Existing MicroVM images consumed by this stack; Terraform does not create or delete them."
  value = {
    for key, tier in local.tier_records : key => {
      arn                 = tier.image_arn
      version             = tier.image_version
      baseline_memory_mib = tier.baseline_memory_mib
      idle_seconds        = tier.idle_seconds
      suspended_seconds   = tier.suspended_seconds
    }
  }
}

output "resource_inventory" {
  description = "Auditable list of resources managed or referenced by this deployment. No secrets are included."
  value = {
    account_id = data.aws_caller_identity.current.account_id
    region     = var.aws_region
    stack      = local.stack_name

    managed_by_terraform = {
      lambda_functions = {
        gateway = {
          name  = aws_lambda_function.gateway.function_name
          arn   = aws_lambda_function.gateway.arn
          alias = aws_lambda_alias.gateway_live.arn
          url   = aws_lambda_function_url.gateway.function_url
        }
        lifecycle = {
          name = aws_lambda_function.lifecycle.function_name
          arn  = aws_lambda_function.lifecycle.arn
        }
      }
      dynamodb = {
        name       = aws_dynamodb_table.state.name
        arn        = aws_dynamodb_table.state.arn
        stream_arn = aws_dynamodb_table.state.stream_arn
        ttl_field  = "ttl_at"
      }
      iam_roles = {
        gateway           = aws_iam_role.gateway.arn
        lifecycle         = aws_iam_role.lifecycle.arn
        microvm_execution = aws_iam_role.microvm_execution.arn
        scheduler         = aws_iam_role.scheduler.arn
      }
      log_groups = {
        gateway   = aws_cloudwatch_log_group.gateway.arn
        lifecycle = aws_cloudwatch_log_group.lifecycle.arn
        microvm   = aws_cloudwatch_log_group.microvm.arn
      }
      scheduler = {
        arn        = aws_scheduler_schedule.reconcile.arn
        expression = aws_scheduler_schedule.reconcile.schedule_expression
      }
      sqs = {
        lifecycle_dlq = aws_sqs_queue.lifecycle_dlq.arn
      }
      alarms = {
        gateway_errors     = aws_cloudwatch_metric_alarm.gateway_errors.arn
        gateway_throttles  = aws_cloudwatch_metric_alarm.gateway_throttles.arn
        function_url_5xx   = aws_cloudwatch_metric_alarm.function_url_5xx.arn
        function_url_p95   = aws_cloudwatch_metric_alarm.function_url_latency.arn
        lifecycle_errors   = aws_cloudwatch_metric_alarm.lifecycle_errors.arn
        lifecycle_dlq      = aws_cloudwatch_metric_alarm.lifecycle_dlq_visible.arn
        dynamodb_throttles = aws_cloudwatch_metric_alarm.dynamodb_throttles.arn
      }
      config_items = concat(
        [for model_name in keys(local.models) : "MODEL#${model_name}/CONFIG"],
        [for tier in values(local.tier_records) : "POOL#${tier.model}#${tier.name}/CONFIG"]
      )
    }

    referenced_not_managed = {
      microvm_images = {
        for key, tier in local.tier_records : key => "${tier.image_arn}:${tier.image_version}"
      }
      ingress_network_connector = local.ingress_network_connector_arn
      egress_network_connector  = local.egress_network_connector_arn
    }

    lifecycle_policy = {
      per_tier = {
        for key, tier in local.tier_records : key => {
          idle_seconds      = tier.idle_seconds
          suspended_seconds = tier.suspended_seconds
        }
      }
      hard_max_seconds          = var.microvm_max_duration_seconds
      dynamodb_soft_ttl_seconds = var.soft_ttl_seconds
      reconcile_rate            = "5 minutes"
    }

    deletion_guards = {
      dynamodb_deletion_protection = aws_dynamodb_table.state.deletion_protection_enabled
      terraform_prevent_destroy    = true
    }
  }
}
