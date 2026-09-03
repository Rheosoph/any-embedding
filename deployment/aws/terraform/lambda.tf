resource "aws_lambda_function" "gateway" {
  function_name = "${local.stack_name}-gateway"
  description   = "OpenAI-compatible embedding gateway and Lambda MicroVM pool router."
  role          = aws_iam_role.gateway.arn
  runtime       = "nodejs24.x"
  architectures = ["arm64"]
  handler       = "gateway/index.handler"

  filename         = local.gateway_lambda_artifact_path
  source_code_hash = try(filebase64sha256(local.gateway_lambda_artifact_path), null)
  publish          = true

  memory_size                    = var.gateway_memory_mb
  timeout                        = var.gateway_timeout_seconds
  reserved_concurrent_executions = var.gateway_reserved_concurrency

  environment {
    variables = merge(local.lambda_environment_common, {
      API_KEY_SHA256 = var.api_key_sha256
    })
  }

  depends_on = [
    aws_cloudwatch_log_group.gateway,
    aws_iam_role_policy.gateway,
  ]

  lifecycle {
    precondition {
      condition     = fileexists(local.gateway_lambda_artifact_path)
      error_message = "Gateway artifact not found. Run 'mise run package:aws' or set gateway_lambda_artifact_path to an existing zip."
    }

    precondition {
      condition = (
        var.lambda_artifact_path != null ||
        local.gateway_lambda_artifact_path != local.lifecycle_lambda_artifact_path
      )
      error_message = "Gateway and lifecycle artifact paths must differ unless the deprecated shared lambda_artifact_path is used."
    }

    precondition {
      condition = (
        var.lambda_artifact_path == null || (
          var.gateway_lambda_artifact_path == null &&
          var.lifecycle_lambda_artifact_path == null
        )
      )
      error_message = "Deprecated lambda_artifact_path cannot be combined with gateway_lambda_artifact_path or lifecycle_lambda_artifact_path."
    }

    precondition {
      condition     = var.token_refresh_minutes < var.token_minutes
      error_message = "token_refresh_minutes must be lower than token_minutes."
    }

    precondition {
      condition     = var.hard_expiry_headroom_seconds < var.microvm_max_duration_seconds
      error_message = "hard_expiry_headroom_seconds must be lower than microvm_max_duration_seconds."
    }

    precondition {
      condition = alltrue([
        for tier in values(local.tier_records) :
        startswith(tier.image_arn, "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:microvm-image:")
      ])
      error_message = "Every configured MicroVM image must belong to the selected AWS account and Region."
    }


    precondition {
      condition     = var.hard_expiry_headroom_seconds >= var.lease_seconds
      error_message = "hard_expiry_headroom_seconds must be at least lease_seconds so an accepted lease cannot outlive a worker."
    }

    precondition {
      condition = alltrue([
        for tier in values(local.tier_records) :
        tier.idle_seconds + tier.suspended_seconds == var.soft_ttl_seconds
      ])
      error_message = "Every tier's idle_seconds + suspended_seconds must equal soft_ttl_seconds."
    }
  }
}

resource "aws_lambda_alias" "gateway_live" {
  name             = "live"
  description      = "Stable Function URL target for the current published gateway version."
  function_name    = aws_lambda_function.gateway.function_name
  function_version = aws_lambda_function.gateway.version
}

resource "aws_lambda_function_url" "gateway" {
  function_name      = aws_lambda_function.gateway.function_name
  qualifier          = aws_lambda_alias.gateway_live.name
  authorization_type = "NONE"
  invoke_mode        = "RESPONSE_STREAM"

  cors {
    allow_credentials = false
    allow_headers = [
      "authorization",
      "content-type",
    ]
    allow_methods = [
      "GET",
      "POST",
    ]
    allow_origins = var.function_url_allowed_origins
    expose_headers = [
      "content-type",
      "x-request-id",
    ]
    max_age = 86400
  }
}

# New Function URLs require both statements. AuthType NONE makes the URL public
# at Lambda's resource-policy layer; API_KEY_SHA256 is enforced by the handler.
resource "aws_lambda_permission" "public_function_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.gateway.function_name
  qualifier              = aws_lambda_alias.gateway_live.name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "public_invoke_via_function_url" {
  statement_id             = "AllowPublicInvokeViaFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.gateway.function_name
  qualifier                = aws_lambda_alias.gateway_live.name
  principal                = "*"
  invoked_via_function_url = true
}

resource "aws_lambda_function" "lifecycle" {
  function_name = "${local.stack_name}-lifecycle"
  description   = "Idempotent DynamoDB TTL cleanup and five-minute registered-worker reconciler."
  role          = aws_iam_role.lifecycle.arn
  runtime       = "nodejs24.x"
  architectures = ["arm64"]
  handler       = "lifecycle/index.handler"

  filename         = local.lifecycle_lambda_artifact_path
  source_code_hash = try(filebase64sha256(local.lifecycle_lambda_artifact_path), null)

  memory_size                    = var.lifecycle_memory_mb
  timeout                        = var.lifecycle_timeout_seconds
  reserved_concurrent_executions = var.lifecycle_reserved_concurrency

  environment {
    variables = local.lambda_environment_common
  }

  depends_on = [
    aws_cloudwatch_log_group.lifecycle,
    aws_iam_role_policy.lifecycle,
  ]

  lifecycle {
    precondition {
      condition     = fileexists(local.lifecycle_lambda_artifact_path)
      error_message = "Lifecycle artifact not found. Run 'mise run package:aws' or set lifecycle_lambda_artifact_path to an existing zip."
    }
  }
}
