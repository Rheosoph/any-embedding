# This file is intentionally not loaded by Terraform. The main stack references
# existing images and never creates a MicroVM image implicitly.
#
# To opt in, copy this file to ../managed-images.tf, upload a build artifact to
# S3, fill every placeholder, and review the plan separately from the runtime
# stack. The module path below is evaluated after that copy.

terraform {
  required_providers {
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.90.0"
    }
  }
}

provider "awscc" {
  region = var.aws_region
}

module "managed_microvm_images" {
  source = "./modules/microvm-image-awscc"

  images = {
    # example = {
    #   name               = "any-embedding-example"
    #   description        = "Explicitly managed embedding worker image"
    #   code_artifact_uri  = "s3://REPLACE_ME/source.zip"
    #   base_image_arn     = "arn:aws:lambda:eu-west-1:aws:microvm-image:REPLACE_ME"
    #   base_image_version = "REPLACE_ME"
    #   build_role_arn     = "arn:aws:iam::REPLACE_ME:role/REPLACE_ME"
    #   minimum_memory_mib = 2048
    #   hooks = {
    #     port = 8080
    #     microvm_image_hooks = {
    #       ready = "ENABLED"
    #     }
    #   }
    #   logging = {
    #     cloudwatch = {
    #       log_group  = "/aws/lambda/microvms/REPLACE_ME"
    #       log_stream = "image-build"
    #     }
    #   }
    # }
  }

  tags = local.common_tags
}

output "explicitly_managed_microvm_images" {
  value = module.managed_microvm_images.images
}
