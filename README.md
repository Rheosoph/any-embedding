# any-embedding

<p align="left">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" />
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-OpenAI%20compatible-009688?logo=fastapi&logoColor=white" />
  <img alt="Google Cloud" src="https://img.shields.io/badge/Google%20Cloud-Cloud%20Run%20GPU-4285F4?logo=googlecloud&logoColor=white" />
  <img alt="AWS" src="https://img.shields.io/badge/AWS-Lambda%20MicroVMs-FF9900?logo=amazonaws&logoColor=white" />
</p>

A high-performance control plane for self-hosted embeddings.

<p align="left">
  <img alt="Flow Like" width="18" height="18" src="https://flow-like.com/favicon.svg" />
  <strong>In production for <a href="https://flow-like.com">flow-like.com</a></strong><br />
  Serverless gateway on Google Cloud Run, with isolated CPU and GPU embedding workers behind it.
</p>

any-embedding gives you one OpenAI-compatible endpoint in front of a fleet of sentence-transformers models, with per-model workers, baked model images, and provider-specific deployments for Google Cloud Run and AWS Lambda MicroVMs.

It is built for the reality of modern embedding infrastructure:
- the leaderboard changes constantly
- CPU and GPU models need different deployment shapes
- clients want one stable API, not a zoo of custom services

## Why teams reach for this

- One API, many models. Route by model name and keep client integration fixed.
- Config-driven fleet. Add or remove models in [config.yaml](config.yaml), then regenerate local compose or deploy with Terraform.
- No cold-start downloads. Worker images bake model weights at build time.
- GPU where it matters. Heavy models can run on dedicated GPU workers; smaller models stay on cheaper CPU workers.
- OpenAI-compatible surface. Existing SDKs and integrations can usually point at this with minimal adaptation.
- Single-command cloud deploy. Build, push, and apply infrastructure with one `mise` task.
- Clean separation of concerns. Gateway handles auth and routing. Workers only load and serve one model.

## How it works

```text
Client
  |
  v
Gateway (Cloud Run, Lambda Function URL, or local)
  - API key auth
  - OpenAI-compatible /v1/embeddings
  - model registry loaded from config.yaml
  - routes request to the correct worker
  |
  +--> Worker: BAAI/bge-large-en-v1.5
  +--> Worker: Qwen/Qwen3-Embedding-0.6B
  +--> Worker: gte-multilingual-base
  +--> Worker: CLIP / multimodal model
```

Each worker loads exactly one model and exposes an internal `/embed` endpoint. The gateway stays thin, predictable, and cheap. The workers scale independently based on the actual model mix you serve.

## Production posture

This repo is no longer just a demo wrapper around sentence-transformers. The current codebase already includes meaningful production controls:

- Constant-time API key comparison in the gateway.
- In-memory rate limiting at the public edge.
- Structured audit logging for API requests.
- Cloud Run identity tokens for gateway-to-worker calls.
- Internal-only worker ingress on GCP.
- SSRF protection for remote image URLs in multimodal workers.
- Secret Manager and CMEK-backed secret storage in the GCP deployment.
- Monitoring, uptime checks, and alerting in the GCP Terraform stack.
- DynamoDB lease fencing, native suspend/resume, reconciliation, and alarms in the AWS Terraform stack.

## Model catalog

The entire stack is driven by [config.yaml](config.yaml).

```yaml
models:
  - name: "bge-large-en-v1.5"
    model: "BAAI/bge-large-en-v1.5"
    type: "text"
    max_tokens: 512
    dimensions: 1024

  - name: "qwen3-embedding-0.6b"
    model: "Qwen/Qwen3-Embedding-0.6B"
    type: "text"
    max_tokens: 32768
    dimensions: 1024
    gpu: true
    cpu: "4"
    memory: "16Gi"
```

Supported per-model overrides:
- `cpu`
- `memory`
- `gpu`
- `max_instances`
- `min_instances`
- `sentence_transformers_version`
- `transformers_version`

This is the core design choice in the repo: configuration defines the fleet, and both local development and cloud deployment consume the same source of truth.

## Quickstart

### Run the full stack locally with Docker

This is the fastest way to validate the whole system end to end.

```bash
./test.sh
```

Useful variants:

```bash
./test.sh --up
./test.sh --down
```

The test script generates [docker-compose.yaml](docker-compose.yaml), builds the images, starts the gateway and workers, runs integration checks, and tears the stack down unless you keep it running.

If you want gated models to build locally, copy [.env.example](.env.example) to `.env` and set `HF_TOKEN` first.

### Develop locally without Docker

With `mise`:

```bash
mise install
mise run install
```

Start a worker:

```bash
MODEL_NAME="BAAI/bge-large-en-v1.5" mise run worker
```

Start the gateway in another terminal:

