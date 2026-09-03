# AWS Ireland and GCP benchmark

Measured on 2026-09-02 through the public, OpenAI-compatible gateways for the live AWS Ireland deployment and the existing GCP deployment. Both runs used `gte-multilingual-base`, the same bearer credential, request fixtures, client, ordering, 20 short warm samples, three long-input samples, and three four-request concurrency bursts.

- AWS: local ignored capture `aws-ireland-2026-09-02.json`, `eu-west-1`, MicroVM image version `1.0`.
- GCP: local ignored capture `gcp-europe-west1-2026-09-02.json`, `europe-west1`, 4 vCPU, 16 GiB, one non-zonal L4.
- Harness: [`benchmark.py`](../benchmarks/function-url/benchmark.py).

Raw captures are excluded because they contain provider endpoint, account,
image, or instance identifiers. This report preserves the benchmark method and
decision-relevant aggregates.

Both endpoints returned one 768-dimensional embedding for the short and long requests and the requested number of 768-dimensional embeddings for each batch. The AWS worker pool was verified empty immediately before its run. GCP was configured with a zero minimum instance count, but its worker state was not forcibly reset or directly inspected. The gateway process was not deliberately forced cold on either provider. The first-request values therefore describe this observed run, not a cold-start percentile or controlled infrastructure-only measurement.

## Headline comparison

| Workload | AWS Ireland | GCP `europe-west1` | Result |
|---|---:|---:|---|
| Observed first short request | 4,872 ms | 17,341 ms | AWS 3.56x faster |
| Second short request | 368 ms | 196 ms | GCP 1.88x faster |
| Warm short, 20 runs | 261 ms p50 / 282 ms mean / 396 ms p95 | 144 ms p50 / 148 ms mean / 177 ms p95 | GCP 1.81x faster at p50 |
| Batch 8 | 700 ms / 11.4 embeddings/s | 178 ms / 45.0 embeddings/s | GCP 3.94x the throughput |
| Batch 32 | 780 ms / 41.0 embeddings/s | 260 ms / 122.9 embeddings/s | GCP 3.00x the throughput |

The observed first-to-warm-p50 premium was 4,611 ms on AWS and 17,197 ms on GCP. This sample supports a materially shorter first-use delay for the AWS pool in this run, while the warm distribution supports lower latency on the GCP L4 worker. Neither conclusion should be generalized to p95 cold starts without repeated, independently forced cold cycles.

The health probes were 171/123 ms on AWS and 105/70 ms on GCP. They measure gateway transport and control-plane response; they are not worker inference or forced gateway cold-start tests.

## Long input

The long-input fixtures are pinned repeated-text inputs that were previously validated at the listed model-token targets. Values below are end-to-end wall times from three sequential requests. The p50 is preferred for the comparison because the first AWS request at 2,048 and 8,192 tokens also had to create that tier's first worker.

| Input | AWS routed tier | AWS wall p50 | GCP wall p50 | GCP advantage | AWS mean tokens/s | GCP mean tokens/s |
|---|---|---:|---:|---:|---:|---:|
| 512 tokens | small | 772 ms | 147 ms | 5.26x lower latency | 668 | 3,420 |
| 2,048 tokens | medium | 1,960 ms | 201 ms | 9.76x lower latency | 566* | 9,902 |
| 4,096 tokens | medium | 5,753 ms | 350 ms | 16.43x lower latency | 713 | 11,787 |
| 8,192 tokens | large | 10,260 ms | 797 ms | 12.88x lower latency | 687* | 10,280 |

\* The reported token rate uses the three-sample mean, which includes the first worker-launch sample. For 2,048 tokens the AWS samples were 6,949, 1,946, and 1,960 ms; for 8,192 tokens they were 15,313, 10,260, and 10,206 ms.

The public contract accepts an individual text up to 100,000 Unicode code points and batches up to 2,048 items, but this model truncates each input at 8,192 tokens. Lambda's synchronous request-body limit remains 6 MB even with response streaming, so clients must split payloads that exceed it. The largest response measured here was batch 32 at about 520 KB.

## Batching and concurrency

Client-side batching was substantially more efficient than independent AWS requests: batch 32 reached 41.0 embeddings/s, versus 5.34 requests/s averaged over the four-way short bursts. GCP reached 122.9 embeddings/s for batch 32.

| Four-way workload | AWS aggregate p50 | AWS mean throughput | GCP aggregate p50 | GCP mean throughput |
|---|---:|---:|---:|---:|
| Short, small tier | 606 ms | 5.34 requests/s | 277 ms | 14.68 requests/s |
| 2,048 tokens, medium tier | 7,813 ms | 0.512 requests/s | 576 ms | 6.96 requests/s |

The configured per-VM capacities `2/4/4` remain admission and queue-safety limits, not claims of linear inference parallelism. For cost and throughput, prefer one compatible client batch over many independent calls. Latency-sensitive independent traffic needs more workers, which trades reduced queueing for more launch and lifecycle cost.

## What higher CPU and RAM changed

The matched public run exercised routing rather than holding the workload constant across every AWS tier. The controlled tier comparison comes from the earlier direct version-1.0 Frankfurt experiment. Frankfurt resources have since been deleted; its raw capture remains local and ignored.

AWS couples more baseline memory with more baseline CPU, burst ceiling, disk, and network capacity, so this is not a RAM-only experiment:

| Tier | Baseline | Peak | Warm short | Batch 32 | 512 tokens | 2,048 tokens | 4,096 tokens | 8,192 tokens |
|---|---|---|---:|---:|---:|---:|---:|---:|
| small | 2 GiB / 1 vCPU | 8 GiB / 4 vCPU | 113 ms | 52.3 emb/s | 589 ms | 3,402 ms | 10,335 ms | 36,059 ms |
| medium | 4 GiB / 2 vCPU | 16 GiB / 8 vCPU | 104 ms | 75.1 emb/s | 404 ms | 1,910 ms | 5,624 ms | 18,244 ms |
| large | 8 GiB / 4 vCPU | 32 GiB / 16 vCPU | 94 ms | 106.3 emb/s | 239 ms | 947 ms | 2,669 ms | 8,443 ms |

Against small, medium was only 1.09x faster for a short input but 1.44x faster for batch 32 and 1.78-1.98x faster for 2K-8K inputs. Large was 1.20x faster for a short input, 2.03x for batch 32, and 3.59-4.27x faster for 2K-8K. Extra capacity has little payoff for tiny requests and a large payoff for longer attention windows or batches, which supports routing small work to the cheapest tier.

Do not use the historical direct timings as a regional A/B test: they omit the public gateway, use different hosts, and were measured before the Ireland cutover. They isolate tier shape well enough to guide routing, not to predict an Ireland SLO.

## Historical Frankfurt evidence

The original Frankfurt Function URL matrix and direct MicroVM captures remain local and ignored. They used a different deployed image revision and benchmark method and are intentionally excluded from the matched AWS-versus-GCP table above.

The live GCP first request (17,341 ms) also differs materially from the earlier user-supplied 36,979 ms result. That is another reason to keep both as timestamped observations instead of treating either one as a cold-start distribution. The two matched runs were sequential rather than simultaneous, so ambient network and host variance remain possible.

See the [cost model](costs.md) for regional price inputs and conservative active-request proxies. AWS documents the MicroVM lifecycle in [Running and using MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html) and Function URL streaming limits in [Response streaming](https://docs.aws.amazon.com/lambda/latest/dg/configuration-response-streaming.html).
