# GCP latency and rollout

The current `gte-multilingual-base` bottleneck is tail latency. In the seven-day
Cloud Run request-log sample inspected on September 9, 2026, the gateway and GTE
worker each had 455 embedding POSTs, all returning HTTP 200 at the server:

| Service | p50 | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Gateway embedding endpoint | 79.6 ms | 17.79 s | 36.77 s | 61.56 s |
| GTE multilingual worker | 16.9 ms | 17.13 s | 36.33 s | 60.98 s |

The gateway sample includes all models routed through that endpoint. It had 62
requests over 10 seconds, 16 over 30 seconds, and one over 60 seconds. Server-side
HTTP 200 does not prove that a consumer was still waiting to receive the result.
These are production request-log aggregates, not a controlled cold-start test.
The [earlier matched benchmark](../aws/docs/benchmarks.md) also measured a 17.3 s
first GTE request versus 144 ms warm p50.

The inspected GTE deployment had an L4, 4 vCPU, 16 GiB, minimum zero instances,
maximum one instance, and concurrency 80. This combines scale-from-zero delays
with a single instance that can accept many overlapping model calls.

There were 56 GTE instance starts in the same window. All 56 GTE requests over
10 seconds overlapped an instance-start event (allowing a five-second leading
tolerance). The logged model-loading phase itself took 3.70 seconds at median
and 10.28 seconds at maximum; this excludes the rest of container startup.
Instance start to model ready took 13.08 seconds at median and 30.88 seconds at
p95; the time before the model constructor began was 9.38 seconds at median.
This correlation makes cold starts the leading explanation for the slow tail.

## Scale-to-zero startup

Keep minimum instances at **zero** for both GTE and the gateway. No additional scheduled
keepalive or reserved warm capacity is introduced. The GTE entry explicitly sets
`min_instances: 0`; `gateway_min_instances` and `worker_min_instances` default
to zero. The GTE `warmup` setting is false, avoiding a synthetic encode before
the first real request. Model loading still completes before readiness.

GTE uses custom code from `Alibaba-NLP/new-impl`. The downloader now bundles that
code at commit `40ced75c3017eb27626c9d4ea981bde21a2662f4`, already used by the AWS
GTE image, and rewrites the matching `auto_map` references to local modules.
All relative module dependencies are validated at build time. GTE can then run
with `offline: true`, which sets both Hugging Face offline flags on Cloud Run.
Model weights and inference precision are unchanged.

The Docker download stage installs only Hugging Face Hub instead of the full
ML runtime, downloads straight into the image's model directory, and keeps only
the files inference reads: ONNX and OpenVINO exports, notebooks, images and a
`pytorch_model.bin` that sits next to a `model.safetensors` are skipped. That
removes roughly 21 GB of dead weight across the fleet (e5-large-v2 shrinks from
6.7 GB of weights to 1.3 GB, gte-large-en-v1.5 from 6.4 GB to 1.7 GB). Every
model pins a Hub commit (`model_revision`) so rebuilds are reproducible.

Both worker images start from `python:3.12-slim`; the GPU image no longer
carries the `nvidia/cuda` runtime base because the PyTorch CUDA wheels vendor
the CUDA libraries and Cloud Run injects the driver. Dependencies are installed
with `uv` from the `pyproject.toml` extras, the model layer sits below the
application layers so a code change never re-exports weights, and the images
run uvicorn without access logs (Cloud Run records every request already).

All eleven workers run offline. Besides `gte-multilingual-base`, the custom
model code for `gte-large-en-v1.5` and the four Jina v2 models is baked at a
pinned commit, so no worker contacts the Hub at startup. The Jina images pin
`sentence-transformers` 5.1.2, the last release compatible with their
`transformers` 4.44.2 pin.

## Cold-start work inside the worker

Cloud Run streams container images lazily: every first open of a file is a
remote fetch, and importing the worker touches several thousand Python files
before the model constructor even starts (the 9.4 s median measured above).
The worker now starts two daemon threads before importing torch:

