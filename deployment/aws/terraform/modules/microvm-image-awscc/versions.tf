terraform {
  required_version = ">= 1.5.0"

  required_providers {
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.90.0"
    }
  }
}
