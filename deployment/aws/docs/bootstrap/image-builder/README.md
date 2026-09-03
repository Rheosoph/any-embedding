# MicroVM image-builder bootstrap policy

These IAM documents define the least-privilege image-builder role:

- `trust.json` lets the Lambda service assume the role and tag the session.
- `permissions.example.json` is the safe, account-neutral permissions template.
- `../../../artifacts/bootstrap/image-builder/permissions.json` is the rendered
  local policy. The entire artifacts tree is intentionally ignored because it
  contains the artifact bucket and account-scoped log ARN.

The runtime Terraform stack does not load these files implicitly. Copy the
example into the ignored artifacts path, replace both placeholders, and review
the result before use. Optional image management through Terraform is documented in
[`../../../terraform/modules/microvm-image-awscc`](../../../terraform/modules/microvm-image-awscc/README.md).
