"""GCP worker readiness and request handling tests without model downloads."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import types
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import numpy as np
import torch

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
        # asyncio primitives bind to the loop they first block on; every test
        # runs on a fresh loop, so start each one without a semaphore.
        self.semaphore_patch = patch.object(worker, "_encode_semaphore", None)
        self.semaphore_patch.start()
        self.addCleanup(self.semaphore_patch.stop)
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
        self.assertEqual(response.headers["content-type"], "application/json")
        expected = {
            "object": "list",
            "data": [
                {"object": "embedding", "embedding": vector, "index": index}
                for index, vector in enumerate(vectors)
            ],
            "model": "gte-multilingual-base",
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        }
        self.assertEqual(response.json(), expected)
        # Key order and compact separators match the former FastAPI/pydantic output.
        self.assertEqual(response.content, json.dumps(expected, separators=(",", ":")).encode())
        self.model.encode.assert_called_once_with(
            ["hello world", "guten Morgen", "bonjour"],
            batch_size=7,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_tensor=True,
        )

    async def test_float32_model_output_round_trips_exactly(self) -> None:
        rng = np.random.default_rng(7)
        vectors = rng.standard_normal((4, 64)).astype(np.float32)
        vectors[0, :3] = [1e-5, 3.25e-6, -7.5e-9]
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        self.model.encode.return_value = vectors

        response = await self.client.post(
            "/embed", json={"model": "gte-multilingual-base", "input": ["a"] * 4}
        )

        self.assertEqual(response.status_code, 200)
        received = [item["embedding"] for item in response.json()["data"]]
        # Every value must equal the float64 widening of the model's float32
        # output, exactly as tolist() produced before.
        self.assertEqual(received, vectors.tolist())

    async def test_torch_tensor_and_list_outputs_are_accepted(self) -> None:
        for output in (torch.tensor([[0.25, -0.5]]), [[0.25, -0.5]]):
            with self.subTest(output=type(output).__name__):
                self.model.encode.return_value = output
                response = await self.client.post(
                    "/embed", json={"model": "gte-multilingual-base", "input": "hello"}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"][0]["embedding"], [0.25, -0.5])

    async def test_base64_encoding_format_returns_little_endian_float32(self) -> None:
        vectors = np.array([[0.1, -0.2, 0.3], [1.0, 0.0, -1.0]], dtype=np.float32)
        self.model.encode.return_value = vectors

        response = await self.client.post(
            "/embed",
            json={"model": "gte-multilingual-base", "input": ["a", "b"], "encoding_format": "base64"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(list(payload), ["object", "data", "model", "usage"])
        for index, item in enumerate(payload["data"]):
            self.assertEqual(list(item), ["object", "embedding", "index"])
            self.assertEqual(item["index"], index)
            self.assertIsInstance(item["embedding"], str)
            decoded = np.frombuffer(base64.b64decode(item["embedding"]), dtype="<f4")
            np.testing.assert_array_equal(decoded, vectors[index])
        self.assertEqual(payload["usage"], {"prompt_tokens": 2, "total_tokens": 2})

    async def test_invalid_encoding_format_is_rejected_by_request_validation(self) -> None:
        # The EmbeddingRequest validator runs before the handler, so the worker
        # boundary answers with FastAPI's 422 rather than the 400 error body.
        response = await self.client.post(
            "/embed",
            json={"model": "gte-multilingual-base", "input": "hello", "encoding_format": "int8"},
        )

        self.assertEqual(response.status_code, 422)
        errors = response.json()["detail"]
        self.assertEqual(errors[0]["loc"], ["body", "encoding_format"])
        self.assertIn("encoding_format must be 'float' or 'base64'", errors[0]["msg"])
        self.model.encode.assert_not_called()

    def test_serializer_rejects_unknown_encoding_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "encoding_format must be 'float' or 'base64'"):
            worker._serialize_response(np.zeros((1, 2)), "m", "int8", 0)

    async def test_value_error_during_input_preparation_returns_400_error_body(self) -> None:
        response = await self.client.post(
            "/embed", json={"model": "gte-multilingual-base", "input": {"type": "image"}}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(
            response.json(),
            {"error": {"message": "Image input requires 'image' field", "type": "invalid_request_error"}},
        )
        self.model.encode.assert_not_called()

    async def test_value_error_during_encode_returns_400_and_other_errors_propagate(self) -> None:
        self.model.encode.side_effect = ValueError("sequence too long")
        response = await self.client.post(
            "/embed", json={"model": "gte-multilingual-base", "input": "hello"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["message"], "sequence too long")

        self.model.encode.side_effect = RuntimeError("cuda out of memory")
        with self.assertRaisesRegex(RuntimeError, "cuda out of memory"):
            await self.client.post("/embed", json={"model": "gte-multilingual-base", "input": "hello"})

    async def test_encode_semaphore_limits_parallel_encodes_while_input_prep_overlaps(self) -> None:
        normalize = worker._normalize_inputs
        lock = threading.Lock()
        prepared: list[str] = []
        encoded: list[list[str]] = []
        first_encoding = threading.Event()
        second_prepared = threading.Event()
        release = threading.Event()

        def prepare(raw_input):
            with lock:
                prepared.append(raw_input)
                if len(prepared) == 2:
                    second_prepared.set()
            return normalize(raw_input)

        def encode(inputs, **kwargs):
            with lock:
                encoded.append(inputs)
            first_encoding.set()
            release.wait(timeout=5)
            return np.array([[0.25, -0.5]])

        self.model.encode.side_effect = encode
        with (
            patch.object(worker, "WORKER_ENCODE_PARALLELISM", 1),
            patch.object(worker, "_normalize_inputs", side_effect=prepare),
        ):
            first = asyncio.create_task(
                self.client.post("/embed", json={"model": "m", "input": "first"})
            )
            try:
                self.assertTrue(await asyncio.to_thread(first_encoding.wait, 3))
                second = asyncio.create_task(
                    self.client.post("/embed", json={"model": "m", "input": "second"})
                )
                # Input preparation of the second request proceeds while the
                # first one is inside encode ...
                self.assertTrue(await asyncio.to_thread(second_prepared.wait, 3))
                await asyncio.sleep(0.2)
                # ... but its encode waits for the semaphore.
                self.assertEqual(encoded, [["first"]])
                self.assertFalse(second.done())
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(encoded, [["first"], ["second"]])

    async def test_encode_parallelism_above_one_lets_encodes_overlap(self) -> None:
        # A barrier that both encodes must reach: if the semaphore wrongly
        # serialized them, the barrier times out and the requests fail.
        barrier = threading.Barrier(2, timeout=3)

        def encode(inputs, **kwargs):
            barrier.wait()
            return np.array([[0.25, -0.5]])

        self.model.encode.side_effect = encode
        with patch.object(worker, "WORKER_ENCODE_PARALLELISM", 2):
            responses = await asyncio.wait_for(
                asyncio.gather(
                    self.client.post("/embed", json={"model": "m", "input": "first"}),
                    self.client.post("/embed", json={"model": "m", "input": "second"}),
                ),
                timeout=5,
            )

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(self.model.encode.call_count, 2)

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
            ["Embedding warmup"], batch_size=1, normalize_embeddings=True, show_progress_bar=False,
            convert_to_tensor=True,
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

    async def test_prefetch_reads_model_directory_and_manifest_and_logs_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp, "model")
            Path(model_dir, "sub").mkdir(parents=True)
            Path(model_dir, "weights.bin").write_bytes(b"w" * 3000)
            Path(model_dir, "sub", "tokenizer.json").write_bytes(b"t" * 100)
            Path(model_dir, "config.json").write_bytes(b"{}")
            manifest = Path(tmp, "manifest.txt")
            manifest.write_text(
                f"{Path(model_dir, 'config.json')}\n"
                f"{Path(tmp, 'missing.pyc')}\n"
                "\n"
                f"{Path(model_dir, 'sub', 'tokenizer.json')}\n"
                f"{model_dir}\n"
            )

            with (
                patch.object(worker, "MODEL_PATH", str(model_dir)),
                patch.object(worker, "IMPORT_MANIFEST_PATH", str(manifest)),
                # weights (3000) fit, tokenizer (100) no longer does, config (2) still does.
                patch.object(worker, "MODEL_PREFETCH_MAX_BYTES", 3050),
                patch.object(worker, "_cgroup_memory_limit", return_value=None),
                patch.dict(worker._PREFETCH_STATS, worker._new_prefetch_stats()),
                patch.object(worker, "_PREFETCH_THREADS", []),
            ):
                threads = worker._start_prefetch()
                worker._PREFETCH_THREADS.extend(threads)
                self.assertEqual(sorted(thread.name for thread in threads), ["prefetch-imports", "prefetch-model"])
                for thread in threads:
                    self.assertTrue(thread.daemon)
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

                with self.assertLogs(worker.logger, level="INFO") as logs:
                    async with worker.lifespan(worker.app):
                        pass

                stats = worker._PREFETCH_STATS
                self.assertEqual(stats["model_files"], 2)
                self.assertEqual(stats["model_skipped"], 1)
                self.assertEqual(stats["model_bytes"], 3002)
                self.assertGreater(stats["model_ms"], 0)
                self.assertEqual(stats["manifest_files"], 2)
                self.assertEqual(stats["manifest_bytes"], 102)
                self.assertGreater(stats["manifest_ms"], 0)

        prefetch_lines = [line for line in logs.output if "model_prefetch " in line]
        self.assertEqual(len(prefetch_lines), 1)
        self.assertIn("model_files=2 model_skipped=1 model_bytes=3002", prefetch_lines[0])
        self.assertIn("manifest_files=2 manifest_bytes=102", prefetch_lines[0])
        self.assertIn("pending_threads=0", prefetch_lines[0])


class PrefetchConfigTests(unittest.TestCase):
    def test_budget_is_capped_by_cgroup_memory_limit(self) -> None:
        with patch.object(worker, "MODEL_PREFETCH_MAX_BYTES", 10**9):
            with patch.object(worker, "_cgroup_memory_limit", return_value=None):
                self.assertEqual(worker._prefetch_budget(), 10**9)
            with patch.object(worker, "_cgroup_memory_limit", return_value=5000):
                self.assertEqual(worker._prefetch_budget(), 3000)

    def test_cgroup_limit_ignores_unlimited_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            v2 = Path(tmp, "memory.max")
            v1 = Path(tmp, "memory.limit_in_bytes")
            with patch.object(worker, "_CGROUP_MEMORY_FILES", (str(v2), str(v1))):
                self.assertIsNone(worker._cgroup_memory_limit())
                v2.write_text("max\n")
                self.assertIsNone(worker._cgroup_memory_limit())
                v1.write_text("8589934592\n")
                self.assertEqual(worker._cgroup_memory_limit(), 8589934592)
                v2.write_text("4294967296\n")
                self.assertEqual(worker._cgroup_memory_limit(), 4294967296)

    def test_start_prefetch_without_model_dir_or_manifest_starts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(worker, "MODEL_PATH", str(Path(tmp, "absent"))),
                patch.object(worker, "IMPORT_MANIFEST_PATH", str(Path(tmp, "absent.txt"))),
            ):
                self.assertEqual(worker._start_prefetch(), [])

    def test_prefetch_never_raises_on_unreadable_manifest(self) -> None:
        stats = worker._new_prefetch_stats()
        with tempfile.TemporaryDirectory() as tmp:
            worker._prefetch_import_manifest(str(Path(tmp, "missing.txt")), stats)
            worker._prefetch_model_dir(str(Path(tmp, "missing")), 10**6, stats)
        self.assertEqual(stats["manifest_files"], 0)
        self.assertEqual(stats["model_files"], 0)

    def test_write_import_manifest_lists_existing_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp, "import_manifest.txt")
            count = worker.write_import_manifest(str(manifest))
            lines = manifest.read_text(encoding="utf-8").splitlines()

        self.assertEqual(count, len(lines))
        self.assertEqual(count, len(set(lines)), "manifest entries must be unique")
        self.assertGreater(count, 1000)
        for line in lines:
            self.assertTrue(os.path.isabs(line), line)
            self.assertTrue(os.path.isfile(line), line)
        worker_entry = worker.__cached__ if os.path.isfile(worker.__cached__) else worker.__file__
        self.assertIn(os.path.abspath(worker_entry), lines)
        model_sources = [
            line for line in lines
            if f"{os.sep}transformers{os.sep}models{os.sep}" in line and line.endswith(".py")
        ]
        self.assertGreater(len(model_sources), 100)
        # Imported modules come first (import order), the scanned sources last.
        self.assertIn(f"{os.sep}transformers{os.sep}models{os.sep}", lines[-1])

    def test_write_import_manifest_never_triggers_lazy_module_imports(self) -> None:
        # transformers registers lazy proxy modules whose __getattr__ imports a
        # submodule on any attribute miss; probing them would drag in optional
        # extras (torchvision) and fail the image build.
        probed: list[str] = []

        class LazyModule(types.ModuleType):
            def __getattr__(self, name: str):
                probed.append(name)
                raise ImportError(f"lazy import of {name} must never happen")

        lazy = LazyModule("fake_lazy_module")
        lazy.__file__ = worker.__file__
        with patch.dict(sys.modules, {"fake_lazy_module": lazy}):
            with tempfile.TemporaryDirectory() as tmp:
                manifest = Path(tmp, "import_manifest.txt")
                count = worker.write_import_manifest(str(manifest))

        self.assertEqual(probed, [], "lazy module attributes must not be read")
        self.assertGreater(count, 1000)


class EnvironmentParsingTests(unittest.TestCase):
    def test_integer_settings_reject_non_numeric_and_below_minimum_values(self) -> None:
        with patch.dict(os.environ, {"WORKER_CONCURRENCY": "four"}):
            with self.assertRaisesRegex(ValueError, "WORKER_CONCURRENCY must be an integer"):
                worker._env_int("WORKER_CONCURRENCY", "4", minimum=1)
        with patch.dict(os.environ, {"MODEL_BATCH_SIZE": "0"}):
            with self.assertRaisesRegex(ValueError, "MODEL_BATCH_SIZE must be at least 1"):
                worker._env_int("MODEL_BATCH_SIZE", "32", minimum=1)
        with patch.dict(os.environ, {"WORKER_CPU_LIMIT": "0"}):
            self.assertEqual(worker._env_int("WORKER_CPU_LIMIT", "0", minimum=0), 0)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(worker._env_int("MODEL_PREFETCH_MAX_BYTES", str(8 * 1024**3), 0), 8 * 1024**3)

    def test_boolean_and_choice_settings_reject_unknown_values(self) -> None:
        with patch.dict(os.environ, {"MODEL_PREFETCH": "maybe"}):
            with self.assertRaisesRegex(ValueError, "MODEL_PREFETCH must be true or false"):
                worker._env_bool("MODEL_PREFETCH", "true")
        with patch.dict(os.environ, {"MODEL_PREFETCH": "False"}):
            self.assertFalse(worker._env_bool("MODEL_PREFETCH", "true"))
        with patch.dict(os.environ, {"MODEL_DTYPE": "int8"}):
            with self.assertRaisesRegex(ValueError, "MODEL_DTYPE must be one of float32, float16, bfloat16"):
                worker._env_choice("MODEL_DTYPE", "float32", ("float32", "float16", "bfloat16"))
        with patch.dict(os.environ, {"MODEL_MATMUL_PRECISION": "High"}):
            self.assertEqual(
                worker._env_choice("MODEL_MATMUL_PRECISION", "highest", ("highest", "high", "medium")),
                "high",
            )

    def test_executor_is_sized_from_worker_concurrency(self) -> None:
        self.assertEqual(worker._executor._max_workers, max(2, worker.WORKER_CONCURRENCY))
        self.assertEqual(worker._executor._thread_name_prefix, "embed")


class FakeSentenceTransformer:
    """Stand-in whose constructor signature mirrors sentence-transformers 3+/5."""

    def __init__(self, source: str, *, trust_remote_code: bool, device: str, model_kwargs: dict | None = None):
        self.args = (source, trust_remote_code, device, model_kwargs)
        self.max_seq_length: int | None = 8192
        self.to = Mock()

    def parameters(self):
        return iter(())


class LegacySentenceTransformer(FakeSentenceTransformer):
    """Stand-in for sentence-transformers 2.7.0, which has no model_kwargs."""

    def __init__(self, source: str, *, trust_remote_code: bool, device: str):
        super().__init__(source, trust_remote_code=trust_remote_code, device=device)


class ModelLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        for target, value in (
            ("_model", None),
            ("MODEL_NAME", "test/model"),
            ("MODEL_DTYPE", "float32"),
            ("MODEL_MATMUL_PRECISION", "highest"),
            ("MODEL_MAX_SEQ_LENGTH", 0),
        ):
            patcher = patch.object(worker, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            patch.object(worker, "_resolve_model_source", return_value="/baked/model"),
            patch.object(worker.torch.cuda, "is_available", return_value=False),
            patch.object(worker.torch, "set_float32_matmul_precision"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_float32_default_leaves_constructor_and_dtype_untouched(self) -> None:
        with patch.object(worker, "SentenceTransformer", FakeSentenceTransformer):
            model = worker.get_model()

        self.assertEqual(model.args, ("/baked/model", True, "cpu", None))
        model.to.assert_not_called()
        self.assertEqual(model.max_seq_length, 8192)
        self.assertIs(worker.get_model(), model, "model is loaded once")

    def test_dtype_is_passed_via_model_kwargs_when_supported(self) -> None:
        with (
            patch.object(worker, "MODEL_DTYPE", "float16"),
            patch.object(worker, "SentenceTransformer", FakeSentenceTransformer),
        ):
            model = worker.get_model()

        self.assertEqual(model.args[3], {"torch_dtype": torch.float16})
        model.to.assert_not_called()

    def test_dtype_falls_back_to_module_cast_without_model_kwargs(self) -> None:
        with (
            patch.object(worker, "MODEL_DTYPE", "bfloat16"),
            patch.object(worker, "SentenceTransformer", LegacySentenceTransformer),
        ):
            model = worker.get_model()

        self.assertIsNone(model.args[3])
        model.to.assert_called_once_with(dtype=torch.bfloat16)

    def test_bfloat16_tensor_output_is_converted_by_the_worker(self) -> None:
        # sentence-transformers 2.7.0 cannot turn bfloat16 into numpy itself; the
        # worker asks for a tensor and does the float cast (torch refuses
        # bfloat16 .numpy() directly).
        class Bf16Model:
            def encode(self, inputs, **kwargs):
                assert kwargs["convert_to_tensor"] is True
                return torch.tensor([[0.25, -0.5]] * len(inputs), dtype=torch.bfloat16)

        with self.assertRaises(TypeError):
            torch.tensor([0.25], dtype=torch.bfloat16).numpy()
        embeddings = worker._encode(Bf16Model(), ["a", "b"], batch_size=32)
        self.assertEqual(embeddings.dtype, np.float32)
        self.assertEqual(embeddings.tolist(), [[0.25, -0.5], [0.25, -0.5]])

    def test_max_seq_length_is_only_ever_lowered(self) -> None:
        class Unlimited(FakeSentenceTransformer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.max_seq_length = None

        cases = [(512, FakeSentenceTransformer, 512), (16000, FakeSentenceTransformer, 8192), (512, Unlimited, None)]
        for cap, model_class, expected in cases:
            with self.subTest(cap=cap, model=model_class.__name__):
                with (
                    patch.object(worker, "_model", None),
                    patch.object(worker, "MODEL_MAX_SEQ_LENGTH", cap),
                    patch.object(worker, "SentenceTransformer", model_class),
                    self.assertLogs(worker.logger, level="INFO") as logs,
                ):
                    model = worker.get_model()
                self.assertEqual(model.max_seq_length, expected)
                self.assertTrue(any(
                    "Model loaded" in line and f"dtype=float32, max_seq_length={expected}" in line
                    for line in logs.output
                ))

    def test_matmul_precision_only_applies_on_cuda(self) -> None:
        with (
            patch.object(worker, "MODEL_MATMUL_PRECISION", "high"),
            patch.object(worker, "SentenceTransformer", FakeSentenceTransformer),
        ):
            worker.get_model()
            worker.torch.set_float32_matmul_precision.assert_not_called()

            worker._model = None
            with patch.object(worker.torch.cuda, "is_available", return_value=True):
                model = worker.get_model()
            worker.torch.set_float32_matmul_precision.assert_called_once_with("high")
            self.assertEqual(model.args[2], "cuda")


if __name__ == "__main__":
    unittest.main()
