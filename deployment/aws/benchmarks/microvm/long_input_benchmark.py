"""Benchmark exact long-token inputs on an existing Lambda MicroVM."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from typing import Any
from urllib.request import Request, urlopen


TOKEN_TARGETS_TO_HELLO_REPEATS = {
    512: 255,
    2048: 1023,
    4096: 2047,
    8192: 4095,
}


def aws_json(region: str, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "aws",
            "lambda-microvms",
            *arguments,
            "--region",
            region,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


def create_token(region: str, microvm_id: str) -> str:
    response = aws_json(
        region,
        "create-microvm-auth-token",
        "--microvm-identifier",
        microvm_id,
        "--expiration-in-minutes",
        "60",
        "--allowed-ports",
        '[{"allPorts":{}}]',
    )
    return response["authToken"]["X-aws-proxy-auth"]


def microvm_state(region: str, microvm_id: str) -> str:
    return aws_json(
        region,
        "get-microvm",
        "--microvm-identifier",
        microvm_id,
    )["state"]


def wait_for_state(
    region: str, microvm_id: str, target: str, timeout_seconds: float = 120
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = microvm_state(region, microvm_id)
        if state == target:
            return state
        time.sleep(2)
    raise TimeoutError(f"MicroVM did not reach {target} within {timeout_seconds}s")


def normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    if endpoint.startswith("https://"):
        return endpoint
    if endpoint.startswith("http://"):
        raise ValueError("The Lambda MicroVM endpoint must use HTTPS")
    return f"https://{endpoint}"


def embed(endpoint: str, token: str, text: str) -> dict[str, Any]:
    payload = json.dumps(
        {"model": "gte-multilingual-base", "input": text}, separators=(",", ":")
    ).encode()
    request = Request(
        f"{endpoint}/v1/embeddings",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "X-aws-proxy-auth": token,
            "X-aws-proxy-port": "8080",
        },
    )
    started = time.perf_counter()
    with urlopen(request, timeout=900) as response:
        body = json.loads(response.read())
    dimensions = len(body["data"][0]["embedding"])
    if dimensions != 768:
        raise ValueError(f"Expected 768 dimensions, got {dimensions}")
    raw_benchmark = body.get("_benchmark")
    benchmark = raw_benchmark if isinstance(raw_benchmark, dict) else {}
    raw_encode_ms = benchmark.get("encode_ms")
    server_encode_ms = (
        float(raw_encode_ms)
        if isinstance(raw_encode_ms, (int, float))
        and not isinstance(raw_encode_ms, bool)
        else None
    )
    return {
        "wall_ms": (time.perf_counter() - started) * 1000,
        "server_encode_ms": server_encode_ms,
        "dimensions": dimensions,
        "request_number": benchmark.get("request_number"),
    }


def summary(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values), 3),
        "mean_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(statistics.median(values), 3),
        "max_ms": round(max(values), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--microvm-id", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")

    endpoint = normalize_endpoint(args.endpoint)
    results: dict[str, Any] = {}
    output: dict[str, Any] = {
        "region": args.region,
        "microvm_id": args.microvm_id,
        "endpoint": endpoint,
        "input_construction": (
            "Repeat the ASCII string 'hello ' 255/1023/2047/4095 times; "
            "validate against the pinned model tokenizer before use"
        ),
        "results": results,
    }
    benchmark_error: Exception | None = None
    cleanup_error: Exception | None = None
    try:
        output["starting_state"] = microvm_state(args.region, args.microvm_id)
        token = create_token(args.region, args.microvm_id)
        output["resume_probe"] = embed(
            endpoint, token, "The quick brown fox jumps over the lazy dog."
        )
        output["state_after_probe"] = microvm_state(args.region, args.microvm_id)

        for tokens, repeats in TOKEN_TARGETS_TO_HELLO_REPEATS.items():
            text = "hello " * repeats
            samples = [
                embed(endpoint, token, text) for _ in range(args.iterations)
            ]
            wall = summary([sample["wall_ms"] for sample in samples])
            server_encode_values = [
                float(sample["server_encode_ms"])
                for sample in samples
                if sample["server_encode_ms"] is not None
            ]
            server_encode = (
                summary(server_encode_values) if server_encode_values else None
            )
            result = {
                "target_model_tokens": tokens,
                "hello_repeats": repeats,
                "characters": len(text),
                "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "iterations": args.iterations,
                "samples": samples,
                "wall": wall,
                "server_encode": server_encode,
                "server_encode_samples": len(server_encode_values),
                "wall_tokens_per_second": round(
                    tokens * 1000 / wall["mean_ms"], 3
                ),
                "encode_tokens_per_second": (
                    round(tokens * 1000 / server_encode["mean_ms"], 3)
                    if server_encode is not None
                    else None
                ),
                "dimensions": samples[0]["dimensions"],
            }
            results[str(tokens)] = result
            print(json.dumps({"progress": result}), file=sys.stderr, flush=True)
    except Exception as error:
        benchmark_error = error
    finally:
        try:
            aws_json(
                args.region,
                "suspend-microvm",
                "--microvm-identifier",
                args.microvm_id,
            )
            output["final_state"] = wait_for_state(
                args.region, args.microvm_id, "SUSPENDED"
            )
        except Exception as error:
            cleanup_error = error

    output["status"] = "ok" if not benchmark_error and not cleanup_error else "error"
    if benchmark_error:
        output["benchmark_error"] = repr(benchmark_error)
    if cleanup_error:
        output["cleanup_error"] = repr(cleanup_error)
    print(json.dumps(output, indent=2))
    if benchmark_error or cleanup_error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
