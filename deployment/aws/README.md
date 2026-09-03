# AWS Lambda MicroVM deployment

This directory deploys a small ARM64 Node.js 24 Lambda as an OpenAI-compatible, response-streaming Function URL in `eu-west-1` (Ireland). The control plane is written in TypeScript and bundled for Lambda with esbuild. The gateway selects an embedding tier, leases a worker in DynamoDB, and talks to that worker's dedicated Lambda MicroVM endpoint with a port-scoped JWE. A second Lambda consumes TTL deletions and runs a five-minute reconciliation pass.

By default, the stack derives three Ireland `gte-multilingual-base` image ARNs from the active AWS account and references immutable version `1.0`. It does **not** create, update, or delete those images. Optional image-as-code support is isolated in [`terraform/modules/microvm-image-awscc`](terraform/modules/microvm-image-awscc/README.md) and has an empty default. Each immutable MicroVM image version has a one-week minimum storage charge; see the [cost model](docs/costs.md) and [matched AWS/GCP benchmark](docs/benchmarks.md).

After deployment, retrieve the endpoint locally with `terraform -chdir=deployment/aws/terraform output -raw function_url`. Function URLs and resource inventories are deliberately excluded from source control.

## Directory layout

| Directory | Purpose |
|---|---|
| [`terraform/`](terraform) | One root module, its reusable image module, and variable example |
| [`scripts/`](scripts) | Packaging, planning, deployment, publishing, and inventory entry points |
| [`benchmarks/`](benchmarks) | Function URL and direct-MicroVM harnesses; raw results are ignored |
| [`docs/`](docs) | Sanitized benchmark/cost reports and account-neutral bootstrap templates |
| `artifacts/` | Ignored current plans, MicroVM packages, and generated inventories |

Lambda archives remain in the ignored `terraform/build/` directory because the AWS provider treats a changed local `filename` as a code deployment even when the bytes are identical. Keeping that path stable lets this source reorganization produce a true no-op Terraform plan.

## Architecture

```text
client
  -> Lambda Function URL (AuthType NONE, RESPONSE_STREAM, alias live)
  -> gateway Lambda (API-key hash check, model routing)
  -> DynamoDB conditional lease / per-pool launch lock
  -> one dedicated Lambda MicroVM endpoint (X-aws-proxy-auth)
  -> streamed OpenAI-compatible response

DynamoDB TTL service deletion -> filtered Stream -> lifecycle Lambda -> idempotent terminate
EventBridge Scheduler / 5 min  -> lifecycle Lambda -> repair stale registered rows
failed stream/schedule delivery -> encrypted SQS DLQ -> CloudWatch alarm
```

Lambda MicroVMs are a separate service surface from ordinary Lambda functions: the SDK client is `@aws-sdk/client-lambda-microvms`, CLI commands are under `aws lambda-microvms`, every VM has its own endpoint, and there is no native load balancer across those endpoints. See [Running and using MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html).

### Lifecycle policy

The default native idle policy is tier-specific:

| Tier | `maxIdleDurationSeconds` | `suspendedDurationSeconds` | Estimated Ireland cost crossover |
|---|---:|---:|---:|
| small | 485 | 1,315 | 475 s |
| medium | 255 | 1,545 | 250 s |
| large | 145 | 1,655 | 138 s |

Every tier retains the same 1,800-second total inactivity window. `maximumDurationInSeconds = 25200` terminates a worker after seven hours regardless of activity, and `autoResumeEnabled = true` holds an endpoint request while a suspended worker resumes. The crossover uses each tier's baseline rate and observed image-build size proxy, so it must be revalidated against billed checkpoint sizes before production tuning.

The historical Frankfurt small-tier resume test exposed a latency tradeoff that the cost crossover alone cannot capture: the resume hook completed in roughly one second, but the first short encode then spent 11,976 ms faulting the model checkpoint back into active memory, producing 14,728 ms client wall time. Later calls returned toward the warm range. Collect an Ireland resume distribution before setting an interactive SLO; if the behavior repeats, either lengthen the warm window or terminate/relaunch small workers instead of resuming them.

`ttl_at` is a logical 30-minute soft expiry and a cleanup fallback. DynamoDB may physically remove an expired item only days later, so TTL is never used as a timer. Hot-path reads and conditional claims reject logically expired rows. The native MicroVM idle policy is the primary suspend/terminate mechanism; the five-minute reconciler repairs stale state. See [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html) and [expired-item behavior](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ttl-expired-items.html).