```bash
API_KEY="test-key" \
WORKER_URL_BGE_LARGE_EN_V1_5="http://localhost:8081" \
mise run gateway
```

Without `mise`, you can install directly:

```bash
uv sync --extra gateway --extra worker
```

Available task entry points live in [mise.toml](mise.toml), including local run, test, and AWS/GCP build and deploy flows.

## API

### `POST /v1/embeddings`

Text request:

```bash
curl https://<gateway-url>/v1/embeddings \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "bge-large-en-v1.5",
    "input": "query: serverless GPU embeddings"
  }'
```

Image request for multimodal models:

```bash
curl https://<gateway-url>/v1/embeddings \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "clip-vit-large-patch14",
    "input": {
      "type": "image",
      "image": {
        "type": "image_base64",
        "image_base64": "<base64-encoded-image>"
      }
    }
  }'
```

The API supports:
- single text inputs
- batch text inputs
- image inputs via `image_base64`
- image inputs via `image_url`

### `GET /v1/models`

List all configured models through an OpenAI-style response.

```bash
curl https://<gateway-url>/v1/models \
  -H "Authorization: Bearer <API_KEY>"
```

### `GET /health`

Basic health check for the gateway.

## Why the architecture is strong

- The gateway is stateless. It only validates auth, loads config, and forwards requests.
- The workers are isolated. One bad model does not poison the rest of the fleet.
- The images are reproducible. You know exactly which model ships with which service.
- The deployment is boring in the best possible way. Local compose, test automation, deploy automation, and Terraform all follow the same model registry.

This is the kind of setup that scales attention well: it is easy to explain, easy to extend, and does not collapse into custom one-off infrastructure as the model set grows.

## Deploy to Google Cloud

<p align="left">
  <img alt="Google Cloud logo" width="28" src="https://cdn.jsdelivr.net/gh/devicons/devicon/icons/googlecloud/googlecloud-original.svg" />
  <strong>Google Cloud Run deployment with serverless GPU workers</strong>
</p>

Google Cloud is the right deployment target for this project because Cloud Run is the only serverless option in this architecture that lets you keep the operational simplicity of a fully managed platform while still attaching GPUs to the workers that need them.

This is also the deployment story behind <a href="https://flow-like.com"><strong>flow-like.com</strong></a>: one public gateway, isolated model workers, and serverless GPU capacity when heavier embedding models need it.

Why that matters here:
- Serverless plus GPU is the hard problem. CPU-only embeddings are easy; serious embedding fleets usually are not.
- Cloud Run lets the gateway stay lightweight and public while model workers scale independently behind IAM.
- The Terraform in [deployment/gcp/main.tf](deployment/gcp/main.tf) already maps `gpu: true` models to NVIDIA L4-backed Cloud Run services.
- You avoid running Kubernetes just to serve one or two heavier embedding models.
- The project architecture matches Cloud Run cleanly: small gateway, isolated workers, image-based deploys, explicit service-to-service auth.
- The deployment path now includes Secret Manager, CMEK, long-term audit logs, uptime checks, and alerting without changing the application shape.

In short: if you want managed infrastructure and GPU-backed model workers without operating a cluster, Google Cloud is the story.

### Prerequisites

- A GCP project with billing enabled
- Docker
- Terraform 1.5+
- Authenticated `gcloud` access to push container images
- An Artifact Registry repository for your gateway and worker images
- A local `terraform.tfvars` copied from [deployment/gcp/terraform.tfvars.example](deployment/gcp/terraform.tfvars.example)

### Environment setup

Copy the example `.env` file and add your Hugging Face token (required for gated models like `google/embeddinggemma-300m`):

```bash
cp .env.example .env
```

Edit `.env` and set your token:

```
HF_TOKEN=hf_your_token_here
```

The deploy script in [deployment/gcp/deploy.py](deployment/gcp/deploy.py) loads `.env` automatically, passes `HF_TOKEN` as a BuildKit secret for image builds, and avoids baking that secret into the final image layers. The `.env` file is git-ignored.

### Deploy

A single command builds all Docker images (gateway + every worker), pushes them to your registry, and runs `terraform apply`:

```bash
mise run deploy:gcp
```

To preview changes without applying:

```bash
mise run deploy:gcp:plan
```

To re-apply Terraform without rebuilding images:

```bash
mise run deploy:gcp:tf-only
```

To test the deployed backend after rollout:

```bash
./deployment/gcp/test_backend.sh
```

Terraform will:
- create one Cloud Run service per model
- attach GPUs for models marked with `gpu: true`
- create the gateway service account
- create a dedicated worker service account
- wire worker invocation permissions
- store API keys and optional Hugging Face tokens in Secret Manager
- encrypt secrets and retained logs with Cloud KMS
- keep workers internal-only while exposing the gateway publicly
- configure audit-log retention, uptime checks, and alerting
- publish the gateway service and output its URL

