"""Launch, benchmark, and leave suspended one Lambda MicroVM via AWS CLI."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from typing import Any
from urllib.request import Request, urlopen


EXACT_GCP_TEST_TEXT = "The quick brown fox jumps over the lazy dog."


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


def optional_benchmark_metric(
    samples: list[dict[str, Any]], metric: str
) -> tuple[dict[str, float] | None, int]:
    """Summarize optional private worker telemetry without requiring it."""

    values: list[float] = []
    for sample in samples:
        benchmark = sample.get("benchmark")
        if not isinstance(benchmark, dict):
            continue
        value = benchmark.get(metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(float(value))
    return (summarize(values) if values else None, len(values))


class AwsCli:
    def __init__(self, region: str) -> None:
        self.region = region

    def call(self, *arguments: str) -> dict[str, Any]:
        command = [
            "aws",
            "lambda-microvms",
            *arguments,
            "--region",
            self.region,
            "--output",
            "json",
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return json.loads(result.stdout) if result.stdout.strip() else {}


def wait_for_state(
    aws: AwsCli,
    microvm_id: str,
    expected_state: str,
    timeout_seconds: float = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = aws.call("get-microvm", "--microvm-identifier", microvm_id)
        if last.get("state") == expected_state:
            return last
        if last.get("state") in {"TERMINATED", "TERMINATING"}:
            raise RuntimeError(f"MicroVM entered {last.get('state')}: {last}")
        time.sleep(0.25)
    raise TimeoutError(
        f"MicroVM did not reach {expected_state}; last state: {last.get('state')}"
    )


def create_auth_token(aws: AwsCli, microvm_id: str) -> str:
    response = aws.call(
        "create-microvm-auth-token",
        "--microvm-identifier",
        microvm_id,
        "--expiration-in-minutes",
        "60",
        "--allowed-ports",
        '[{"allPorts":{}}]',
    )
    return response["authToken"]["X-aws-proxy-auth"]


def embed(endpoint: str, token: str, inputs: str | list[str]) -> dict[str, Any]:
    payload = json.dumps(
        {"model": "gte-multilingual-base", "input": inputs}, separators=(",", ":")
    ).encode()
    request = Request(
        f"https://{endpoint.rstrip('/')}/v1/embeddings",
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
    with urlopen(request, timeout=180) as response:
        body = json.loads(response.read())
    latency_ms = (time.perf_counter() - started) * 1000
    data = body.get("data") if isinstance(body, dict) else None
    expected_items = len(inputs) if isinstance(inputs, list) else 1
    if not isinstance(data, list) or len(data) != expected_items:
        actual_items = len(data) if isinstance(data, list) else None
        raise RuntimeError(
            f"unexpected embedding item count: {actual_items}; "
            f"expected {expected_items}"
        )
    dimensions = [
        len(item.get("embedding", [])) if isinstance(item, dict) else 0
        for item in data
    ]
    if any(dimension != 768 for dimension in dimensions):
        raise RuntimeError(f"unexpected embedding dimensions: {dimensions}")
    raw_benchmark = body.get("_benchmark")
    benchmark = raw_benchmark if isinstance(raw_benchmark, dict) else {}
    return {
        "wall_ms": latency_ms,
        "dimensions": dimensions[0] if dimensions else 768,
        "items": len(data),
        "benchmark": benchmark,
    }


def ensure_not_running(aws: AwsCli, microvm_id: str) -> dict[str, Any]:
    """Leave a launched benchmark VM suspended (or accept terminal state)."""

    current = aws.call("get-microvm", "--microvm-identifier", microvm_id)
    state = current.get("state")
    if state in {"TERMINATED", "TERMINATING", "SUSPENDED"}:
        return current
    if state == "SUSPENDING":
        return wait_for_state(aws, microvm_id, "SUSPENDED")
    if state in {"PENDING", "RESUMING"}:
        wait_for_state(aws, microvm_id, "RUNNING")
    aws.call("suspend-microvm", "--microvm-identifier", microvm_id)
    return wait_for_state(aws, microvm_id, "SUSPENDED")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-arn", required=True)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--warm-iterations", type=int, default=20)
    parser.add_argument("--batch-repeats", type=int, default=5)
    parser.add_argument("--runtime-log-group")
    parser.add_argument("--execution-role-arn")
    args = parser.parse_args()

    if args.runtime_log_group and not args.execution_role_arn:
        parser.error("--runtime-log-group requires --execution-role-arn")

    aws = AwsCli(args.region)
    ingress = (
        f"arn:aws:lambda:{args.region}:aws:network-connector:"
        "aws-network-connector:ALL_INGRESS"
    )
    run_arguments = [
        "run-microvm",
        "--image-identifier",
        args.image_arn,
        "--ingress-network-connectors",
        ingress,
        "--idle-policy",
        '{"autoResumeEnabled":true,"maxIdleDurationSeconds":900,'
        '"suspendedDurationSeconds":28800}',
    ]
    if args.runtime_log_group:
        run_arguments.extend(
            [
                "--logging",
                json.dumps({"cloudWatch": {"logGroup": args.runtime_log_group}}),
            ]
        )
    if args.execution_role_arn:
        run_arguments.extend(
            ["--execution-role-arn", args.execution_role_arn]
        )

    launch_started_wall = time.time()
    output: dict[str, Any] = {
        "benchmark_started_unix_seconds": launch_started_wall,
        "region": args.region,
        "image_arn": args.image_arn,
        "gcp_reference": {
            "first_ms": 36979,
            "second_ms": 248,
            "delta_ms": 36731,
            "dimensions": 768,
        },
    }
    microvm_id: str | None = None
    benchmark_error: Exception | None = None
    cleanup_error: Exception | None = None

    try:
        launch_started = time.perf_counter()
        run_response = aws.call(*run_arguments)
        run_api_ms = (time.perf_counter() - launch_started) * 1000
        raw_microvm_id = run_response.get("microvmId")
        microvm_id = raw_microvm_id if isinstance(raw_microvm_id, str) else None
        if not microvm_id:
            raise RuntimeError("run-microvm response did not include microvmId")
        endpoint = run_response.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise RuntimeError("run-microvm response did not include endpoint")
        output.update(
            {
                "microvm_id": microvm_id,
                "endpoint": endpoint,
                "image_version": run_response.get("imageVersion"),
            }
        )

        # Token creation is independent of application readiness and avoids adding
        # its latency after the MicroVM is already RUNNING.
        token = create_auth_token(aws, microvm_id)
        wait_for_state(aws, microvm_id, "RUNNING")
        launch_to_running_ms = (time.perf_counter() - launch_started) * 1000

        first = embed(endpoint, token, EXACT_GCP_TEST_TEXT)
        launch_to_first_embedding_ms = (
            time.perf_counter() - launch_started
        ) * 1000
        second = embed(endpoint, token, EXACT_GCP_TEST_TEXT)
        output["launch"] = {
            "run_api_ms": round(run_api_ms, 3),
            "launch_to_running_ms": round(launch_to_running_ms, 3),
            "launch_to_first_embedding_ms": round(
                launch_to_first_embedding_ms, 3
            ),
        }
        output["first"] = {
            "wall_ms": round(first["wall_ms"], 3),
            "dimensions": first["dimensions"],
            "server": first["benchmark"],
        }
        output["second"] = {
            "wall_ms": round(second["wall_ms"], 3),
            "dimensions": second["dimensions"],
            "server": second["benchmark"],
        }

        warm_results = [
            embed(endpoint, token, EXACT_GCP_TEST_TEXT)
            for _ in range(args.warm_iterations)
        ]
        warm_wall_values = [float(result["wall_ms"]) for result in warm_results]
        warm_server_encode, warm_server_samples = optional_benchmark_metric(
            warm_results, "encode_ms"
        )
        output["warm"] = {
            **summarize(warm_wall_values),
            "requests_per_second_at_mean": round(
                1000 / statistics.fmean(warm_wall_values), 3
            ),
            "server_encode": warm_server_encode,
            "server_encode_samples": warm_server_samples,
        }

        batches: dict[str, Any] = {}
        for batch_size in (1, 8, 32):
            results = [
                embed(endpoint, token, [EXACT_GCP_TEST_TEXT] * batch_size)
                for _ in range(args.batch_repeats)
            ]
            wall_values = [float(result["wall_ms"]) for result in results]
            server_encode, server_samples = optional_benchmark_metric(
                results, "encode_ms"
            )
            wall_summary = summarize(wall_values)
            wall_summary["embeddings_per_second_at_mean"] = round(
                batch_size * 1000 / statistics.fmean(wall_values), 3
            )
            batches[str(batch_size)] = {
                "wall": wall_summary,
                "server_encode": server_encode,
                "server_encode_samples": server_samples,
            }
        output["batches"] = batches

        aws.call("suspend-microvm", "--microvm-identifier", microvm_id)
        wait_for_state(aws, microvm_id, "SUSPENDED")
        resume_started = time.perf_counter()
        resume_result = embed(endpoint, token, EXACT_GCP_TEST_TEXT)
        output["resume"] = {
            "resume_to_embedding_ms": round(
                (time.perf_counter() - resume_started) * 1000, 3
            ),
            "http_wall_ms": round(resume_result["wall_ms"], 3),
            "server": resume_result["benchmark"],
        }
    except Exception as error:
        benchmark_error = error
    finally:
        if microvm_id:
            try:
                final_state = ensure_not_running(aws, microvm_id)
                output["final_state"] = final_state.get("state")
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
