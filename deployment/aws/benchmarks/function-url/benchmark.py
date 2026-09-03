"""Benchmark one OpenAI-compatible endpoint for repeatable live comparisons."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import statistics
import threading
import time
from collections.abc import Sequence
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


SHORT_TEXT = "The quick brown fox jumps over the lazy dog."
LONG_INPUTS = {
    512: "hello " * 255,
    2048: "hello " * 1023,
    4096: "hello " * 2047,
    8192: "hello " * 4095,
}
EXPECTED_TIERS = {
    512: "small",
    2048: "medium",
    4096: "medium",
    8192: "large",
}

AWS_FUNCTION_URL_HOST = re.compile(
    r"\.lambda-url\.(?P<region>[a-z0-9-]+)\.on\.aws$"
)
ENDPOINT_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REGION_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
ENVIRONMENT_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORKER_POOL_PRECONDITIONS = (
    "empty-verified",
    "running-verified",
    "suspended-verified",
    "not-forced-unknown",
)


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values), 3),
        "mean_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def infer_aws_region(url: str) -> str | None:
    """Return the Region encoded in a standard Lambda Function URL hostname."""

    hostname = urlsplit(url).hostname or ""
    match = AWS_FUNCTION_URL_HOST.search(hostname.lower())
    return match.group("region") if match else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark one OpenAI-compatible endpoint and write one JSON document "
            "to standard output."
        )
    )
    parser.add_argument(
        "--url",
        default=(
            os.environ.get("BENCHMARK_ENDPOINT_URL")
            or os.environ.get("AWS_GATEWAY_URL")
        ),
        help=(
            "Endpoint base URL. Defaults to BENCHMARK_ENDPOINT_URL, then the "
            "legacy AWS_GATEWAY_URL."
        ),
    )
    parser.add_argument(
        "--endpoint-label",
        default=os.environ.get("BENCHMARK_ENDPOINT_LABEL", "aws"),
        help=(
            "Stable comparison label, for example aws-ireland or gcp-europe-west1 "
            "(default: BENCHMARK_ENDPOINT_LABEL or aws)."
        ),
    )
    parser.add_argument(
        "--region",
        help=(
            "Region metadata. Defaults to BENCHMARK_REGION, an AWS Function URL's "
            "encoded Region, or an error when neither is available."
        ),
    )
    parser.add_argument(
        "--api-key-env-var",
        "--api-key-env",
        dest="api_key_env",
        default="API_KEY",
        help=(
            "Name of the environment variable containing the bearer key. The key "
            "itself is never accepted as an argument or emitted in the result."
        ),
    )
    parser.add_argument(
        "--worker-pool-precondition",
        default=os.environ.get("BENCHMARK_WORKER_POOL_PRECONDITION"),
        choices=WORKER_POOL_PRECONDITIONS,
        help=(
            "Worker state immediately before the run. Required explicitly or via "
            "BENCHMARK_WORKER_POOL_PRECONDITION so first-use timings cannot be "
            "mistaken for a controlled cold start."
        ),
    )
    parser.add_argument("--warm-iterations", type=int, default=10)
    parser.add_argument("--long-repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--concurrent-repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=900)
    return parser


def parse_arguments(
    argv: Sequence[str] | None = None,
) -> tuple[argparse.Namespace, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.url:
        parser.error(
            "--url, BENCHMARK_ENDPOINT_URL, or AWS_GATEWAY_URL is required"
        )

    args.url = args.url.rstrip("/")
    parsed_url = urlsplit(args.url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        parser.error(
            "--url must be an HTTPS endpoint without user info, query, or fragment"
        )

    args.endpoint_label = args.endpoint_label.strip()
    if not ENDPOINT_LABEL.fullmatch(args.endpoint_label):
        parser.error(
            "--endpoint-label must be 1-64 letters, numbers, dots, underscores, "
            "or hyphens"
        )

    inferred_region = infer_aws_region(args.url)
    environment_region = os.environ.get("BENCHMARK_REGION")
    if args.region is not None:
        args.region = args.region.strip().lower()
        args.region_source = "argument"
    elif environment_region:
        args.region = environment_region.strip().lower()
        args.region_source = "environment"
    elif inferred_region:
        args.region = inferred_region
        args.region_source = "function_url"
    else:
        parser.error(
            "--region or BENCHMARK_REGION is required when the URL does not encode "
            "an AWS Region"
        )
    if not REGION_LABEL.fullmatch(args.region):
        parser.error("--region has an invalid format")
    if inferred_region and args.region.lower() != inferred_region:
        parser.error("--region does not match the AWS Function URL Region")

    if not ENVIRONMENT_VARIABLE_NAME.fullmatch(args.api_key_env):
        parser.error("API key environment variable name has an invalid format")
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        parser.error("API key environment variable is missing or empty")
    if args.worker_pool_precondition is None:
        parser.error(
            "--worker-pool-precondition or "
            "BENCHMARK_WORKER_POOL_PRECONDITION is required"
        )
    if args.warm_iterations < 1 or args.long_repeats < 1:
        parser.error("--warm-iterations and --long-repeats must be at least 1")
    if args.concurrency < 1 or args.concurrent_repeats < 1:
        parser.error("--concurrency and --concurrent-repeats must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args, api_key


class Gateway:
    def __init__(self, url: str, api_key: str, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def json(
        self,
        method: str,
        path: str,
        payload: Any = None,
    ) -> tuple[Any, float, int]:
        body = None if payload is None else json.dumps(
            payload, separators=(",", ":")
        ).encode()
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_body = response.read()
                status = response.status
        except HTTPError as error:
            response_body = error.read()
            status = error.code
        wall_ms = (time.perf_counter() - started) * 1000
        parsed = json.loads(response_body)
        if status >= 400:
            # Do not echo an arbitrary endpoint response: a misconfigured service
            # could reflect the bearer credential it received.
            raise RuntimeError(f"{method} {path} returned HTTP {status}")
        return parsed, wall_ms, len(response_body)

    def embed(self, inputs: str | list[str]) -> dict[str, Any]:
        body, wall_ms, response_bytes = self.json(
            "POST",
            "/v1/embeddings",
            {"model": "gte-multilingual-base", "input": inputs},
        )
        data = body["data"]
        if len(data) != (len(inputs) if isinstance(inputs, list) else 1):
            raise RuntimeError("response item count does not match request")
        if data and len(data[0]["embedding"]) != 768:
            raise RuntimeError("response embedding does not have 768 dimensions")
        expected_keys = {"object", "data", "model", "usage"}
        if set(body) != expected_keys:
            raise RuntimeError(
                f"public response keys differ from GCP contract: {sorted(body)}"
            )
        return {
            "wall_ms": round(wall_ms, 3),
            "response_bytes": response_bytes,
            "items": len(data),
            "dimensions": len(data[0]["embedding"]) if data else 768,
        }


def concurrent_burst(
    gateway: Gateway,
    inputs: str | list[str],
    concurrency: int,
) -> dict[str, Any]:
    """Start callers together so the worker's dynamic batcher can coalesce them."""

    barrier = threading.Barrier(concurrency)

    def invoke() -> dict[str, Any]:
        barrier.wait(timeout=10)
        return gateway.embed(inputs)

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        samples = list(executor.map(lambda _: invoke(), range(concurrency)))
    aggregate_ms = (time.perf_counter() - started) * 1000
    items_per_request = len(inputs) if isinstance(inputs, list) else 1
    return {
        "concurrency": concurrency,
        "aggregate_wall_ms": round(aggregate_ms, 3),
        "requests_per_second": round(concurrency * 1000 / aggregate_ms, 3),
        "embeddings_per_second": round(
            concurrency * items_per_request * 1000 / aggregate_ms,
            3,
        ),
        "request_latency": summarize([sample["wall_ms"] for sample in samples]),
        "samples": samples,
    }


