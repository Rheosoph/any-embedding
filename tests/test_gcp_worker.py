"""GCP worker readiness and request handling tests without model downloads."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

import httpx
import numpy as np

from app.gcp import worker


class GcpWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.model = Mock()
        self.model.encode.return_value = np.array([[0.25, -0.5]])
        self.model_patch = patch.object(worker, "get_model", return_value=self.model)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.name_patch = patch.object(worker, "MODEL_NAME", "Alibaba-NLP/gte-multilingual-base")
        self.name_patch.start()
        self.addCleanup(self.name_patch.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker.app), base_url="http://worker"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_health_responds_during_blocking_input_preparation_and_encode(self) -> None:
        # Event handshakes test overlap rather than assuming inference takes a
        # particular amount of time on the machine running the test.
        normalize = worker._normalize_inputs
        loop_thread = threading.get_ident()

        for stage in ("normalization", "encode"):
            with self.subTest(stage=stage):
                entered = threading.Event()
                release = threading.Event()
                finished = threading.Event()
                work_threads: list[int] = []

                def block() -> None:
                    work_threads.append(threading.get_ident())
                    entered.set()
                    # Bound the wait so an inline-execution regression fails
                    # the test instead of deadlocking its own event loop.
                    release.wait(timeout=5)
                    finished.set()

                def prepare(raw_input):
                    if stage == "normalization":
                        block()
                    return normalize(raw_input)

                def encode(*args, **kwargs):
                    if stage == "encode":
                        block()
                    return np.array([[0.25, -0.5]])

                self.model.encode.side_effect = encode
                with patch.object(worker, "_normalize_inputs", side_effect=prepare):
                    request = asyncio.create_task(
                        self.client.post(
                            "/embed", json={"model": "gte-multilingual-base", "input": "hello"}
                        )
                    )
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                        response = await asyncio.wait_for(self.client.get("/health"), timeout=2)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(
                            response.json(),
                            {"status": "ok", "model": "Alibaba-NLP/gte-multilingual-base"},
                        )
                        self.assertFalse(finished.is_set(), "health must respond while work is blocked")
                        self.assertFalse(request.done())
                        self.assertNotEqual(work_threads, [loop_thread])
                    finally:
                        release.set()
                        result = await asyncio.wait_for(request, timeout=3)
                    self.assertEqual(result.status_code, 200)

    async def test_embedding_values_order_usage_and_configured_batch_size(self) -> None:
        vectors = [[0.25, -0.5], [0.75, 0.125], [-0.25, 1.0]]
        self.model.encode.return_value = np.array(vectors)

        with patch.object(worker, "MODEL_BATCH_SIZE", 7):
            response = await self.client.post(
                "/embed",
                json={
                    "model": "gte-multilingual-base",
                    "input": ["hello world", {"type": "text", "text": "guten Morgen"}, "bonjour"],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "embedding": vector, "index": index}
                    for index, vector in enumerate(vectors)
                ],
                "model": "gte-multilingual-base",
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            },
        )
        self.model.encode.assert_called_once_with(
            ["hello world", "guten Morgen", "bonjour"],
            batch_size=7,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

    async def test_enabled_warmup_finishes_before_startup_yields(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        ready = asyncio.Event()
        work_threads: list[int] = []
        loop_thread = threading.get_ident()

        def warmup(*args, **kwargs):
            work_threads.append(threading.get_ident())
            entered.set()
            release.wait(timeout=5)
            finished.set()
            return np.array([[0.25, -0.5]])

        async def start() -> None:
            async with worker.lifespan(worker.app):
                self.assertTrue(finished.is_set())
                ready.set()

        self.model.encode.side_effect = warmup
        with patch.object(worker, "MODEL_WARMUP", True), patch.object(worker, "MODEL_TYPE", "text"):
            startup = asyncio.create_task(start())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertFalse(ready.is_set(), "startup must await warmup before becoming ready")
                self.assertFalse(startup.done())
                self.assertFalse(finished.is_set())
                self.assertNotEqual(work_threads, [loop_thread])
            finally:
                release.set()
                await asyncio.wait_for(startup, timeout=3)

        self.assertTrue(ready.is_set())
        self.model.encode.assert_called_once_with(
            ["Embedding warmup"], batch_size=1, normalize_embeddings=True, show_progress_bar=False
        )

    async def test_failed_warmup_prevents_startup_readiness(self) -> None:
        self.model.encode.side_effect = RuntimeError("warmup failed")
        ready = False

        with patch.object(worker, "MODEL_WARMUP", True):
            with self.assertRaisesRegex(RuntimeError, "warmup failed"):
                async with worker.lifespan(worker.app):
                    ready = True

        self.assertFalse(ready)
        self.model.encode.assert_called_once()

    async def test_disabled_warmup_loads_model_without_encoding(self) -> None:
        with patch.object(worker, "MODEL_WARMUP", False):
            async with worker.lifespan(worker.app):
                worker.get_model.assert_called_once_with()
                self.model.encode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
