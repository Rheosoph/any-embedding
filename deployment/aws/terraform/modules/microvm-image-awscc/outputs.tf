output "images" {
  description = "Created image ARNs and their latest active versions."
  value = {
    for key, image in awscc_lambda_microvm_image.this : key => {
      image_arn                   = image.image_arn
      latest_active_image_version = image.latest_active_image_version
      state                       = image.state
    }
  }
}
