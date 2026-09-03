from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("benchmark.py")
MODULE_SPEC = importlib.util.spec_from_file_location(
    "function_url_benchmark",
    MODULE_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
benchmark = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = benchmark
MODULE_SPEC.loader.exec_module(benchmark)


class ArgumentTests(unittest.TestCase):
    def test_worker_pool_precondition_is_required(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "AWS_GATEWAY_URL": (
                        "https://example.lambda-url.eu-west-1.on.aws/"
                    ),
                    "API_KEY": "not-printed",
                },
                clear=True,
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            benchmark.parse_arguments([])

        self.assertIn("worker-pool-precondition", stderr.getvalue())
        self.assertNotIn("not-printed", stderr.getvalue())

    def test_legacy_aws_url_env_infers_ireland_and_defaults_to_three_long_runs(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "AWS_GATEWAY_URL": (
                    "https://example.lambda-url.eu-west-1.on.aws/"
                ),
                "API_KEY": "not-printed",
                "BENCHMARK_WORKER_POOL_PRECONDITION": "empty-verified",
            },
            clear=True,
        ):
            args, api_key = benchmark.parse_arguments([])

        self.assertEqual(args.url, "https://example.lambda-url.eu-west-1.on.aws")
        self.assertEqual(args.endpoint_label, "aws")
        self.assertEqual(args.region, "eu-west-1")
        self.assertEqual(args.region_source, "function_url")
        self.assertEqual(args.long_repeats, 3)
        self.assertEqual(api_key, "not-printed")

    def test_generic_endpoint_accepts_explicit_comparison_metadata(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GCP_BENCHMARK_KEY": "also-not-printed",
            },
            clear=True,
        ):
            args, api_key = benchmark.parse_arguments(
                [
                    "--url",
                    "https://embedding.example.run.app/",
                    "--endpoint-label",
                    "gcp-europe-west1",
                    "--region",
                    "europe-west1",
                    "--api-key-env",
                    "GCP_BENCHMARK_KEY",
                    "--worker-pool-precondition",
                    "not-forced-unknown",
                ]
            )

        self.assertEqual(args.url, "https://embedding.example.run.app")
        self.assertEqual(args.endpoint_label, "gcp-europe-west1")
        self.assertEqual(args.region, "europe-west1")
        self.assertEqual(args.region_source, "argument")
        self.assertEqual(api_key, "also-not-printed")

    def test_environment_region_is_recorded_for_generic_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BENCHMARK_ENDPOINT_URL": "https://embedding.example.run.app",
                "BENCHMARK_ENDPOINT_LABEL": "gcp-live",
                "BENCHMARK_REGION": "europe-west1",
                "API_KEY": "not-printed",
                "BENCHMARK_WORKER_POOL_PRECONDITION": "running-verified",
            },
            clear=True,
        ):
            args, _ = benchmark.parse_arguments([])

        self.assertEqual(args.region, "europe-west1")
        self.assertEqual(args.region_source, "environment")

    def test_conflicting_aws_region_is_rejected(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "API_KEY": "not-printed",
                    "BENCHMARK_WORKER_POOL_PRECONDITION": "empty-verified",
                },
                clear=True,
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            benchmark.parse_arguments(
                [
                    "--url",
                    "https://example.lambda-url.eu-west-1.on.aws",
                    "--region",
                    "eu-central-1",
                ]
            )

        self.assertIn("does not match", stderr.getvalue())
        self.assertNotIn("not-printed", stderr.getvalue())

    def test_invalid_api_key_env_name_is_not_echoed(self) -> None:
        mistaken_secret = "secret-value-that-is-not-an-env-name"
        stderr = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {"BENCHMARK_WORKER_POOL_PRECONDITION": "empty-verified"},
                clear=True,
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            benchmark.parse_arguments(
                [
                    "--url",
                    "https://example.lambda-url.eu-west-1.on.aws",
                    "--api-key-env-var",
                    mistaken_secret,
                ]
            )

        self.assertNotIn(mistaken_secret, stderr.getvalue())


