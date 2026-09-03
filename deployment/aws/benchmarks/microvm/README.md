# Direct AWS Lambda MicroVM benchmarks

This directory preserves the benchmark harnesses and measured results for
`Alibaba-NLP/gte-multilingual-base` on the distinct AWS Lambda MicroVMs
service. The canonical worker image source is
[`../../../../app/aws/microvm`](../../../../app/aws/microvm), and its deployment artifact
is assembled by
[`../../scripts/package-microvm.sh`](../../scripts/package-microvm.sh). The service creates
an authenticated HTTPS endpoint per MicroVM; it does not use conventional
Lambda Function URLs.

The image uses the AWS snapshot-compatible Amazon Linux 2023 container base,
installs its locked Python environment with `uv`, pins the model and
trusted-code revisions, loads and warms the model before the MicroVM image
`/ready` hook succeeds, and serves both `/embed` and `/v1/embeddings` on port
8080. Do not copy runtime files back into this benchmark directory; update the
canonical worker instead.

`benchmark.py` uses the native AWS CLI to launch a MicroVM, create the endpoint
JWE, run cold/warm/batch/resume probes, and leave the MicroVM suspended rather
than terminating it. AWS CLI 2.36 or newer is required for the
`lambda-microvms` command group. All three harnesses compute wall-clock metrics
from the public OpenAI-compatible response. Private `_benchmark` response
telemetry is optional: when absent, `server_encode` and derived encode
throughput are `null`, while `server_encode_samples` records zero. The worker
therefore does not need to add non-standard fields to its public response.

Every harness attempts to suspend its target MicroVM in a `finally` block.
Failures in the benchmark or cleanup are emitted in the result and produce a
non-zero exit status; inspect `final_state` before assuming cleanup succeeded.

`long_input_benchmark.py` reuses an existing MicroVM for sequential 512,
2,048, 4,096, and 8,192-token probes, then confirms that the MicroVM returns to
`SUSPENDED`. Validate the repeated-text fixtures with the pinned tokenizer
before running it. Write raw output under `results/`; that directory is ignored
because captures contain MicroVM IDs and endpoints.

`capacity_benchmark.py` launches a specific image version and benchmarks its
launch, short-input latency, warm throughput, batches, all four long-input
sizes, and suspend/resume behavior. It validates the configured PyTorch thread
count and always attempts to leave the created MicroVM in `SUSPENDED` state.
The sanitized 2/4/8 GiB, cold-start, and warm-throughput aggregates are retained
in the [benchmark report](../../docs/benchmarks.md) and
[cost model](../../docs/costs.md), without account or instance identifiers.
