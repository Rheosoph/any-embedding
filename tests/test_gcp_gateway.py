"""GCP forwarding deadlines, client lifecycle, and metadata token caching."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import httpx
from fastapi import Request
from fastapi.testclient import TestClient

from app.gcp import gateway


MODEL = "gte-multilingual-base"
WORKER_URL = "https://worker.example"
API_KEY = "private-consumer-key"
INPUT_TEXT = "private-embedding-input"
RESPONSE = {
    "object": "list",
    "data": [{"object": "embedding", "embedding": [0.25, -0.5], "index": 0}],
    "model": MODEL,
    "usage": {"prompt_tokens": 1, "total_tokens": 1},
}


def token(expiry: float, audience: str = WORKER_URL) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expiry, "aud": audience}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.private-signature"


class IDTokenProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, handler) -> gateway._IDTokenProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return gateway._IDTokenProvider(client)

    async def test_cache_is_isolated_by_audience(self) -> None:
        requests = []

        def metadata(request):
            requests.append(request)
            return httpx.Response(
                200, text=token(time.time() + 3600, request.url.params["audience"])
            )

        provider = self.provider(metadata)
        first = await provider.get_token("https://one.example")
        second = await provider.get_token("https://two.example")
        self.assertNotEqual(first, second)
        self.assertEqual(await provider.get_token("https://one.example"), first)
        self.assertEqual(await provider.get_token("https://two.example"), second)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r.headers["Metadata-Flavor"] == "Google" for r in requests))

    async def test_refreshes_before_expiry(self) -> None:
        now = time.time()
        responses = [token(now + 120), token(now + 7200)]
        calls = []

        def metadata(request):
            calls.append(request)
            return httpx.Response(200, text=responses[len(calls) - 1])

        provider = self.provider(metadata)
        with patch.object(gateway.time, "time", return_value=now):
            self.assertEqual(await provider.get_token(WORKER_URL), responses[0])
        with patch.object(gateway.time, "time", return_value=now + 59):
            self.assertEqual(await provider.get_token(WORKER_URL), responses[0])
        with patch.object(gateway.time, "time", return_value=now + 60):
            self.assertEqual(await provider.get_token(WORKER_URL), responses[1])
        self.assertEqual(len(calls), 2)

    async def test_concurrent_refreshes_are_coalesced(self) -> None:
        calls = []
        expected = token(time.time() + 3600)

        async def metadata(request):
            calls.append(request)
            await asyncio.sleep(0.01)
            return httpx.Response(200, text=expected)

        provider = self.provider(metadata)
        results = await asyncio.gather(*(provider.get_token(WORKER_URL) for _ in range(20)))
        self.assertEqual(results, [expected] * 20)
        self.assertEqual(len(calls), 1)

    async def test_metadata_failure_backoff_expires_and_preserves_cached_tokens(self) -> None:
        calls = []
        expected = token(time.time() + 3600)

        def metadata(request):
            calls.append(request)
            if len(calls) == 2:
                raise httpx.ConnectError("metadata unavailable", request=request)
            return httpx.Response(200, text=expected)

        provider = self.provider(metadata)
        self.assertEqual(await provider.get_token(WORKER_URL), expected)
        self.assertIsNone(await provider.get_token("https://second.example"))
        self.assertIsNone(await provider.get_token("https://third.example"))
        self.assertEqual(await provider.get_token(WORKER_URL), expected)
        self.assertEqual(len(calls), 2)
        provider.metadata_unavailable_until = time.monotonic() - 1
        self.assertEqual(await provider.get_token("https://second.example"), expected)
        self.assertEqual(len(calls), 3)

    async def test_metadata_http_failure_is_temporarily_cached(self) -> None:
        calls = []

        def metadata(request):
            calls.append(request)
            return httpx.Response(503)

        provider = self.provider(metadata)
        self.assertIsNone(await provider.get_token(WORKER_URL))
        self.assertIsNone(await provider.get_token(WORKER_URL))
        self.assertEqual(len(calls), 1)

    async def test_uncacheable_token_is_never_reused(self) -> None:
        for invalid_token in ("not-a-jwt", token(time.time() - 1), token(float("inf"))):
            with self.subTest(token=invalid_token):
                calls = []

                def metadata(request):
                    calls.append(request)
                    return httpx.Response(200, text=invalid_token)

                provider = self.provider(metadata)
                self.assertEqual(await provider.get_token(WORKER_URL), invalid_token)
                self.assertEqual(await provider.get_token(WORKER_URL), invalid_token)
                self.assertEqual(len(calls), 2)

    async def test_cache_hit_is_logged_at_debug_and_refresh_at_info(self) -> None:
        provider = self.provider(
            lambda request: httpx.Response(200, text=token(time.time() + 3600))
        )
        with self.assertLogs(gateway.logger, "DEBUG") as logs:
            await provider.get_token(WORKER_URL)
            await provider.get_token(WORKER_URL)
        records = [r for r in logs.records if r.msg.startswith("id_token_lookup")]
        self.assertEqual(
            [(r.outcome, r.levelno) for r in records],
            [("refreshed", logging.INFO), ("cache_hit", logging.DEBUG)],
        )


class GatewayForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clients = []
        self.client_kwargs = []
        self.worker_requests = []
        self.metadata_requests = []
        self.id_token = token(time.time() + 3600)
        self.worker_handler = lambda request: httpx.Response(200, json=RESPONSE)
        self.metadata_handler = lambda request: httpx.Response(200, text=self.id_token)
        gateway._request_log.clear()

    @contextmanager
    def client(self, timeout="120", registry=None, prefetch_timeout=None):
        async_client = httpx.AsyncClient
        registry = registry if registry is not None else {MODEL: {"worker_url": WORKER_URL}}

        async def worker_transport(request):
            self.worker_requests.append(request)
            result = self.worker_handler(request)
            return await result if asyncio.iscoroutine(result) else result

        async def metadata_transport(request):
            self.metadata_requests.append(request)
            result = self.metadata_handler(request)
            return await result if asyncio.iscoroutine(result) else result

        def make_client(**kwargs):
            transport = worker_transport if not self.clients else metadata_transport
            client = async_client(transport=httpx.MockTransport(transport), **kwargs)
            self.clients.append(client)
            self.client_kwargs.append(kwargs)
            return client

        with ExitStack() as stack:
            stack.enter_context(patch.object(gateway, "API_KEY", API_KEY))
            stack.enter_context(patch.dict("os.environ", {"WORKER_TIMEOUT_SECONDS": timeout}))
            stack.enter_context(patch.object(
                gateway, "_load_model_registry", return_value=registry
            ))
            stack.enter_context(patch.object(gateway.httpx, "AsyncClient", side_effect=make_client))
            if prefetch_timeout is not None:
                stack.enter_context(patch.object(
                    gateway, "_TOKEN_PREFETCH_TIMEOUT_SECONDS", prefetch_timeout
                ))
            yield stack.enter_context(TestClient(gateway.app))

    def post(self, client, headers=None, **body):
        return client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {API_KEY}", **(headers or {})},
            json={"model": MODEL, "input": INPUT_TEXT, **body},
        )

    def test_clients_and_token_are_reused_and_clients_close_on_shutdown(self) -> None:
        with self.client() as client:
            first = self.post(client)
            second = self.post(client)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json(), RESPONSE)
            self.assertEqual(second.json(), RESPONSE)
            self.assertEqual(len(self.clients), 2)
            self.assertIs(gateway.app.state.worker_client, self.clients[0])
            self.assertIs(gateway.app.state.id_token_provider.client, self.clients[1])
            self.assertTrue(all(not c.is_closed for c in self.clients))
        self.assertTrue(all(c.is_closed for c in self.clients))
        self.assertEqual(len(self.metadata_requests), 1)
        self.assertEqual(len(self.worker_requests), 2)
        for request in self.worker_requests:
            self.assertEqual(str(request.url), f"{WORKER_URL}/embed")
            self.assertEqual(request.headers["Authorization"], f"Bearer {self.id_token}")
            self.assertEqual(json.loads(request.content)["input"], INPUT_TEXT)

    def test_worker_client_keeps_connections_warm_and_bounds_connect_time(self) -> None:
        with self.client(timeout="90"):
            pass
        worker_kwargs, metadata_kwargs = self.client_kwargs
        limits = worker_kwargs["limits"]
        self.assertEqual(limits.max_connections, 200)
        self.assertEqual(limits.max_keepalive_connections, 100)
        self.assertEqual(limits.keepalive_expiry, 300.0)
        timeout = worker_kwargs["timeout"]
        self.assertEqual(timeout.connect, 10.0)
        self.assertEqual((timeout.read, timeout.write, timeout.pool), (90.0, 90.0, 90.0))
        self.assertEqual(metadata_kwargs, {"timeout": 5.0})

    def test_metadata_unavailable_still_allows_local_workers_without_repeated_lookup(self) -> None:
        def unavailable(request):
            raise httpx.ConnectError("metadata not available", request=request)

        self.metadata_handler = unavailable
        with self.client() as client:
            self.assertEqual(self.post(client).status_code, 200)
            self.assertEqual(self.post(client).status_code, 200)
        # The startup prefetch consumes the single attempt; requests back off.
        self.assertEqual(len(self.metadata_requests), 1)
        self.assertTrue(all("Authorization" not in r.headers for r in self.worker_requests))

    def test_transport_failure_returns_502_with_no_sensitive_details(self) -> None:
        def unavailable(request):
            raise httpx.ConnectError(f"{API_KEY} {INPUT_TEXT} {self.id_token}", request=request)

        self.worker_handler = unavailable
        with self.client() as client, self.assertLogs(gateway.logger, "INFO") as logs:
            response = self.post(client)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "Embedding worker unavailable"})
        self.assertEqual(len(self.worker_requests), 1)
        self.assert_logs_are_safe(logs.records)

    def test_worker_httpx_timeout_returns_504(self) -> None:
        def timed_out(request):
            raise httpx.ReadTimeout("worker stalled", request=request)

        self.worker_handler = timed_out
        with self.client() as client:
            response = self.post(client)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json(), {"detail": "Embedding worker timed out"})
        self.assertEqual(len(self.worker_requests), 1)

    def test_total_deadline_cancels_stalled_worker(self) -> None:
        async def stalled(request):
            await asyncio.sleep(60)

        self.worker_handler = stalled
        with self.client(timeout="0.02") as client:
            response = self.post(client)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(len(self.worker_requests), 1)

    def test_total_deadline_includes_metadata_and_releases_refresh_lock(self) -> None:
        async def metadata(request):
            # Stall the startup prefetch and the first forwarded request.
            if len(self.metadata_requests) <= 2:
                await asyncio.sleep(60)
            return httpx.Response(200, text=self.id_token)

        self.metadata_handler = metadata
        with self.client(timeout="0.02", prefetch_timeout=0.02) as client:
            self.assertEqual(len(self.metadata_requests), 1)
            response = self.post(client)
            self.assertEqual(response.status_code, 504)
            self.assertEqual(len(self.worker_requests), 0)
            self.assertEqual(self.post(client).status_code, 200)
        self.assertEqual(len(self.metadata_requests), 3)
        self.assertEqual(len(self.worker_requests), 1)

    def test_worker_http_error_preserves_existing_status_and_detail(self) -> None:
        self.worker_handler = lambda request: httpx.Response(503, text="worker warming")
        with self.client() as client:
            response = self.post(client)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "worker warming"})

    def test_worker_4xx_body_is_mapped_into_detail(self) -> None:
        self.worker_handler = lambda request: httpx.Response(
            400, json={"error": {"message": "bad input"}}
        )
        with self.client() as client:
            response = self.post(client)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": '{"error":{"message":"bad input"}}'})

    def test_worker_body_is_passed_through_byte_for_byte(self) -> None:
        # Not valid for EmbeddingResponse (base64 embedding, extra key, odd
        # whitespace): the gateway must not parse or re-serialize it.
        body = (
            b'{"object":"list","data":[{"object":"embedding",'
            b'"embedding":"AACAPwAAAMA=","index":0}],"model":"' + MODEL.encode() +
            b'",  "usage":{"prompt_tokens":1,"total_tokens":1},"extra":null}\n'
        )
        self.worker_handler = lambda request: httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/json; charset=utf-8",
                "content-encoding": "identity",
                "transfer-encoding": "chunked",
                "x-worker": "internal",
            },
        )
        with self.client() as client:
            response = self.post(client, encoding_format="base64")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, body)
        self.assertEqual(response.headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(response.headers["content-length"], str(len(body)))
        self.assertNotIn("content-encoding", response.headers)
        self.assertNotIn("transfer-encoding", response.headers)
        self.assertNotIn("x-worker", response.headers)
        self.assertEqual(json.loads(self.worker_requests[0].content)["encoding_format"], "base64")

    def test_passthrough_defaults_to_json_media_type(self) -> None:
        body = b'{"object":"list","data":[],"model":"m","usage":{"prompt_tokens":0,"total_tokens":0}}'
        self.worker_handler = lambda request: httpx.Response(200, content=body)
        with self.client() as client:
            response = self.post(client)
        self.assertNotIn("content-type", self.worker_handler(None).headers)
        self.assertEqual(response.content, body)
        self.assertEqual(response.headers["content-type"], "application/json")

    def test_startup_prefetches_worker_tokens(self) -> None:
        registry = {
            MODEL: {"worker_url": WORKER_URL + "/"},
            "other": {"worker_url": "https://other.example"},
        }
        self.metadata_handler = lambda request: httpx.Response(
            200, text=token(time.time() + 3600, request.url.params["audience"])
        )
        with self.assertLogs(gateway.logger, "INFO") as logs, self.client(registry=registry) as client:
            audiences = sorted(r.url.params["audience"] for r in self.metadata_requests)
            self.assertEqual(audiences, ["https://other.example", WORKER_URL])
            self.assertEqual(
                set(gateway.app.state.id_token_provider.tokens), set(audiences)
            )
            self.assertEqual(self.post(client).status_code, 200)
        self.assertEqual(len(self.metadata_requests), 2)
        token_logs = [r for r in logs.records if r.msg.startswith("id_token_lookup")]
        self.assertEqual([r.outcome for r in token_logs], ["refreshed", "refreshed"])
        prefetch_logs = [r.getMessage() for r in logs.records if r.msg.startswith("id_token_prefetch")]
        self.assertEqual(len(prefetch_logs), 1)
        self.assertIn("outcome=done audiences=2", prefetch_logs[0])
        self.assert_logs_are_safe(logs.records)

    def test_startup_prefetch_timeout_does_not_block_startup_or_requests(self) -> None:
        async def metadata(request):
            if len(self.metadata_requests) == 1:
                await asyncio.sleep(60)
            return httpx.Response(200, text=self.id_token)

        self.metadata_handler = metadata
        started = time.monotonic()
        with self.assertLogs(gateway.logger, "INFO") as logs, self.client(prefetch_timeout=0.02) as client:
            self.assertLess(time.monotonic() - started, 5.0)
            self.assertEqual(self.post(client).status_code, 200)
            self.assertEqual(self.post(client).status_code, 200)
        self.assertEqual(len(self.metadata_requests), 2)
        self.assertEqual(self.worker_requests[0].headers["Authorization"], f"Bearer {self.id_token}")
        prefetch_logs = [r for r in logs.records if r.msg.startswith("id_token_prefetch")]
        self.assertEqual([r.levelno for r in prefetch_logs], [logging.WARNING])
        self.assertIn("outcome=timeout", prefetch_logs[0].getMessage())

    def test_startup_prefetch_is_skipped_without_workers(self) -> None:
        with self.client(registry={}):
            pass
        self.assertEqual(self.metadata_requests, [])

    def test_rate_limit_keys_on_rightmost_forwarded_for_entry(self) -> None:
        with (
            patch.object(gateway, "RATE_LIMIT_RPM", 2),
            patch.object(gateway, "TRUST_X_FORWARDED_FOR", True),
            self.client() as client,
        ):
            first = {"X-Forwarded-For": "203.0.113.1"}
            second = {"X-Forwarded-For": "203.0.113.2"}
            self.assertEqual(self.post(client, headers=first).status_code, 200)
            self.assertEqual(self.post(client, headers=first).status_code, 200)
            self.assertEqual(self.post(client, headers=first).status_code, 429)
            # A different client behind the same proxy peer is independent.
            self.assertEqual(self.post(client, headers=second).status_code, 200)
            # Client-supplied leading entries cannot dodge the limit.
            spoofed = {"X-Forwarded-For": "198.51.100.9, 203.0.113.1"}
            self.assertEqual(self.post(client, headers=spoofed).status_code, 429)
            # Without the header the TCP peer remains the key.
            self.assertEqual(self.post(client).status_code, 200)
            self.assertEqual(self.post(client).status_code, 200)
            self.assertEqual(self.post(client).status_code, 429)
        self.assertEqual(sorted(gateway._request_log), ["203.0.113.1", "203.0.113.2", "testclient"])

    def test_audit_log_message_carries_fields(self) -> None:
        with (
            patch.object(gateway, "TRUST_X_FORWARDED_FOR", True),
            self.client() as client,
            self.assertLogs(gateway.audit_logger, "INFO") as logs,
        ):
            self.post(client, headers={"X-Forwarded-For": "10.1.1.1, 203.0.113.7"})
            self.post(client, model="missing")
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(messages, [
            f"api_request client_ip=203.0.113.7 method=POST path=/v1/embeddings model={MODEL} status=200",
            "api_request client_ip=testclient method=POST path=/v1/embeddings model=missing status=400",
        ])
        self.assert_logs_are_safe(logs.records)

    def test_timing_logs_do_not_include_input_credentials_or_tokens(self) -> None:
        with self.client() as client, self.assertLogs(gateway.logger, "DEBUG") as logs:
            self.assertEqual(self.post(client).status_code, 200)
            self.assertEqual(self.post(client).status_code, 200)
        token_logs = [r for r in logs.records if r.msg.startswith("id_token_lookup")]
        forward_logs = [r for r in logs.records if r.msg.startswith("worker_forward")]
        # The refresh happened during startup; both requests hit the cache.
        self.assertEqual([r.outcome for r in token_logs], ["cache_hit", "cache_hit"])
        self.assertTrue(all(r.levelno == logging.DEBUG for r in token_logs))
        self.assertEqual(len(forward_logs), 2)
        self.assertTrue(all(r.levelno == logging.INFO for r in forward_logs))
        self.assertTrue(all(r.elapsed_ms >= 0 for r in logs.records))
        self.assertTrue(all(r.worker_elapsed_ms >= 0 for r in forward_logs))
        self.assertTrue(all("elapsed_ms=" in r.getMessage() for r in logs.records))
        self.assertIn("outcome=cache_hit", token_logs[1].getMessage())
        self.assert_logs_are_safe(logs.records)

    def assert_logs_are_safe(self, records) -> None:
        serialized = repr([record.__dict__ for record in records])
        for sensitive in (API_KEY, INPUT_TEXT, self.id_token):
            self.assertNotIn(sensitive, serialized)


class ClientIdentityTests(unittest.TestCase):
    @staticmethod
    def request(headers: dict[str, str] | None = None, client=("10.0.0.9", 4321)) -> Request:
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/embeddings",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": client,
        })

    def test_forwarded_for_is_ignored_without_a_trusted_proxy(self) -> None:
        # The default: exposed directly (docker compose, plain uvicorn) the
        # header is caller-supplied, so the TCP peer stays the key.
        self.assertFalse(gateway.TRUST_X_FORWARDED_FOR)
        request = self.request({"X-Forwarded-For": "203.0.113.1"})
        self.assertEqual(gateway._client_ip(request), "10.0.0.9")

    def test_rightmost_forwarded_for_entry_wins(self) -> None:
        cases = {
            "203.0.113.1": "203.0.113.1",
            "198.51.100.9, 203.0.113.1": "203.0.113.1",
            "198.51.100.9,203.0.113.1 ": "203.0.113.1",
            " 2001:db8::1 ": "2001:db8::1",
        }
        for header, expected in cases.items():
            with self.subTest(header=header), patch.object(gateway, "TRUST_X_FORWARDED_FOR", True):
                self.assertEqual(
                    gateway._client_ip(self.request({"X-Forwarded-For": header})), expected
                )

    def test_falls_back_to_peer_then_unknown(self) -> None:
        with patch.object(gateway, "TRUST_X_FORWARDED_FOR", True):
            self.assertEqual(gateway._client_ip(self.request()), "10.0.0.9")
            self.assertEqual(gateway._client_ip(self.request({"X-Forwarded-For": " "})), "10.0.0.9")
            self.assertEqual(gateway._client_ip(self.request(client=None)), "unknown")


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        gateway._request_log.clear()
        gateway._request_log_swept_at = 0.0

    def check(self, client_ip: str, now: float) -> int | None:
        with patch.object(gateway.time, "monotonic", return_value=now):
            try:
                gateway._check_rate_limit(client_ip)
            except gateway.HTTPException as exc:
                return exc.status_code
        return None

    def test_expired_timestamps_are_popped_from_the_left(self) -> None:
        with patch.object(gateway, "RATE_LIMIT_RPM", 3):
            self.assertIsNone(self.check("a", 1000.0))
            self.assertIsNone(self.check("a", 1010.0))
            self.assertIsNone(self.check("a", 1020.0))
            self.assertEqual(self.check("a", 1059.0), 429)
            self.assertEqual(list(gateway._request_log["a"]), [1000.0, 1010.0, 1020.0])
            # The entry from t=1000 has aged out; the others remain in order.
            self.assertIsNone(self.check("a", 1060.0))
            self.assertEqual(list(gateway._request_log["a"]), [1010.0, 1020.0, 1060.0])
            self.assertIsInstance(gateway._request_log["a"], gateway.deque)

    def test_idle_clients_are_dropped_once_per_window(self) -> None:
        with patch.object(gateway, "RATE_LIMIT_RPM", 10):
            self.assertIsNone(self.check("idle", 1000.0))
            self.assertIsNone(self.check("busy", 1001.0))
            # Within the sweep window idle keys are kept even when expired.
            self.assertIsNone(self.check("busy", 1059.5))
            self.assertIn("idle", gateway._request_log)
            self.assertIsNone(self.check("busy", 1061.0))
            self.assertEqual(sorted(gateway._request_log), ["busy"])
            self.assertEqual(list(gateway._request_log["busy"]), [1059.5, 1061.0])

    def test_zero_limit_leaves_no_empty_keys(self) -> None:
        with patch.object(gateway, "RATE_LIMIT_RPM", 0):
            self.assertEqual(self.check("a", 1000.0), 429)
        self.assertEqual(dict(gateway._request_log), {})


class WorkerTimeoutConfigurationTests(unittest.TestCase):
    def test_default_is_120_seconds(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(gateway._worker_timeout_seconds(), 120.0)

    def test_positive_finite_values_are_accepted(self) -> None:
        for value in ("0.05", "30", "180.5"):
            with self.subTest(value=value), patch.dict("os.environ", {"WORKER_TIMEOUT_SECONDS": value}):
                self.assertEqual(gateway._worker_timeout_seconds(), float(value))

    def test_invalid_values_fail_at_startup(self) -> None:
        for value in ("0", "-1", "NaN", "inf", "-inf", "", "invalid"):
            with self.subTest(value=value), patch.dict("os.environ", {"WORKER_TIMEOUT_SECONDS": value}):
                with self.assertRaisesRegex(ValueError, "must be a finite positive number"):
                    with TestClient(gateway.app):
                        self.fail("Invalid timeout must prevent startup")


if __name__ == "__main__":
    unittest.main()
