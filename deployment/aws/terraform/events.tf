resource "aws_sqs_queue" "lifecycle_dlq" {
  name                      = "${local.stack_name}-lifecycle-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_lambda_event_source_mapping" "ttl_cleanup" {
  event_source_arn = aws_dynamodb_table.state.stream_arn
  function_name    = aws_lambda_function.lifecycle.arn
  enabled          = true

  starting_position              = "TRIM_HORIZON"
  batch_size                     = 10
  bisect_batch_on_function_error = true
  maximum_record_age_in_seconds  = 3600
  maximum_retry_attempts         = 10
  parallelization_factor         = 1
  function_response_types        = ["ReportBatchItemFailures"]

  filter_criteria {
    filter {
      pattern = jsonencode({
        userIdentity = {
          type        = ["Service"]
          principalId = ["dynamodb.amazonaws.com"]
        }
      })
    }
  }

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.lifecycle_dlq.arn
    }
  }

  depends_on = [aws_iam_role_policy.lifecycle]
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values = [
        "arn:${data.aws_partition.current.partition}:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule-group/${local.stack_name}-lifecycle",
      ]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.stack_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "InvokeLifecycleReconciler"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.lifecycle.arn]
  }

  statement {
    sid       = "SendUndeliverableEventsToDlq"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.lifecycle_dlq.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.stack_name}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule_group" "lifecycle" {
  name = "${local.stack_name}-lifecycle"
}

resource "aws_scheduler_schedule" "reconcile" {
  name        = "${local.stack_name}-reconcile"
  group_name  = aws_scheduler_schedule_group.lifecycle.name
  description = "Repairs expired registered instances, request leases, reservations, and stale launch locks."
  state       = "ENABLED"

  schedule_expression          = "rate(5 minutes)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.lifecycle.arn
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      source = "eventbridge.scheduler"
      action = "reconcile"
    })

    dead_letter_config {
      arn = aws_sqs_queue.lifecycle_dlq.arn
    }

    retry_policy {
      maximum_event_age_in_seconds = 300
      maximum_retry_attempts       = 2
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