- one reads the model directory sequentially into the page cache, bounded by
  `MODEL_PREFETCH_MAX_BYTES` and 60 percent of the container memory limit, so
  the weights stream in while Python is still importing;
- one reads the import manifest generated at image build time (the byte-code
  files of every module the worker imports plus the `transformers/models`
  sources that `transformers` scans on import) with eight threads.

Both are best effort and can be disabled per model with `prefetch: false`.
The worker logs a `model_prefetch` line with bytes and timings next to the
existing `boot_to_ready_ms` line, which is how to verify the effect on Cloud Run.

Startup probes now poll every second with no initial delay (workers keep a
240 s budget, the gateway 60 s), so readiness is detected within a second of
the port opening instead of up to five seconds later.

Terraform passes the container's CPU limit to the worker (`WORKER_CPU_LIMIT`,
`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `RAYON_NUM_THREADS`) because Cloud Run
reports the host CPU count to the container. CPU workers default to 4 vCPU and
8 GiB: BERT-large class encodes scale almost linearly with cores and Cloud Run
bills CPU workers per request, so the latency gain is close to cost neutral.

Offline validation used the cached real GTE weights with sentence-transformers
2.7.0, Transformers 4.39.1 and CPU PyTorch 2.3.1 in a non-root Docker container,
network disabled and an empty Hugging Face cache. The newly materialized model
loaded and produced identical 768-dimensional vectors for three English, German
and Chinese inputs; the weights' SHA-256 digest was unchanged. This verifies the
packaging path against that cached revision, not GPU speed or embedding quality
across all inputs. No new live GCP performance numbers are claimed.

Scale-from-zero still includes Cloud Run GPU/container startup. If a consumer's
fixed deadline is shorter than that remaining startup time, guaranteed completion
requires a different request flow, such as an asynchronous job/result API. Raising
only the server timeout or repeatedly retrying the same cold request does not
resolve that deadline mismatch.

Every worker now defaults to `concurrency: 4` (`worker_concurrency` in
Terraform), the value GTE already used. Cloud Run scales out at 60 percent of
that target instead of piling up to 80 overlapping encodes on one instance.
Inside the worker only one request runs `model.encode` at a time
(`encode_parallelism`, default 1); input preparation such as image downloads
still overlaps. With `max_instances: 1`, excess traffic can still queue or be
rejected by Cloud Run. Increase the maximum only after checking GPU quota,
cost and realistic load; reducing concurrency alone does not create more
capacity. See [Cloud Run concurrency](https://docs.cloud.google.com/run/docs/configuring/concurrency).

## Request processing and timeouts

The gateway reuses worker and metadata HTTP connections (keep-alive for five
minutes instead of httpx's five seconds), caches identity tokens per audience
until shortly before expiry, prefetches them for every worker at startup, and
coalesces refreshes. Worker responses are forwarded byte for byte: the gateway
no longer parses, validates and re-serializes the embedding arrays, which cost
several hundred milliseconds and up to 128 MB of memory per 2048-item batch on
its single vCPU. The gateway returns explicit 504/502 responses for upstream
timeouts/transport failures.

The per-client rate limit keys on the real client address (the rightmost
`X-Forwarded-For` entry that Cloud Run appends) rather than the proxy peer,
which had made the limit a global ceiling per gateway instance. Terraform
enables this with `TRUST_X_FORWARDED_FOR=true`; outside Cloud Run the header
is caller-supplied and the TCP peer stays the key. The limit is
`gateway_rate_limit_rpm` in Terraform (default 3000 requests per minute).

`worker_timeout_seconds` in `terraform.tfvars` controls the gateway's total
deadline for token acquisition and worker inference (default 120 seconds). Set
it below the consumer's deadline with room for gateway startup, transport and
response handling. The gateway's Cloud Run timeout is 30 seconds longer; the
worker's Cloud Run timeout remains 300 seconds. Raising a server timeout does
not help a consumer that gives up earlier. A timed-out request can still leave
inference running on the worker; immediate retries may duplicate work.

Workers log model-load, warmup and boot-to-ready times, plus per-request input
preparation and encode times without recording input content. Input preparation
and result conversion run off the event loop so they do not block health checks.
Responses are serialized with `orjson` directly from the embedding array; the
values are identical to the previous output. Clients may request
`encoding_format: "base64"` (little-endian float32, as the OpenAI SDKs do by
default) for payloads a quarter of the size. Optional per-model `batch_size`
controls the model's internal encode batch size (default 32), and `max_tokens`
now caps the model's sequence length. Large batches of long inputs need separate
throughput/memory testing.

Reduced precision is available but off: `dtype: float16` or `bfloat16` loads
the weights in that precision, and `matmul_precision: high` enables TF32
matmuls on the L4. Both change embedding values slightly; benchmark latency and
embedding quality before enabling them. Dynamic batching remains an experiment.

## Apply code changes reliably

`deploy.py` resolves gateway and worker tags to immutable image digests and passes
them through an explicit Terraform variable file. Pushing new bytes to `:latest`
alone does not change Terraform configuration and may otherwise leave Cloud Run
on its previous revision. Both normal deploys and `--skip-build` resolve digests;
`--skip-build` uses the images already in the registry.

Images are content-addressed. Each build is tagged `:latest` and `:h-<hash>`,
where the hash covers the Dockerfile, the files it copies, and the build
arguments (model, revision, pins, custom-code commit). A deploy skips every
image whose hash tag already exists and only rolls services whose image digest
or settings changed; `--skip-build` prefers the same hash tag and falls back to
`:latest`. `--force-build` rebuilds regardless, which is also how to pick up
new base images or unpinned dependency releases. Builds set
`SOURCE_DATE_EPOCH=0` so a cache-hit rebuild reproduces the same digest.

The gateway and all workers build in one pool (`--parallel`, default 4;
BuildKit deduplicates identical steps such as the torch install across
concurrent builds). `terraform apply` runs with `--tf-parallelism` (default 1).
Cloud Run starts one instance per new revision to verify readiness, and those
instances count against the regional L4 quota (3 in `europe-west1`) together
with any GPU instance still serving the previous revision, so concurrent GPU
rollouts can fail on quota. Raise the value for deploys that touch only CPU
workers or the gateway (for example `--tf-parallelism 4`); with
content-addressed images most deploys roll few services anyway.
`terraform init` no longer upgrades providers; the committed lock file is
authoritative, and providers are cached in `TF_PLUGIN_CACHE_DIR`.
**Build the new GTE image before enabling its offline configuration**: older
images do not contain the external model code and can fail to start offline.

```bash
# Build/push changed images and review the infrastructure plan.
uv run python deployment/gcp/deploy.py --plan

# Apply using the images already reviewed in the registry.
uv run python deployment/gcp/deploy.py --skip-build
```

The second command resolves tags again: keep registry tags unchanged between
review and apply. These commands cover the configured fleet. After rollout, verify
the ready revision's image digest and serving settings, then compare short, long,
batch and burst requests with the consumer's actual deadline. Use worker startup
logs to distinguish a real cold cycle from an already-warm first benchmark call.

After rollout, compare the worker startup logs (`model_prefetch`,
`Model loaded`, `boot_to_ready_ms`) and `test_backend.sh` cold/warm latencies
against the numbers at the top of this document. The Terraform defaults for
execution environment (gen2 for workers, gen1 for the gateway) are documented
guidance rather than measurements; A/B them with `worker_execution_environment`
and `gateway_execution_environment` if cold starts matter more than CPU
throughput for a given service.

Local checks:

```bash
.venv/bin/python -m unittest discover -s tests -t .
terraform -chdir=deployment/gcp fmt -check main.tf variables.tf
terraform -chdir=deployment/gcp validate
```