def main(argv: Sequence[str] | None = None) -> None:
    args, api_key = parse_arguments(argv)
    gateway = Gateway(args.url, api_key, args.timeout)
    output: dict[str, Any] = {
        "schema_version": 1,
        "benchmark_unix_seconds": time.time(),
        "endpoint": {
            "label": args.endpoint_label,
            "region": args.region,
            "region_source": args.region_source,
            "base_url": gateway.url,
        },
        # Retain the original flat fields for consumers of the Frankfurt result.
        "region": args.region,
        "gateway_url": gateway.url,
        "model": "gte-multilingual-base",
        "method": {
            "short_text": SHORT_TEXT,
            "long_inputs": (
                "Pinned hello-repeat fixtures previously tokenizer-validated"
            ),
            "warm_iterations": args.warm_iterations,
            "long_repeats": args.long_repeats,
            "concurrency": args.concurrency,
            "concurrent_repeats": args.concurrent_repeats,
            "gateway_cold_start_forced": False,
            "worker_pool_precondition": args.worker_pool_precondition,
        },
    }

    health, health_first_ms, _ = gateway.json("GET", "/health")
    health_second, health_second_ms, _ = gateway.json("GET", "/health")
    if health_second != health:
        raise RuntimeError("health response changed between cold and warm probes")
    models, models_ms, _ = gateway.json("GET", "/v1/models")
    output["contract"] = {
        "health": health,
        "gateway_health_probe_1_ms": round(health_first_ms, 3),
        "gateway_health_probe_2_ms": round(health_second_ms, 3),
        "gateway_health_probe_delta_ms": round(
            health_first_ms - health_second_ms,
            3,
        ),
        "model_ids": [item["id"] for item in models["data"]],
        "models_ms": round(models_ms, 3),
    }

    first = gateway.embed(SHORT_TEXT)
    second = gateway.embed(SHORT_TEXT)
    warm = [gateway.embed(SHORT_TEXT) for _ in range(args.warm_iterations)]
    warm_summary = summarize([sample["wall_ms"] for sample in warm])
    output["short"] = {
        "first": first,
        "second": second,
        "first_to_second_delta_ms": round(first["wall_ms"] - second["wall_ms"], 3),
        "first_to_warm_p50_delta_ms": round(
            first["wall_ms"] - warm_summary["p50_ms"],
            3,
        ),
        "warm": warm_summary,
        "samples": warm,
    }

    batches: dict[str, Any] = {}
    for batch_size in (8, 32):
        result = gateway.embed([SHORT_TEXT] * batch_size)
        result["embeddings_per_second"] = round(
            batch_size * 1000 / result["wall_ms"], 3
        )
        batches[str(batch_size)] = result
    output["batches"] = batches

    long_results: dict[str, Any] = {}
    for token_count, text in LONG_INPUTS.items():
        samples = [gateway.embed(text) for _ in range(args.long_repeats)]
        wall = summarize([sample["wall_ms"] for sample in samples])
        long_results[str(token_count)] = {
            "target_model_tokens": token_count,
            "expected_routing_tier": EXPECTED_TIERS[token_count],
            "characters": len(text),
            "wall": wall,
            "wall_tokens_per_second": round(token_count * 1000 / wall["mean_ms"], 3),
            "samples": samples,
        }
    output["long_inputs"] = long_results

    concurrent: dict[str, Any] = {}
    for name, text in {
        "short_small_tier": SHORT_TEXT,
        "long_2048_medium_tier": LONG_INPUTS[2048],
    }.items():
        bursts = [
            concurrent_burst(gateway, text, args.concurrency)
            for _ in range(args.concurrent_repeats)
        ]
        concurrent[name] = {
            "bursts": bursts,
            "aggregate_wall": summarize(
                [burst["aggregate_wall_ms"] for burst in bursts]
            ),
            "mean_requests_per_second": round(
                statistics.fmean(
                    burst["requests_per_second"] for burst in bursts
                ),
                3,
            ),
        }
    output["concurrent"] = concurrent
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
