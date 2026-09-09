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
Other models retain their existing online behavior unless explicitly configured.
Model weights and inference precision are unchanged.

The Docker download stage now installs only Hugging Face Hub instead of the full
ML runtime. CPU worker images install CPU-only PyTorch. Both worker images set
ownership during `COPY`, avoiding an extra recursive ownership layer over all
weights, and compile newly copied application/model Python code at build time.
These are build and image improvements; their GCP latency impact is not yet measured.

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

GTE now specifies `concurrency: 4`, based on the four-way workload exercised in
the earlier benchmark. This bounds overlapping inference, but is a starting point,
not a validated optimum for production batches. With `max_instances: 1`, excess
traffic can still queue or be rejected by Cloud Run. Increase the maximum only
after checking GPU quota, cost and realistic load; reducing concurrency alone
does not create more capacity. See [Cloud Run concurrency](https://docs.cloud.google.com/run/docs/configuring/concurrency).

## Request processing and timeouts

The gateway reuses worker and metadata HTTP connections, caches identity tokens
per audience until shortly before expiry, and coalesces refreshes. This removes
repeated connection setup and metadata fetches from warm requests. The gateway
returns explicit 504/502 responses for upstream timeouts/transport failures.

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
Optional per-model `batch_size` controls the model's internal encode batch size
(default 32). Large batches of long inputs need separate throughput/memory testing.

Reduced-precision inference and dynamic batching are further experiments;
benchmark both latency and embedding quality before enabling them.

## Apply code changes reliably

`deploy.py` resolves gateway and worker tags to immutable image digests and passes
them through an explicit Terraform variable file. Pushing new bytes to `:latest`
alone does not change Terraform configuration and may otherwise leave Cloud Run
on its previous revision. Both normal deploys and `--skip-build` resolve digests;
`--skip-build` uses the images already in the registry.
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

Local checks:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
terraform -chdir=deployment/gcp fmt -check main.tf variables.tf
terraform -chdir=deployment/gcp validate
```
