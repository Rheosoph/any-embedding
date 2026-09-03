resource "aws_dynamodb_table" "state" {
  name         = "${local.stack_name}-microvm-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pool_key"
  range_key    = "record_key"

  deletion_protection_enabled = true
  stream_enabled              = true
  stream_view_type            = "NEW_AND_OLD_IMAGES"

  attribute {
    name = "pool_key"
    type = "S"
  }

  attribute {
    name = "record_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.enable_point_in_time_recovery
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = terraform.workspace == var.environment
      error_message = "Terraform workspace '${terraform.workspace}' must match environment '${var.environment}'. Select the matching workspace before planning."
    }
  }
}

resource "aws_dynamodb_table_item" "model_config" {
  for_each = local.models

  table_name = aws_dynamodb_table.state.name
  hash_key   = aws_dynamodb_table.state.hash_key
  range_key  = aws_dynamodb_table.state.range_key

  item = jsonencode({
    pool_key   = { S = "MODEL#${each.key}" }
    record_key = { S = "CONFIG" }
    entity     = { S = "MODEL" }
    name       = { S = each.key }
    model_id   = { S = each.value.model_id }
    type       = { S = each.value.type }
    dimensions = { N = tostring(each.value.dimensions) }
    max_tokens = { N = tostring(each.value.max_tokens) }
    order      = { N = tostring(each.value.order) }
    tiers = {
      L = [
        for tier in local.ordered_model_tiers[each.key] : {
          M = {
            name                = { S = tier.name }
            order               = { N = tostring(tier.order) }
            max_item_tokens     = { N = tostring(tier.max_item_tokens) }
            max_total_tokens    = { N = tostring(tier.max_total_tokens) }
            max_batch_items     = { N = tostring(tier.max_batch_items) }
            max_attention_score = { N = tostring(tier.max_attention_score) }
            max_response_bytes  = { N = tostring(tier.max_response_bytes) }
          }
        }
      ]
    }
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table_item" "pool_config" {
  for_each = local.tier_records

  table_name = aws_dynamodb_table.state.name
  hash_key   = aws_dynamodb_table.state.hash_key
  range_key  = aws_dynamodb_table.state.range_key

  item = jsonencode({
    pool_key            = { S = "POOL#${each.value.model}#${each.value.name}" }
    record_key          = { S = "CONFIG" }
    entity              = { S = "POOL" }
    model               = { S = each.value.model }
    tier                = { S = each.value.name }
    order               = { N = tostring(each.value.order) }
    image_arn           = { S = each.value.image_arn }
    image_version       = { S = each.value.image_version }
    baseline_memory_mib = { N = tostring(each.value.baseline_memory_mib) }
    max_instances       = { N = tostring(each.value.max_instances) }
    capacity            = { N = tostring(each.value.capacity) }
    idle_seconds        = { N = tostring(each.value.idle_seconds) }
    suspended_seconds   = { N = tostring(each.value.suspended_seconds) }
  })

  lifecycle {
    prevent_destroy = true
  }
}