class OutputTests(unittest.TestCase):
    def test_http_error_does_not_echo_a_reflected_key(self) -> None:
        secret = "reflected-bearer-key"
        error = benchmark.HTTPError(
            "https://embedding.example.run.app/v1/models",
            401,
            "Unauthorized",
            {},
            io.BytesIO(json.dumps({"error": secret}).encode()),
        )
        client = benchmark.Gateway(
            "https://embedding.example.run.app",
            secret,
            30,
        )

        with (
            patch.object(benchmark, "urlopen", side_effect=error),
            self.assertRaises(RuntimeError) as raised,
        ):
            client.json("GET", "/v1/models")

        self.assertEqual(
            str(raised.exception),
            "GET /v1/models returned HTTP 401",
        )
        self.assertNotIn(secret, str(raised.exception))

    def test_result_has_comparison_metadata_without_api_key(self) -> None:
        secret = "super-secret-benchmark-key"

        class FakeGateway:
            def __init__(self, url: str, api_key: str, timeout: float) -> None:
                self.url = url.rstrip("/")
                self.api_key = api_key
                self.timeout = timeout

            def json(
                self,
                method: str,
                path: str,
                payload: object = None,
            ) -> tuple[object, float, int]:
                del method, payload
                if path == "/health":
                    return {"status": "ok"}, 1.0, 15
                if path == "/v1/models":
                    return {
                        "data": [{"id": "gte-multilingual-base"}]
                    }, 2.0, 40
                raise AssertionError(f"unexpected path: {path}")

            def embed(self, inputs: str | list[str]) -> dict[str, object]:
                item_count = len(inputs) if isinstance(inputs, list) else 1
                return {
                    "wall_ms": 10.0,
                    "response_bytes": 100,
                    "items": item_count,
                    "dimensions": 768,
                }

        concurrent_result = {
            "concurrency": 1,
            "aggregate_wall_ms": 10.0,
            "requests_per_second": 100.0,
            "embeddings_per_second": 100.0,
            "request_latency": {
                "min_ms": 10.0,
                "mean_ms": 10.0,
                "p50_ms": 10.0,
                "p95_ms": 10.0,
                "max_ms": 10.0,
            },
            "samples": [],
        }
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"SAFE_KEY": secret}, clear=True),
            patch.object(benchmark, "Gateway", FakeGateway),
            patch.object(
                benchmark,
                "concurrent_burst",
                return_value=concurrent_result,
            ),
            patch("sys.stdout", stdout),
        ):
            benchmark.main(
                [
                    "--url",
                    "https://embedding.example.run.app",
                    "--endpoint-label",
                    "gcp-live",
                    "--region",
                    "europe-west1",
                    "--api-key-env",
                    "SAFE_KEY",
                    "--worker-pool-precondition",
                    "not-forced-unknown",
                    "--warm-iterations",
                    "1",
                    "--long-repeats",
                    "1",
                    "--concurrency",
                    "1",
                    "--concurrent-repeats",
                    "1",
                ]
            )

        serialized = stdout.getvalue()
        result = json.loads(serialized)
        self.assertNotIn(secret, serialized)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(
            result["endpoint"],
            {
                "label": "gcp-live",
                "region": "europe-west1",
                "region_source": "argument",
                "base_url": "https://embedding.example.run.app",
            },
        )
        self.assertEqual(result["region"], "europe-west1")
        self.assertEqual(result["method"]["long_repeats"], 1)
        self.assertEqual(
            result["method"]["worker_pool_precondition"],
            "not-forced-unknown",
        )
        self.assertNotIn("gcp_reference", result)
        self.assertNotIn("speedup_vs_saved_gcp", result["short"])


if __name__ == "__main__":
    unittest.main()
