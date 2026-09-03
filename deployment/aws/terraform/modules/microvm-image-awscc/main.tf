resource "awscc_lambda_microvm_image" "this" {
  for_each = var.images

  name               = each.value.name
  description        = each.value.description
  base_image_arn     = each.value.base_image_arn
  base_image_version = each.value.base_image_version
  build_role_arn     = each.value.build_role_arn

  code_artifact = {
    uri = each.value.code_artifact_uri
  }

  cpu_configurations = [{
    architecture = "ARM_64"
  }]

  resources = [{
    minimum_memory_in_mi_b = each.value.minimum_memory_mib
  }]

  egress_network_connectors  = each.value.egress_network_connectors
  additional_os_capabilities = each.value.additional_os_capabilities
  environment_variables = [
    for key, value in each.value.environment_variables : {
      key   = key
      value = value
    }
  ]
  hooks   = each.value.hooks
  logging = each.value.logging

  tags = [
    for key, value in merge(var.tags, each.value.tags) : {
      key   = key
      value = value
    }
  ]
}
