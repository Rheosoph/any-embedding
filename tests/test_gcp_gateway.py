"""GCP forwarding deadlines, client lifecycle, and metadata token caching."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import httpx
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


class GatewayForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clients = []
        self.worker_requests = []
        self.metadata_requests = []
        self.id_token = token(time.time() + 3600)
        self.worker_handler = lambda request: httpx.Response(200, json=RESPONSE)
        self.metadata_handler = lambda request: httpx.Response(200, text=self.id_token)
        gateway._request_log.clear()

    @contextmanager
    def client(self, timeout="120"):
        async_client = httpx.AsyncClient

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
            return client

        with ExitStack() as stack:
            stack.enter_context(patch.object(gateway, "API_KEY", API_KEY))
            stack.enter_context(patch.dict("os.environ", {"WORKER_TIMEOUT_SECONDS": timeout}))
            stack.enter_context(patch.object(
                gateway, "_load_model_registry", return_value={MODEL: {"worker_url": WORKER_URL}}
            ))
            stack.enter_context(patch.object(gateway.httpx, "AsyncClient", side_effect=make_client))
            yield stack.enter_context(TestClient(gateway.app))

    def post(self, client):
        return client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": MODEL, "input": INPUT_TEXT},
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

    def test_metadata_unavailable_still_allows_local_workers_without_repeated_lookup(self) -> None:
        def unavailable(request):
            raise httpx.ConnectError("metadata not available", request=request)

        self.metadata_handler = unavailable
        with self.client() as client:
            self.assertEqual(self.post(client).status_code, 200)
            self.assertEqual(self.post(client).status_code, 200)
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
            if len(self.metadata_requests) == 1:
                await asyncio.sleep(60)
            return httpx.Response(200, text=self.id_token)

        self.metadata_handler = metadata
        with self.client(timeout="0.02") as client:
            response = self.post(client)
            self.assertEqual(response.status_code, 504)
            self.assertEqual(len(self.worker_requests), 0)
            self.assertEqual(self.post(client).status_code, 200)
        self.assertEqual(len(self.metadata_requests), 2)
        self.assertEqual(len(self.worker_requests), 1)

    def test_worker_http_error_preserves_existing_status_and_detail(self) -> None:
        self.worker_handler = lambda request: httpx.Response(503, text="worker warming")
        with self.client() as client:
            response = self.post(client)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "worker warming"})

    def test_timing_logs_do_not_include_input_credentials_or_tokens(self) -> None:
        with self.client() as client, self.assertLogs(gateway.logger, "INFO") as logs:
            self.assertEqual(self.post(client).status_code, 200)
            self.assertEqual(self.post(client).status_code, 200)
        token_logs = [r for r in logs.records if r.msg.startswith("id_token_lookup")]
        forward_logs = [r for r in logs.records if r.msg.startswith("worker_forward")]
        self.assertEqual([r.outcome for r in token_logs], ["refreshed", "cache_hit"])
        self.assertEqual(len(forward_logs), 2)
        self.assertTrue(all(r.elapsed_ms >= 0 for r in logs.records))
        self.assertTrue(all(r.worker_elapsed_ms >= 0 for r in forward_logs))
        self.assertTrue(all("elapsed_ms=" in r.getMessage() for r in logs.records))
        self.assertIn("outcome=cache_hit", token_logs[1].getMessage())
        self.assert_logs_are_safe(logs.records)

    def assert_logs_are_safe(self, records) -> None:
        serialized = repr([record.__dict__ for record in records])
        for sensitive in (API_KEY, INPUT_TEXT, self.id_token):
            self.assertNotIn(sensitive, serialized)


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
