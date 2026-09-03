# AWS benchmark evidence

This directory keeps executable harnesses while raw, endpoint-specific results
remain local and ignored:

- [`function-url/`](function-url) measures the deployed OpenAI-compatible public
  endpoint, including routing, pool launch/resume, and response streaming.
- [`microvm/`](microvm) isolates direct Lambda MicroVM launch, capacity tiers,
  batching, long inputs, and suspend/resume behavior.

Harnesses write JSON to standard output; callers choose the destination. The
sanitized September 2 report contains the matched Ireland-versus-GCP run as well
as the earlier user-supplied and direct Frankfurt measurements, which remain
clearly marked as historical. See the [benchmark report](../docs/benchmarks.md)
for the normalized comparison and the [cost model](../docs/costs.md) for pricing.

## Live endpoint comparison

Run `function-url/benchmark.py` once per endpoint with the same iteration and
concurrency settings. Each result has a stable schema version and an `endpoint`
object containing the comparison label, region, and normalized base URL. The
harness retains the legacy `AWS_GATEWAY_URL` default, also accepts
`BENCHMARK_ENDPOINT_URL`, and infers the Region from a standard AWS Lambda
Function URL. Pass `--region` for endpoints such as Cloud Run whose hostname
does not encode it.

The bearer key is read only from the environment variable named by
`--api-key-env-var` (`--api-key-env` remains a compatibility alias); there is
intentionally no command-line option for the key and the value is never included
in JSON output. With the two key variables already populated by secret-injection
tooling, equivalent runs look like:

```bash
python3 deployment/aws/benchmarks/function-url/benchmark.py \
  --url "${AWS_GATEWAY_URL}" \
  --endpoint-label aws-ireland \
  --region eu-west-1 \
  --worker-pool-precondition empty-verified \
  --api-key-env-var AWS_BENCHMARK_API_KEY \
  > /tmp/aws-ireland.json

python3 deployment/aws/benchmarks/function-url/benchmark.py \
  --url "${GCP_GATEWAY_URL}" \
  --endpoint-label gcp-europe-west1 \
  --region europe-west1 \
  --worker-pool-precondition not-forced-unknown \
  --api-key-env-var GCP_BENCHMARK_API_KEY \
  > /tmp/gcp-europe-west1.json
```

Long-input measurements default to three repetitions. Override
`--long-repeats` explicitly only when both comparison runs use the same value.
The worker-pool precondition is mandatory metadata: verify the state immediately
before a controlled run, or use `not-forced-unknown` and avoid describing its
first request as a cold-start distribution.
