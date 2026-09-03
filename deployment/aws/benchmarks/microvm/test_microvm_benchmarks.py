from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


MICROVM_BENCHMARK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MICROVM_BENCHMARK_DIR))

import benchmark  # noqa: E402
import capacity_benchmark  # noqa: E402
import long_input_benchmark  # noqa: E402


def openai_response(*, telemetry: dict[str, object] | None = None) -> bytes:
    body: dict[str, object] = {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "embedding": [0.0] * 768,
                "index": 0,
            }
        ],
        "model": "gte-multilingual-base",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    if telemetry is not None:
        body["_benchmark"] = telemetry
    return json.dumps(body).encode()


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class OptionalTelemetryTests(unittest.TestCase):
    def test_primary_embed_accepts_standard_response_without_telemetry(self) -> None:
        with patch.object(
            benchmark,
            "urlopen",
            return_value=FakeHttpResponse(openai_response()),
        ):
            result = benchmark.embed("worker.example", "token", "hello")

        self.assertEqual(result["dimensions"], 768)
        self.assertEqual(result["items"], 1)
        self.assertEqual(result["benchmark"], {})

    def test_optional_metric_reports_zero_samples_when_absent(self) -> None:
        result, sample_count = benchmark.optional_benchmark_metric(
            [{"benchmark": {}}, {"wall_ms": 1.0}], "encode_ms"
        )

        self.assertIsNone(result)
        self.assertEqual(sample_count, 0)

    def test_capacity_block_keeps_wall_metrics_without_telemetry(self) -> None:
        sample = {
            "wall_ms": 25.0,
            "dimensions": 768,
            "items": 1,
            "benchmark": {},
        }
        with patch.object(capacity_benchmark, "embed", return_value=sample):
            _, result = capacity_benchmark.measured_block(
                "worker.example", "token", "hello", 2
            )

        self.assertEqual(result["wall"]["mean_ms"], 25.0)
        self.assertIsNone(result["server_encode"])
        self.assertEqual(result["server_encode_samples"], 0)

    def test_long_input_embed_accepts_standard_response_without_telemetry(self) -> None:
        with patch.object(
            long_input_benchmark,
            "urlopen",
            return_value=FakeHttpResponse(openai_response()),
        ):
            result = long_input_benchmark.embed(
                "https://worker.example", "token", "hello"
            )

        self.assertEqual(result["dimensions"], 768)
        self.assertIsNone(result["server_encode_ms"])
        self.assertIsNone(result["request_number"])


class CleanupTests(unittest.TestCase):
    def test_primary_benchmark_suspends_vm_after_probe_failure(self) -> None:
        class FakeAwsCli:
            instance: FakeAwsCli | None = None

            def __init__(self, _region: str) -> None:
                self.state = "RUNNING"
                self.calls: list[tuple[str, ...]] = []
                FakeAwsCli.instance = self

            def call(self, *arguments: str) -> dict[str, object]:
                self.calls.append(arguments)
                operation = arguments[0]
                if operation == "run-microvm":
                    return {
                        "microvmId": "microvm-test",
                        "endpoint": "worker.example",
                        "imageVersion": "1.0",
                    }
                if operation == "create-microvm-auth-token":
                    return {"authToken": {"X-aws-proxy-auth": "token"}}
                if operation == "get-microvm":
                    return {"state": self.state}
                if operation == "suspend-microvm":
                    self.state = "SUSPENDED"
                    return {}
                raise AssertionError(f"unexpected AWS operation: {operation}")

        argv = [
            "benchmark.py",
            "--image-arn",
            "arn:aws:lambda:eu-west-1:123456789012:microvm-image:test",
            "--warm-iterations",
            "1",
            "--batch-repeats",
            "1",
        ]
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(benchmark, "AwsCli", FakeAwsCli),
            patch.object(benchmark, "embed", side_effect=RuntimeError("probe failed")),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit),
        ):
            benchmark.main()

        instance = FakeAwsCli.instance
        self.assertIsNotNone(instance)
        assert instance is not None
        self.assertIn(
            ("suspend-microvm", "--microvm-identifier", "microvm-test"),
            instance.calls,
        )
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "error")
        self.assertEqual(output["final_state"], "SUSPENDED")
        self.assertIn("probe failed", output["benchmark_error"])


if __name__ == "__main__":
    unittest.main()