Runtime egress currently uses Lambda's managed `INTERNET_EGRESS` connector. Live `GetMicrovm` validation showed that both omission and an empty `egressNetworkConnectors` list select this service default, so the gateway now sends and inventories it explicitly. Set `egress_network_connector_arn` to a Lambda Core VPC connector when private/controlled egress is required; no VPC connector or NAT gateway is created by this test stack.

Endpoint JWE tokens are port-scoped to 8080, issued for the service maximum of 60 minutes, cached with the registered instance row, and refreshed after 55 minutes or on an authentication failure. Never include the token in logs or responses. Set shorter token/refresh variables if the additional token churn is preferable to the longer credential lifetime.

### Concurrency and race safety

The table uses `pool_key` and `record_key` with strongly consistent base-table reads and DynamoDB transactions:

- A model row is `MODEL#<model> / CONFIG`.
- A pool configuration row is `POOL#<model>#<tier> / CONFIG`.
- Instance, lease, and launch-reservation rows share the tier partition.
- A claim transaction increments `in_flight` only when the instance is ready, unexpired, below `capacity`, and has enough hard-lifetime headroom; it also creates the request-owned lease row.
- Release deletes only the caller's lease and decrements the matching worker. An expired lease is recoverable, so a timed-out gateway cannot leave a permanent `BUSY` flag.
- A per-pool launch lock has an owner and expiry. The reservation is written before `RunMicrovm`, and the run call uses a stable `clientToken` to close retry duplication windows.

Per-VM lease capacity is 2 for small and 4 for medium/large, with `max_instances = 2` for every tier. The corresponding routed ceilings are 4, 8, and 8 in-flight requests per tier. These are admission and queue-safety limits, not claimed parallel model throughput. The deployed version-1.0 worker coalesces compatible requests over a 4 ms window and serializes model calls. In the matched Ireland run, four independent short requests averaged 5.34 requests/second and four independent 2K requests averaged 0.512 requests/second; client batch 32 reached 41.0 embeddings/second. See the [benchmark report](docs/benchmarks.md).

EventBridge Scheduler and DynamoDB Streams both deliver at least once. `TerminateMicrovm` and lifecycle row transitions therefore must remain idempotent. The stream mapping enables partial batch failures, bounded retries, batch bisection, and an SQS on-failure destination. It filters specifically for TTL service deletions using `userIdentity.type=Service` and `principalId=dynamodb.amazonaws.com`, as documented in [DynamoDB TTL Streams](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/time-to-live-ttl-streams.html).

## DynamoDB configuration contract

Terraform materializes these document shapes using DynamoDB's native number and list types.

```json
{
  "pool_key": "MODEL#gte-multilingual-base",
  "record_key": "CONFIG",
  "entity": "MODEL",
  "name": "gte-multilingual-base",
  "model_id": "Alibaba-NLP/gte-multilingual-base",
  "type": "text",
  "dimensions": 768,
  "max_tokens": 8192,
  "order": 1,
  "tiers": [
    {
      "name": "small",
      "order": 1,
      "max_item_tokens": 512,
      "max_total_tokens": 4096,
      "max_batch_items": 32,
      "max_attention_score": 4194304,
      "max_response_bytes": 6000000
    }
  ]
}
```

```json
{
  "pool_key": "POOL#gte-multilingual-base#small",
  "record_key": "CONFIG",
  "entity": "POOL",
  "model": "gte-multilingual-base",
  "tier": "small",
  "order": 1,
  "image_arn": "arn:aws:lambda:eu-west-1:<AWS_ACCOUNT_ID>:microvm-image:any-embedding-gte-multilingual-base",
  "image_version": "1.0",
  "baseline_memory_mib": 2048,
  "max_instances": 2,
  "capacity": 2,
  "idle_seconds": 485,
  "suspended_seconds": 1315
}
```

The default routing limits are operational guardrails, not model-format limits. They send short/small batches to 2 GiB, intermediate work to 4 GiB, and long or large batches to 8 GiB. The measured direct 512/2,048/4,096/8,192-token and batch results are tabulated in the [cost model](docs/costs.md). Tune thresholds with end-to-end p95 latency, actual burst usage, and billed checkpoint sizes. `max_attention_score` is the sum of squared estimated token counts, which better represents transformer cost than total tokens alone.

