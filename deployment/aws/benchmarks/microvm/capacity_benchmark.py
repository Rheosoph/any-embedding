"""Benchmark one Lambda MicroVM capacity tier and leave it suspended."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from typing import Any

from benchmark import (
    AwsCli,
    EXACT_GCP_TEST_TEXT,
    create_auth_token,
    embed,
    ensure_not_running,
    optional_benchmark_metric,
    summarize,
    wait_for_state,
)


LONG_INPUTS = {
    512: "hello " * 255,
    2048: "hello " * 1023,
    4096: "hello " * 2047,
    8192: "hello " * 4095,
}


def measured_block(
    endpoint: str,
    token: str,
    inputs: str | list[str],
    iterations: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    samples = [embed(endpoint, token, inputs) for _ in range(iterations)]
    wall_values = [float(sample["wall_ms"]) for sample in samples]
    server_encode, server_samples = optional_benchmark_metric(samples, "encode_ms")
    return samples, {
        "wall": summarize(wall_values),
        "server_encode": server_encode,
        "server_encode_samples": server_samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-arn", required=True)
    parser.add_argument("--image-version", default="1.0")
    parser.add_argument("--baseline-memory-mib", type=int, required=True)
    parser.add_argument("--expected-torch-threads", type=int, required=True)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--warm-iterations", type=int, default=20)
    parser.add_argument("--batch-repeats", type=int, default=5)
    parser.add_argument("--long-repeats", type=int, default=3)
    args = parser.parse_args()

    if min(args.warm_iterations, args.batch_repeats, args.long_repeats) < 1:
        parser.error("all repeat counts must be at least 1")
    supported_memory_mib = {512, 1024, 2048, 4096, 8192}
    if args.baseline_memory_mib not in supported_memory_mib:
        parser.error(
            f"--baseline-memory-mib must be one of {sorted(supported_memory_mib)}"
        )
    expected_peak_threads = args.baseline_memory_mib // 512
    if args.expected_torch_threads != expected_peak_threads:
        parser.error(
            "--expected-torch-threads must match the tier's peak vCPU count "
            f"({expected_peak_threads})"
        )

    aws = AwsCli(args.region)
    image_configuration = aws.call(
        "get-microvm-image-version",
        "--image-identifier",
        args.image_arn,
        "--image-version",
        args.image_version,
    )
    configured_memory_mib = image_configuration.get("resources", [{}])[0].get(
        "minimumMemoryInMiB"
    )
    configured_threads = image_configuration.get("environmentVariables", {}).get(
        "TORCH_NUM_THREADS"
    )
    if image_configuration.get("state") != "SUCCESSFUL":
        parser.error("the selected image version is not SUCCESSFUL")
    if image_configuration.get("status") != "ACTIVE":
        parser.error("the selected image version is not ACTIVE")
    if configured_memory_mib != args.baseline_memory_mib:
        parser.error(
            f"image has {configured_memory_mib} MiB, not {args.baseline_memory_mib}"
        )
    if configured_threads != str(args.expected_torch_threads):
        parser.error(
            f"image has TORCH_NUM_THREADS={configured_threads}, not "
            f"{args.expected_torch_threads}"
        )

    ingress = (
        f"arn:aws:lambda:{args.region}:aws:network-connector:"
        "aws-network-connector:ALL_INGRESS"
    )
    output: dict[str, Any] = {
        "region": args.region,
        "image_arn": args.image_arn,
        "image_version": args.image_version,
        "capacity": {
            "baseline_memory_mib": args.baseline_memory_mib,
            "baseline_vcpu": args.baseline_memory_mib / 2048,
            "peak_memory_mib": args.baseline_memory_mib * 4,
            "peak_vcpu": args.baseline_memory_mib / 512,
            "expected_torch_threads": args.expected_torch_threads,
        },
        "method": {
            "warm_iterations": args.warm_iterations,
            "batch_repeats": args.batch_repeats,
            "long_repeats": args.long_repeats,
            "sequential_requests": True,
            "long_input_tokenizer_validation": (
                "Pinned tokenizer confirmed exact counts including special tokens"
            ),
        },
        "verified_image_configuration": {
            "state": image_configuration.get("state"),
            "status": image_configuration.get("status"),
            "resources": image_configuration.get("resources"),
            "cpu_configurations": image_configuration.get("cpuConfigurations"),
            "environment_variables": image_configuration.get(
                "environmentVariables"
            ),
        },
    }
    microvm_id: str | None = None
    benchmark_error: Exception | None = None
    cleanup_error: Exception | None = None

    try:
        client_token = str(uuid.uuid4())
        launch_started = time.perf_counter()
        run_response = aws.call(
            "run-microvm",
            "--image-identifier",
            args.image_arn,
            "--image-version",
            args.image_version,
            "--ingress-network-connectors",
            ingress,
            "--idle-policy",
            '{"autoResumeEnabled":true,"maxIdleDurationSeconds":900,'
            '"suspendedDurationSeconds":28800}',
            "--maximum-duration-in-seconds",
            "28800",
            "--client-token",
            client_token,
        )
        run_api_ms = (time.perf_counter() - launch_started) * 1000
        microvm_id = run_response["microvmId"]
        endpoint = run_response["endpoint"]
        output.update(
            {
                "microvm_id": microvm_id,
                "endpoint": endpoint,
                "run_response_image_version": run_response.get("imageVersion"),
                "run_response_started_at": run_response.get("startedAt"),
                "created_resources": [
                    {
                        "type": "AWS::Lambda::MicroVM",
                        "id": microvm_id,
                        "retention": (
                            "left suspended; service maximum lifetime is 28800 seconds"
                        ),
                    }
                ],
            }
        )
        if run_response.get("imageVersion") != args.image_version:
            raise RuntimeError(
                "run response selected unexpected image version: "
                f"{run_response.get('imageVersion')}"
            )

        wait_for_state(aws, microvm_id, "RUNNING")
        launch_to_running_ms = (time.perf_counter() - launch_started) * 1000
        token_started = time.perf_counter()
        token = create_auth_token(aws, microvm_id)
        auth_token_ms = (time.perf_counter() - token_started) * 1000

        first = embed(endpoint, token, EXACT_GCP_TEST_TEXT)
        launch_to_first_embedding_ms = (
            time.perf_counter() - launch_started
        ) * 1000
        second = embed(endpoint, token, EXACT_GCP_TEST_TEXT)
        server = first["benchmark"]
        observed_torch_threads = server.get("torch_threads")
        if (
            observed_torch_threads is not None
            and observed_torch_threads != args.expected_torch_threads
        ):
            raise RuntimeError(
                "unexpected torch thread count: "
                f"{observed_torch_threads} != {args.expected_torch_threads}"
            )

        output["launch"] = {
            "run_api_ms": round(run_api_ms, 3),
            "launch_to_running_ms": round(launch_to_running_ms, 3),
            "auth_token_after_running_ms": round(auth_token_ms, 3),
            "launch_to_first_embedding_ms": round(
                launch_to_first_embedding_ms, 3
            ),
        }
        output["first"] = first
        output["second"] = second
        output["server_observed"] = {
            "telemetry_available": bool(server),
            "architecture": server.get("architecture"),
            "cpu_count": server.get("cpu_count"),
            "torch_threads": server.get("torch_threads"),
            "model_load_ms_before_snapshot": server.get(
                "model_load_ms_before_snapshot"
            ),
            "warmup_ms_before_snapshot": server.get(
                "warmup_ms_before_snapshot"
            ),
            "boot_to_ready_ms_before_snapshot": server.get(
                "boot_to_ready_ms_before_snapshot"
            ),
        }

        warm_samples, warm = measured_block(
            endpoint,
            token,
            EXACT_GCP_TEST_TEXT,
            args.warm_iterations,
        )
        warm["requests_per_second_at_mean"] = round(
            1000 / statistics.fmean(
                [float(sample["wall_ms"]) for sample in warm_samples]
            ),
            3,
        )
        warm["samples"] = warm_samples
        output["warm"] = warm
        print(json.dumps({"progress": "warm complete"}), file=sys.stderr)

        batches: dict[str, Any] = {}
        for batch_size in (1, 8, 32):
            samples, result = measured_block(
                endpoint,
                token,
                [EXACT_GCP_TEST_TEXT] * batch_size,
                args.batch_repeats,
            )
            result["embeddings_per_second_at_mean"] = round(
                batch_size * 1000
                / statistics.fmean(
                    [float(sample["wall_ms"]) for sample in samples]
                ),
                3,
            )
            result["samples"] = samples
            batches[str(batch_size)] = result
            print(
                json.dumps({"progress": f"batch {batch_size} complete"}),
                file=sys.stderr,
            )
        output["batches"] = batches

        long_inputs: dict[str, Any] = {}
        for token_count, text in LONG_INPUTS.items():
            samples, result = measured_block(
                endpoint, token, text, args.long_repeats
            )
            mean_wall_ms = statistics.fmean(
                [float(sample["wall_ms"]) for sample in samples]
            )
            server_encode = result["server_encode"]
            mean_encode_ms = (
                float(server_encode["mean_ms"])
                if isinstance(server_encode, dict)
                else None
            )
            result.update(
                {
                    "tokens": token_count,
                    "characters": len(text),
                    "wall_tokens_per_second": round(
                        token_count * 1000 / mean_wall_ms, 3
                    ),
                    "encode_tokens_per_second": (
                        round(token_count * 1000 / mean_encode_ms, 3)
                        if mean_encode_ms is not None
                        else None
                    ),
                    "samples": samples,
                }
            )
            long_inputs[str(token_count)] = result
            print(
                json.dumps({"progress": f"long {token_count} complete"}),
                file=sys.stderr,
                flush=True,
            )
        output["long_inputs"] = long_inputs

        aws.call("suspend-microvm", "--microvm-identifier", microvm_id)
        wait_for_state(aws, microvm_id, "SUSPENDED")
        resume_started = time.perf_counter()
        resume_result = embed(endpoint, token, EXACT_GCP_TEST_TEXT)
        output["resume"] = {
            "resume_to_embedding_ms": round(
                (time.perf_counter() - resume_started) * 1000, 3
            ),
            "result": resume_result,
        }
    except Exception as error:
        benchmark_error = error
    finally:
        if microvm_id:
            try:
                final = ensure_not_running(aws, microvm_id)
                output["final_state"] = final.get("state")
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
