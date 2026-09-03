# Cost model

The Ireland stack avoids API Gateway, NAT Gateway, provisioned DynamoDB capacity, an always-warm fleet, and per-instance Scheduler resources. Lambda Function URLs have no separate endpoint fee, but invocations, execution time, streamed response bytes, logs, and normal data transfer remain billable.

All figures are USD before tax, support, credits, free-tier offsets, or discounts. They are engineering estimates, not quotes. Refresh the [AWS public price list](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-ppslong.html), [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/), and [Cloud Run pricing](https://cloud.google.com/run/pricing) before making a budget commitment.

## Ireland price inputs

The AWS Price List values retrieved on 2026-09-02 for ARM in `eu-west-1` are:

| Charge | Ireland rate |
|---|---:|
| MicroVM vCPU | $0.0000291572 / vCPU-second |
| MicroVM memory | $0.0000038603 / GiB-second |
| MicroVM snapshot read | $0.0016403088 / GiB |
| MicroVM snapshot write | $0.0040636422 / GiB |
| MicroVM image/suspended snapshot storage | $0.0001322222 / GiB-hour |
| Gateway Lambda compute, first ARM tier | $0.0000133334 / GiB-second |
| Gateway Lambda requests | $0.20 / million |
| Lambda streamed response bytes | $0.008 / GiB |
| DynamoDB standard-table read request units | $0.1415 / million |
| DynamoDB standard-table write request units | $0.705 / million |
| DynamoDB standard-table storage above the account allowance | $0.283 / GiB-month |

Response streaming's first 6 MiB per response is not charged, and the Lambda free tier currently includes 100 GB/month of streamed responses. Apply both allowances correctly per response and account; normal data-transfer charges are separate. DynamoDB prices are per request unit rather than per SDK call, so item size, consistency, and transactions affect the result.

MicroVM image storage has a one-week minimum retention period. Using the earlier build-reported memory-plus-disk sizes as a proxy, one version of all three images is about 9.860 GiB: approximately **$0.219** for 168 hours or **$0.939** for 720 hours. CUR data is required for the actual image and suspend-checkpoint sizes.

## Tier baseline and idle lifecycle

The baseline running rate is:

```text
hourly = 3,600 * (baseline_vcpu * vcpu_rate + baseline_gib * memory_rate)
```

| Tier | Baseline | Peak ceiling | Baseline/hour | Baseline/720 h |
|---|---|---|---:|---:|
| small | 1 vCPU / 2 GiB | 4 vCPU / 8 GiB | $0.132760 | $95.59 |
| medium | 2 vCPU / 4 GiB | 8 vCPU / 16 GiB | $0.265520 | $191.17 |
| large | 4 vCPU / 8 GiB | 16 vCPU / 32 GiB | $0.531040 | $382.35 |
| one of every tier | 7 vCPU / 14 GiB | 28 vCPU / 56 GiB | **$0.929321** | **$669.11** |

For one idle gap of `g` seconds:

```text
keep-running = g * baseline_compute_rate
suspend      = snapshot_gib * (snapshot_write_rate + snapshot_read_rate)
             + g * snapshot_gib * snapshot_storage_rate_per_second
```

Using the measured checkpoint-size proxies, the Ireland crossovers and configured native policies are:

| Tier | Size proxy | Crossover | Run idle | Suspended | Capacity / VM | Max VMs |
|---|---:|---:|---:|---:|---:|---:|
| small | 3.061 GiB | 475 s | 485 s | 1,315 s | 2 | 2 |
| medium | 3.228 GiB | 250 s | 255 s | 1,545 s | 4 | 2 |
| large | 3.571 GiB | 138 s | 145 s | 1,655 s | 4 | 2 |

Every tier reaches native termination after 1,800 seconds of inactivity and has a seven-hour hard lifetime. The DynamoDB `ttl_at` value is logical expiry and delayed cleanup, not an inactivity timer.

A no-reuse lifecycle—one image read, the configured idle-running window, one suspend write, and snapshot storage for the rest of the 30-minute window—has these MicroVM-only estimates:

| Tier | Read + write | Idle compute | Suspended storage | Lifecycle component |
|---|---:|---:|---:|---:|
| small | $0.01746 | $0.01789 | $0.00015 | **$0.03549** |
| medium | $0.01841 | $0.01881 | $0.00018 | **$0.03740** |
| large | $0.02037 | $0.02139 | $0.00022 | **$0.04197** |

These are not request prices. They exclude active launch/inference resources, gateway Lambda, DynamoDB, logs, and transfer. Reuse amortizes image reads and suspend writes over the requests in one activity episode; a resume adds another snapshot read and a later suspend adds another write.

## AWS versus GCP

The existing GCP `europe-west1` worker is configured for Cloud Run instance-based billing with 4 vCPU, 16 GiB, and one non-zonally-redundant L4:

```text
GCP worker/second = 4 * $0.000018
                  + 16 * $0.000002
                  + 1 * $0.0001867
                  = $0.0002907

GCP worker/hour   = $1.04652
GCP worker/720 h  = $753.49
```

| Continuously allocated configuration | Hour | 720 hours |
|---|---:|---:|
| AWS small + medium + large baseline | $0.92932 | $669.11 |
| GCP 4 vCPU / 16 GiB / L4 worker | $1.04652 | $753.49 |
| Nominal difference | AWS 11.2% lower | AWS $84.38 lower |

This is a configured-capacity comparison, not throughput equivalence. The matched benchmark showed the GCP GPU worker materially faster for warm, batched, concurrent, and long-input work. AWS obtains its economic advantage only when the router selects the smallest adequate tier and idle workers suspend or terminate; keeping all AWS tiers continuously active is not the intended operating mode.

The Ireland baseline rates are about 16.7% below the former Frankfurt rates. One continuously running instance of every tier would have cost about $803.09 per 720 hours in Frankfurt versus $669.11 in Ireland, a nominal $133.98 reduction before other charges.

### Conservative active-request proxy

The following estimates use the 2026-09-02 matched public-path p50 wall times for short and long inputs, and the single recorded wall time for each batch, from the [benchmark report](benchmarks.md). For AWS, the selected MicroVM tier is charged at its **full peak ceiling for the entire client wall time**, then the 256 MiB gateway duration and one Lambda request are added. For GCP, the full 4-vCPU/16-GiB/L4 worker rate is applied to the client wall time. This intentionally conservative method can exceed actual AWS burst usage; it also excludes GCP gateway compute, request charges, lifecycle idle time, snapshot operations, logs, storage, and transfer.

| Workload | AWS selected tier | AWS proxy per request/batch | GCP worker proxy per request/batch | Proxy per million embeddings: AWS / GCP |
|---|---|---:|---:|---:|
| Warm short, p50 | small | $0.0000396 | $0.0000419 | $39.63 / $41.94 |
| Batch 8 | small | $0.0001058 | $0.0000517 | $13.23 / $6.46 |
| Batch 32 | small | $0.0001179 | $0.0000757 | $3.68 / $2.36 |
| 512 tokens, p50 | small | $0.0001166 | $0.0000427 | $116.60 / $42.66 |
| 2,048 tokens, p50 | medium | $0.0005849 | $0.0000584 | $584.94 / $58.36 |
| 4,096 tokens, p50 | medium | $0.0017167 | $0.0001018 | $1,716.69 / $101.79 |
| 8,192 tokens, p50 | large | $0.0060885 | $0.0002316 | $6,088.51 / $231.60 |

The short single-request proxy is roughly level because AWS's lower per-second CPU price offsets its slower warm latency. GCP is cheaper in this proxy for batching and long input because its measured GPU speed more than offsets the higher full-instance rate. These values must not be presented as invoice prices or multiplied into a monthly forecast without adding the providers' actual lifecycle traces.

At very sparse traffic, AWS's $0.03549-$0.04197 no-reuse lifecycle component can dominate one inference. Cloud Run GPU instance-based billing has a one-minute minimum, which is theoretically $0.01744 for this worker configuration before gateway and other charges; however, an idle GPU instance may remain allocated for up to ten minutes during scale-down, or $0.17442 at this rate. Real instance lifetime and AWS checkpoint billing must be measured from billing exports before deciding which provider wins at a particular arrival rate. See [Cloud Run autoscaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling).

## Historical CPU/RAM evidence

The direct September 2 capacity run measured one version-1.0 MicroVM per tier in Frankfurt before those resources were deleted. Its raw capture is local and ignored because it contains account, image, and instance identifiers. The sanitized measurements below isolate tier shape; they are not part of the regional cost comparison and do not include the public gateway.

| Tier | Warm short | Batch 32 | 512 tokens | 2,048 tokens | 4,096 tokens | 8,192 tokens |
|---|---:|---:|---:|---:|---:|---:|
| small | 113 ms | 52.3 emb/s | 589 ms | 3,402 ms | 10,335 ms | 36,059 ms |
| medium | 104 ms | 75.1 emb/s | 404 ms | 1,910 ms | 5,624 ms | 18,244 ms |
| large | 94 ms | 106.3 emb/s | 239 ms | 947 ms | 2,669 ms | 8,443 ms |

The larger tiers spend more per second but become progressively more efficient for long attention windows. Small remains the cost-first choice for short calls. Use current Ireland end-to-end p95 and billed burst/checkpoint measurements before changing the router.

## Monthly estimator

Build both provider estimates from the same arrival trace, input-token distribution, batch sizes, output sizes, and latency objective:

```text
aws_baseline = running_seconds
  * (baseline_vcpu * vcpu_rate + baseline_gib * memory_rate)

aws_burst = extra_vcpu_seconds * vcpu_rate
  + extra_gib_seconds * memory_rate

aws_snapshot_ops = launches * image_gib * read_rate
  + resumes * checkpoint_gib * read_rate
  + suspends * checkpoint_gib * write_rate

aws_snapshot_storage = suspended_gib_hours * storage_rate

aws_gateway = requests * lambda_request_rate
  + gateway_gib_seconds * lambda_arm_gib_second_rate
```

Add image storage, DynamoDB consumed units and storage, lifecycle Lambda/SQS activity, CloudWatch, response streaming, and network transfer. For GCP, include the full billable instance lifetime and one-minute minimum for every scale-from-zero episode, plus gateway, logging, storage, registry, and network charges.

## Cost controls in the stack

- Smallest compatible tier first, with native tier-specific suspension and a seven-hour hard ceiling.
- Two instances maximum per model/tier by default, with `2/4/4` leases per VM.
- One on-demand DynamoDB table, one shared five-minute reconciler, and one shared DLQ.
- Lambda-managed internet egress; no NAT Gateway or VPC connector in this test deployment.
- PITR off by default and CloudWatch log retention set to 30 days.
- CloudWatch alarms for gateway/lifecycle errors, throttling, latency, DynamoDB throttling, and visible DLQ messages.

Do not log embeddings, request bodies, API keys, or endpoint JWE tokens. Besides being sensitive, they create avoidable ingestion cost.
