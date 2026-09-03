resource "aws_cloudwatch_metric_alarm" "gateway_errors" {
  alarm_name          = "${local.stack_name}-gateway-errors"
  alarm_description   = "The public embedding gateway returned an unhandled Lambda error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.gateway.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "gateway_throttles" {
  alarm_name          = "${local.stack_name}-gateway-throttles"
  alarm_description   = "Gateway reserved concurrency was exhausted; Function URL callers receive 429 responses."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.gateway.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "function_url_5xx" {
  alarm_name          = "${local.stack_name}-function-url-5xx"
  alarm_description   = "The public Function URL returned an application or platform 5xx response."
  namespace           = "AWS/Lambda"
  metric_name         = "Url5xxCount"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.gateway.function_name
    Resource     = "${aws_lambda_function.gateway.function_name}:${aws_lambda_alias.gateway_live.name}"
  }
}

resource "aws_cloudwatch_metric_alarm" "function_url_latency" {
  alarm_name          = "${local.stack_name}-function-url-p95-latency"
  alarm_description   = "The public Function URL p95 exceeded two minutes."
  namespace           = "AWS/Lambda"
  metric_name         = "UrlRequestLatency"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 120000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.gateway.function_name
    Resource     = "${aws_lambda_function.gateway.function_name}:${aws_lambda_alias.gateway_live.name}"
  }
}

resource "aws_cloudwatch_metric_alarm" "lifecycle_errors" {
  alarm_name          = "${local.stack_name}-lifecycle-errors"
  alarm_description   = "TTL cleanup or the periodic MicroVM reconciler failed."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.lifecycle.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lifecycle_dlq_visible" {
  alarm_name          = "${local.stack_name}-lifecycle-dlq-not-empty"
  alarm_description   = "A DynamoDB stream batch or Scheduler delivery exhausted retries and needs inspection."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    QueueName = aws_sqs_queue.lifecycle_dlq.name
  }
}

resource "aws_cloudwatch_metric_alarm" "dynamodb_throttles" {
  alarm_name          = "${local.stack_name}-dynamodb-throttles"
  alarm_description   = "The on-demand state table throttled a pool routing or lease operation."
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  metric_query {
    id          = "throttles"
    expression  = "read_throttles + write_throttles"
    label       = "State table throttle events"
    return_data = true
  }

  metric_query {
    id          = "read_throttles"
    return_data = false

    metric {
      metric_name = "ReadThrottleEvents"
      namespace   = "AWS/DynamoDB"
      period      = 300
      stat        = "Sum"
      unit        = "Count"

      dimensions = {
        TableName = aws_dynamodb_table.state.name
      }
    }
  }

  metric_query {
    id          = "write_throttles"
    return_data = false

    metric {
      metric_name = "WriteThrottleEvents"
      namespace   = "AWS/DynamoDB"
      period      = 300
      stat        = "Sum"
      unit        = "Count"

      dimensions = {
        TableName = aws_dynamodb_table.state.name
      }
    }
  }
}