## Function URL behavior and limits

The Function URL is deliberately `AuthType = NONE` so existing clients can use a normal bearer API key. Since October 2025, a new public Function URL needs **both** resource-policy statements:

- `lambda:InvokeFunctionUrl` conditioned on `lambda:FunctionUrlAuthType = NONE`.
- `lambda:InvokeFunction` conditioned on `lambda:InvokedViaFunctionUrl = true`.

Both are present in [`terraform/lambda.tf`](terraform/lambda.tf); see [Function URL access control](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html). This makes the AWS edge endpoint public. The handler requires `API_KEY_SHA256` for `/v1/models` and `/v1/embeddings`; `/health` stays public. Use a high-entropy API key because an offline guess can be checked against its hash.

The client surface matches the existing provider: `GET /health`, `GET /v1/models`, and `POST /v1/embeddings`, with the same model name, request/response shape, and either `Authorization: Bearer <key>` or the existing raw Authorization value. A client can switch providers by replacing only its base URL.

Response streaming raises the response ceiling to 200 MB, with the first 6 MB uncapped and the remainder limited to 2 MB/s. It does **not** raise the synchronous request-body limit of 6 MB. Very large input batches must therefore be split client-side even when their response would fit. AWS charges $0.008/GiB written to the response stream after the first 6 MiB per request; the Lambda free tier currently also includes 100 GB/month of response streaming. Normal data-transfer charges still apply. Function execution has a hard 900-second timeout, and a client disconnect does not automatically stop or stop billing the invocation. See [Lambda response streaming](https://docs.aws.amazon.com/lambda/latest/dg/config-rs-invoke-furls.html), [Lambda pricing](https://aws.amazon.com/lambda/pricing/), and [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html).

Reserved concurrency is 50 for the gateway, so the Function URL accepts at most 500 requests/second at Lambda's 10x reserved-concurrency URL limit and returns 429 when all executions are occupied. In practice, the configured pool size and the regional `RunMicrovm` quota bind first. The gateway's in-memory per-IP rate limit is only a best-effort abuse guard because Lambda execution environments do not share memory.

## Source and build layout

The Lambda control-plane source lives under [`app/aws`](../../app/aws):

- `contract.ts` and `routing.ts` contain the shared HTTP contract and tier selection.
- `gateway/*.ts` contains the Function URL handler, MicroVM client, and DynamoDB pool registry.
- `lifecycle/index.ts` contains stream recovery and scheduled reconciliation.
- [`tests/aws`](../../tests/aws) contains the TypeScript unit tests.

Install from the committed npm lock and run the complete quality gate from the repository root:

```bash
npm --prefix app/aws ci --ignore-scripts
npm --prefix app/aws run check
```

`npm run check` type-checks the strict TypeScript project, runs the AWS tests, and builds the bundles. Its build step uses esbuild to write `app/aws/dist/gateway/index.js` and `app/aws/dist/lifecycle/index.js`. The packaging script runs the complete check itself, so a package cannot be produced from unchecked source.

## Deploy

Requirements:

- Terraform 1.5 or newer.
- AWS provider 6.x or newer.
- AWS Cloud Control provider 1.90 or newer only when the optional image-as-code example is enabled.
- A current native AWS CLI authenticated to the target account.
- Node.js 24, npm, and `zip` for the Lambda build.
- `jq` for binding a saved Terraform plan to its exact Lambda artifacts.
- `uv` for validating the MicroVM worker lock before packaging an image source artifact.
- IAM permission to create the Terraform resources and pass the generated roles.

From the repository root, prepare variables and build the two Lambda artifacts:

```bash
install -m 600 deployment/aws/terraform/terraform.tfvars.example \
  deployment/aws/terraform/terraform.tfvars
terraform -chdir=deployment/aws/terraform workspace select ireland
source deployment/aws/scripts/lib/common.sh
printf %s 'a-long-random-api-key' | sha256_files
# Put the resulting lowercase digest in terraform.tfvars or TF_VAR_api_key_sha256.
./deployment/aws/scripts/package-lambdas.sh
```

The live deployment uses the separate `ireland` Terraform workspace; create it once with `terraform -chdir=deployment/aws/terraform workspace new ireland` if it is absent. `package-lambdas.sh` installs from `app/aws/package-lock.json`, runs `npm run check`, and invokes esbuild. It creates two deterministic archives with no TypeScript source or `node_modules` tree:

Terraform enforces that the selected workspace exactly matches `environment`; a plan from `default` or any other mismatched workspace fails before infrastructure changes are proposed.

| Artifact | Archive entry | Lambda handler | Terraform variable |
|---|---|---|---|
| `terraform/build/gateway.zip` | `gateway/index.js` | `gateway/index.handler` | `gateway_lambda_artifact_path` |
| `terraform/build/lifecycle.zip` | `lifecycle/index.js` | `lifecycle/index.handler` | `lifecycle_lambda_artifact_path` |

Review and deploy an exact saved plan:

```bash
./deployment/aws/scripts/deploy.sh validate
./deployment/aws/scripts/deploy.sh plan
./deployment/aws/scripts/deploy.sh apply
```

`deploy.sh plan` rebuilds both archives before creating the saved plan, then writes a SHA-256 manifest covering the plan and every Lambda archive referenced by it. `apply` refuses legacy plans without that manifest, rejects any plan or artifact changed after review, and consumes the approval manifest so the same plan cannot be applied twice. Plans and local state are created with owner-only permissions, and no script performs an implicit apply.

No backend is imposed for this test stack. Configure an encrypted, locked remote backend before production use, but migrate it as a separate operation: back up the current state, add the backend configuration, and run `terraform -chdir=deployment/aws/terraform init -migrate-state`. The deployment wrapper deliberately uses ordinary `init`; it will not silently reconfigure or migrate a backend. Confirm the remote state contains the same lineage and all addresses before running a new plan.

The Terraform variable `lambda_artifact_path` remains only as a compatibility bridge for callers coordinating their own direct Terraform migration. The managed `deploy.sh plan` workflow deliberately refuses a shared artifact and requires two distinct paths. Existing automation must migrate to `gateway_lambda_artifact_path` and `lifecycle_lambda_artifact_path` before using the managed plan/apply workflow.

### Build and publish a MicroVM image revision

The Python worker under [`app/aws/microvm`](../../app/aws/microvm) is an independent uv project. Its `pyproject.toml` declares the benchmarked model stack, and its committed `uv.lock` fixes the complete dependency graph for Python 3.12. Torch is sourced explicitly from PyTorch's CPU index so the ARM64 worker lock cannot acquire CUDA packages. The Docker build uses a pinned uv image and installs with `uv sync --frozen`; it does not consume the repository-root Python environment.

Package, upload, and submit a new immutable image build in that order. From the repository root:

```bash
./deployment/aws/scripts/package-microvm.sh
source deployment/aws/scripts/lib/common.sh
artifact_sha="$(sha256_files deployment/aws/artifacts/microvm/source.zip | cut -d ' ' -f 1)"
content_id="${artifact_sha:0:12}"
release_id="gte_${content_id}"
artifact_uri="s3://BUCKET/gte-multilingual-base/${content_id}.zip"

aws s3 cp \
  deployment/aws/artifacts/microvm/source.zip \
  "${artifact_uri}" \
  --region eu-west-1
./deployment/aws/scripts/publish-microvm-images.sh "${artifact_uri}" "${release_id}"
```

Replace `BUCKET` with the image-build artifact bucket and use a fresh, content-derived key for every release. `package-microvm.sh` first checks that `uv.lock` agrees with `pyproject.toml`, then packages the Dockerfile, Python sources, manifest, and lock. `release_id` must be 1-32 letters, numbers, underscores, or hyphens; a short artifact SHA is a useful choice.

`publish-microvm-images.sh` submits builds for the 2, 4, and 8 GiB images. It neither waits for them to become `ACTIVE` nor changes the runtime stack. Before routing traffic to a new image, confirm every build succeeded, record the immutable versions, update the corresponding `models` entries in Terraform, and review a new saved plan. Ireland is the default; use the script's explicit `--region`, `--account-id`, and `--build-role-name` options for another target and review the image names before publishing.

The optional AWSCC image module requires provider version 1.90 or newer. If enabling [`terraform/examples/managed-images.tf`](terraform/examples/managed-images.tf), follow the module's explicit [`init -upgrade`, multi-platform lock, validation, and lockfile-review workflow](terraform/modules/microvm-image-awscc/README.md) before using the deployment wrapper again.

After apply:

```bash
endpoint="$(terraform -chdir=deployment/aws/terraform output -raw function_url)"
curl -fsS "${endpoint}health"
curl -fsS "${endpoint}v1/models" \
  -H "Authorization: Bearer ${API_KEY}"
curl -fsS "${endpoint}v1/embeddings" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H 'Content-Type: application/json' \
  --data '{"model":"gte-multilingual-base","input":["hello","Grüße aus Ireland"]}'
```

Do not put the plaintext key in a tracked tfvars file or shell history. The digest is marked sensitive in Terraform but is necessarily present in state and the Lambda environment, so protect both.

## Inventory and operations

`terraform -chdir=deployment/aws/terraform output -json resource_inventory` lists every managed function, table, role, log group, schedule, queue, alarm, and configuration item, plus external image/connector references. It contains no API key hash, but it does contain account IDs, ARNs, and the Function URL, so save it only under ignored artifacts:

```bash
./deployment/aws/scripts/export-inventory.sh
```

The DynamoDB table has AWS deletion protection and Terraform `prevent_destroy`; configuration items are also guarded. Deliberate teardown requires a reviewed code change to remove those guards first. The external MicroVM images are never in this stack's state.

Inspect failures with:

```bash
aws logs tail "/aws/lambda/any-embedding-ireland-gateway" --follow --region eu-west-1
aws logs tail "/aws/lambda/any-embedding-ireland-lifecycle" --follow --region eu-west-1
aws sqs receive-message \
  --queue-url "$(aws sqs get-queue-url --queue-name any-embedding-ireland-lifecycle-dlq --query QueueUrl --output text --region eu-west-1)" \
  --region eu-west-1
```

Operational failure cases:

- `403` from a worker endpoint means the JWE is missing, expired, or not scoped to port 8080; refresh once and retry without logging the token.
- `429` from a worker means per-VM connection/RPS capacity was reached; release the lease and retry another worker with jitter.
- `502` after traffic to a suspended endpoint indicates resume/application failure; mark the row failed and terminate it.
- `RunMicrovm` is limited to 5 TPS by default. Back off with jitter and rely on the per-pool launch lock and idempotency token.
- `GetMicrovm` state is eventually consistent. Endpoint readiness/response is authoritative; do not poll `GetMicrovm` on every embedding request.
- Never lease a VM close to its seven-hour hard expiry. The configured 900-second headroom is compared with predicted request runtime before a lease is accepted.

The service defaults include 5 TPS for run/resume, 2 TPS for suspend, 10 TPS for terminate, 100 TPS for get, and 50 TPS for auth-token creation. Confirm the live Ireland account quotas in [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html) before raising `max_instances`.

## IAM surface

The gateway role is restricted to the configured image ARNs and generated runtime role. It has:

- `lambda:RunMicrovm`, `lambda:GetMicrovm`, `lambda:TerminateMicrovm`, `lambda:CreateMicrovmAuthToken` on configured image ARNs.
- permission-only `lambda:PassNetworkConnector` on `*` because AWS does not expose resource-level scoping for that action.
- `iam:PassRole` only for the generated MicroVM execution role. Lambda MicroVM's current `RunMicrovm` authorization path did not supply a matching `iam:PassedToService` context value, so this remains resource-scoped instead of using that unsupported condition.
- table-scoped DynamoDB read/transaction/write permissions and log-stream writes.

The lifecycle role has table/stream access, `lambda:GetMicrovm` and `lambda:TerminateMicrovm` on configured images, account-level `lambda:ListMicrovms` for orphan discovery, DLQ send, and log writes. It can terminate an unregistered worker only when its image ARN is configured and its age exceeds the orphan grace period. The MicroVM runtime role can only write its own log group. Its trust policy includes `sts:AssumeRole` and `sts:TagSession`, as required by [Lambda MicroVM security and permissions](https://docs.aws.amazon.com/lambda/latest/dg/microvms-security.html). The Scheduler role can only invoke the lifecycle function and send to its DLQ.

Read the [cost model](docs/costs.md) before changing lifecycle windows, pool capacity, or tier thresholds.
