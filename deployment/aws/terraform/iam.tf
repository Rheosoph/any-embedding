data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "microvm_assume" {
  statement {
    effect = "Allow"
    actions = [
      "sts:AssumeRole",
      "sts:TagSession",
    ]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${local.stack_name}-gateway"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "lifecycle" {
  name               = "${local.stack_name}-lifecycle"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "microvm_execution" {
  name               = "${local.stack_name}-microvm-runtime"
  assume_role_policy = data.aws_iam_policy_document.microvm_assume.json
  description        = "Runtime role passed only to any-embedding Lambda MicroVMs."
}

data "aws_iam_policy_document" "gateway" {
  statement {
    sid = "WriteGatewayLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.gateway.arn}:*"]
  }

  statement {
    sid = "ReadPoolConfiguration"
    actions = [
      "dynamodb:GetItem",
    ]
    resources = [aws_dynamodb_table.state.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "MODEL#*",
        "POOL#*",
      ]
    }
  }

  statement {
    sid = "LeasePoolRows"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:TransactWriteItems",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.state.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values   = ["POOL#*"]
    }
  }

  # The model registry is intentionally in the same table and listModels uses a
  # strongly consistent Scan. Scan cannot be restricted with LeadingKeys.
  statement {
    sid       = "ListModelConfiguration"
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.state.arn]
  }

  statement {
    sid = "OperateConfiguredMicrovmImages"
    actions = [
      "lambda:CreateMicrovmAuthToken",
      "lambda:GetMicrovm",
      "lambda:RunMicrovm",
      "lambda:TerminateMicrovm",
    ]
    resources = local.image_arns
  }

  statement {
    sid       = "PassLambdaManagedNetworkConnectors"
    actions   = ["lambda:PassNetworkConnector"]
    resources = ["*"]
  }

  statement {
    sid       = "PassOnlyTheMicrovmRuntimeRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.microvm_execution.arn]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "${local.stack_name}-gateway"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

data "aws_iam_policy_document" "lifecycle" {
  statement {
    sid = "WriteLifecycleLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lifecycle.arn}:*"]
  }

  statement {
    sid = "ReadDynamoDbStream"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
    ]
    resources = [aws_dynamodb_table.state.stream_arn]
  }

  statement {
    sid       = "ListDynamoDbStreams"
    actions   = ["dynamodb:ListStreams"]
    resources = ["*"]
  }

  statement {
    sid = "ScanStateRows"
    actions = [
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.state.arn]
  }

  statement {
    sid = "ReconcilePoolStateRows"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:TransactWriteItems",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.state.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values   = ["POOL#*"]
    }
  }

  statement {
    sid = "InspectAndTerminateConfiguredMicrovms"
    actions = [
      "lambda:GetMicrovm",
      "lambda:TerminateMicrovm",
    ]
    resources = local.image_arns
  }

  statement {
    sid       = "ListMicrovmsForOrphanReconciliation"
    actions   = ["lambda:ListMicrovms"]
    resources = ["*"]
  }

  statement {
    sid       = "SendFailedLifecycleBatches"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.lifecycle_dlq.arn]
  }
}

resource "aws_iam_role_policy" "lifecycle" {
  name   = "${local.stack_name}-lifecycle"
  role   = aws_iam_role.lifecycle.id
  policy = data.aws_iam_policy_document.lifecycle.json
}

data "aws_iam_policy_document" "microvm_execution" {
  statement {
    sid = "WriteMicrovmRuntimeLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.microvm.arn}:*"]
  }
}

resource "aws_iam_role_policy" "microvm_execution" {
  name   = "${local.stack_name}-microvm-runtime-logs"
  role   = aws_iam_role.microvm_execution.id
  policy = data.aws_iam_policy_document.microvm_execution.json
}
