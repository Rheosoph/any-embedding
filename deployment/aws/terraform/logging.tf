resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/aws/lambda/${local.stack_name}-gateway"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "lifecycle" {
  name              = "/aws/lambda/${local.stack_name}-lifecycle"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "microvm" {
  name              = "/aws/lambda/microvms/${local.stack_name}"
  retention_in_days = var.log_retention_days
}
