# Optional AWSCC MicroVM image module

The standard HashiCorp AWS provider does not currently expose a Lambda MicroVM image resource. AWS CloudFormation added `AWS::Lambda::MicrovmImage`, and the AWS Cloud Control provider exposes it as `awscc_lambda_microvm_image`.

This module is deliberately disconnected from the runtime stack and defaults to `images = {}`. Nothing is built unless an operator copies [`../../examples/managed-images.tf`](../../examples/managed-images.tf) to `../../managed-images.tf`, supplies a Lambda-managed base-image ARN/version, an S3 build artifact, and a build role, then reviews and applies that plan. The created ARN/version must subsequently be placed in the runtime `models` map.

Enabling the example adds `hashicorp/awscc >= 1.90.0`. After copying the example into the root module, explicitly upgrade and regenerate the provider lock selections, validate them, and review the complete lockfile diff:

```bash
terraform -chdir=deployment/aws/terraform init -upgrade
terraform -chdir=deployment/aws/terraform providers lock \
  -platform=darwin_arm64 \
  -platform=linux_amd64 \
  -platform=linux_arm64
terraform -chdir=deployment/aws/terraform validate
git diff -- deployment/aws/terraform/.terraform.lock.hcl
```

Commit the reviewed root lockfile before returning to the deployment wrapper's `-lockfile=readonly` workflow. `init -upgrade` may also change the selected AWS provider within its configured constraint, so treat every provider selection in that diff as part of the review.

AWSCC's schema currently marks all image configuration fields required, including empty collections, hooks, and logging. The module supplies them explicitly. `additional_os_capabilities` defaults to an empty set; use `ALL` only when the worker genuinely needs elevated Linux capabilities. See the official [`AWS::Lambda::MicrovmImage` schema](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-lambda-microvmimage.html) and [MicroVM image guidance](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html).

Image creation is asynchronous. Do not route production traffic to an output merely because Terraform created the resource: wait until the requested version is `ACTIVE`, pin that exact version in `models`, and deploy the runtime configuration in a separate reviewed change. Image storage has a one-week minimum retention charge.