### GCP layout

- [deployment/gcp/deploy.py](deployment/gcp/deploy.py) builds and pushes images, then runs Terraform.
- [deployment/gcp/main.tf](deployment/gcp/main.tf) defines Cloud Run services, IAM, secrets, logging, and monitoring.
- [deployment/gcp/test_backend.sh](deployment/gcp/test_backend.sh) validates the live deployment against every model in [config.yaml](config.yaml).
- [deployment/gcp/terraform.tfvars.example](deployment/gcp/terraform.tfvars.example) is the starting point for project-specific values.

The GCS backend is partially configured: `deploy.py` supplies `tfstate_bucket`
from the ignored `terraform.tfvars`, so the committed Terraform provider file
contains no project-specific bucket identifier.

## Deploy to AWS

The AWS stack targets `eu-west-1` (Ireland). Its public, OpenAI-compatible Function URL is emitted only as a Terraform output; `/v1/models` and `/v1/embeddings` use the same bearer-key contract as GCP. The control plane is strict TypeScript targeting Node.js 24. esbuild turns it into independent gateway and lifecycle bundles, while the Python MicroVM worker is a separate uv project with its own `pyproject.toml` and committed `uv.lock`.

Build both checked Lambda packages from the repository root:

```bash
./deployment/aws/scripts/package-lambdas.sh
```

For a standalone quality check without packaging:

```bash
npm --prefix app/aws ci --ignore-scripts
npm --prefix app/aws run check
```

The Terraform stack references the two archives separately. MicroVM image releases use a distinct package, S3 upload, and immutable publish workflow; Ireland currently serves `gte-multilingual-base` image version `1.0` in 2/4/8 GiB tiers. In the matched live run, AWS's observed first request was 4.87 seconds versus GCP's 17.34 seconds, while GCP was faster for warm, batched, concurrent, and long-input work. The nominal continuously allocated baseline is $669.11 per 720 hours for one of every AWS tier versus $753.49 for the configured GCP L4 worker; this is not a throughput-equivalent comparison. See the [AWS deployment guide](deployment/aws/README.md), [benchmark report](deployment/aws/docs/benchmarks.md), and [cost model](deployment/aws/docs/costs.md).

### AWS layout

- [app/aws](app/aws) contains the TypeScript Function URL gateway, tier router, pool registry, lifecycle reconciler, and uv-locked MicroVM worker; its tests live in [tests/aws](tests/aws).
- [deployment/aws](deployment/aws) separates the Ireland Terraform root, release scripts, benchmark harnesses, and sanitized documentation. Raw benchmark captures, resource inventories, and endpoint-specific handoff files stay in ignored local artifacts.
- AWS workers use real Lambda MicroVMs; only the small public router and lifecycle reconciler are ordinary Lambda functions.

## Project structure

- [app/gcp](app/gcp) - Cloud Run gateway, workers, and model downloader
- [app/aws](app/aws) - TypeScript Lambda control plane and standalone uv-locked MicroVM worker
- [tests/aws](tests/aws) - TypeScript tests for the AWS request contract, routing, pool, MicroVM client, and lifecycle logic
- [app/shared](app/shared) - provider-neutral Python API models
- [app/gateway.py](app/gateway.py) and [app/worker.py](app/worker.py) - backward-compatible GCP import shims
- [config.yaml](config.yaml) - model fleet definition
- [generate_compose.py](generate_compose.py) - local stack generation
- [deployment/aws](deployment/aws) - organized Lambda MicroVM Terraform, deployment scripts, benchmarks, and cost documentation
- [deployment/gcp/deploy.py](deployment/gcp/deploy.py) - build, push, and deploy orchestration for GCP
- [deployment/gcp/main.tf](deployment/gcp/main.tf) - Cloud Run services, IAM, secrets, logging, monitoring, GPU configuration
- [deployment/gcp/test_backend.sh](deployment/gcp/test_backend.sh) - live backend validation after deploy
- [.env.example](.env.example) - optional Hugging Face token setup for gated models
- [mise.toml](mise.toml) - local development, test, and deploy tasks
- [test.sh](test.sh) - end-to-end local validation

## Notes

- Clients must add any model-specific prefixes such as `query:` or `passage:` themselves.
- Some models are gated and require a valid `HF_TOKEN` with accepted license terms.
- Some Jina models pin an older `transformers` version for compatibility.
- The public gateway uses a preshared API key for authentication.
- Multimodal image URL inputs are restricted to avoid internal-network fetches.

## What to build next

If this project gets the attention it deserves, the obvious next layer is not a rewrite. It is observability, request accounting, and model-level traffic policy on top of the same gateway/worker split.

## License

Licensed under the [MIT License](LICENSE).
