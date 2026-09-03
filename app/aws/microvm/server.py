"""Snapshot-friendly, dynamically batched embedding server for Lambda MicroVMs."""

from __future__ import annotations

import json
import os
import platform
import queue
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_BOOT_STARTED = time.perf_counter()

import torch
from sentence_transformers import SentenceTransformer


MODEL_ID = os.environ.get("MODEL_ID", "Alibaba-NLP/gte-multilingual-base")
MODEL_API_NAME = os.environ.get("MODEL_API_NAME", "gte-multilingual-base")
MODEL_PATH = os.environ.get("MODEL_PATH", "/opt/model")
EXPECTED_DIMENSIONS = int(os.environ.get("EXPECTED_DIMENSIONS", "768"))
MAX_INPUT_CHARACTERS = int(os.environ.get("MAX_INPUT_CHARACTERS", "100000"))
MAX_BATCH_ITEMS = int(os.environ.get("MAX_BATCH_ITEMS", "2048"))
MODEL_BATCH_SIZE = int(os.environ.get("MODEL_BATCH_SIZE", "32"))
DYNAMIC_BATCH_WINDOW_MS = float(os.environ.get("DYNAMIC_BATCH_WINDOW_MS", "4"))
DYNAMIC_BATCH_MAX_ITEMS = int(os.environ.get("DYNAMIC_BATCH_MAX_ITEMS", "2048"))
JOB_TIMEOUT_SECONDS = float(os.environ.get("JOB_TIMEOUT_SECONDS", "840"))
WARMUP_TEXT = "The quick brown fox jumps over the lazy dog."
PORT = int(os.environ.get("PORT", "8080"))
TORCH_THREADS = int(os.environ.get("TORCH_NUM_THREADS", "4"))
MAX_BODY_BYTES = 6 * 1024 * 1024

torch.set_num_threads(TORCH_THREADS)
torch.set_num_interop_threads(1)

_MODEL_LOAD_STARTED = time.perf_counter()
MODEL = SentenceTransformer(MODEL_PATH, trust_remote_code=True, device="cpu")
MODEL_LOAD_MS = (time.perf_counter() - _MODEL_LOAD_STARTED) * 1000
_INFERENCE_LOCK = threading.Lock()

_WARMUP_STARTED = time.perf_counter()
with _INFERENCE_LOCK:
    _warmup_embedding = MODEL.encode(
        [WARMUP_TEXT],
        batch_size=1,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
WARMUP_MS = (time.perf_counter() - _WARMUP_STARTED) * 1000
if tuple(_warmup_embedding.shape) != (1, EXPECTED_DIMENSIONS):
    raise RuntimeError(
        f"unexpected warmup shape {_warmup_embedding.shape}; "
        f"expected (1, {EXPECTED_DIMENSIONS})"
    )

BOOT_TO_READY_MS = (time.perf_counter() - _BOOT_STARTED) * 1000
print(
    json.dumps(
        {
            "event": "model_ready",
            "model": MODEL_ID,
            "dimensions": EXPECTED_DIMENSIONS,
            "model_load_ms": round(MODEL_LOAD_MS, 3),
            "warmup_ms": round(WARMUP_MS, 3),
            "boot_to_ready_ms": round(BOOT_TO_READY_MS, 3),
            "architecture": platform.machine(),
            "cpu_count": os.cpu_count(),
            "torch_threads": torch.get_num_threads(),
            "dynamic_batch_window_ms": DYNAMIC_BATCH_WINDOW_MS,
        }
    ),
    flush=True,
)


def _normalized_text_inputs(raw_input: Any) -> list[str]:
    items = raw_input if isinstance(raw_input, list) else [raw_input]
    if len(items) > MAX_BATCH_ITEMS:
        raise ValueError(f"input must not exceed {MAX_BATCH_ITEMS} items")

    texts: list[str] = []
    for item in items:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict) and item.get("type", "text") == "text":
            text = item.get("text")
            if not isinstance(text, str):
                raise ValueError("text input object requires a string 'text' field")
        else:
            raise ValueError("this MicroVM image accepts text inputs only")
        if len(text) > MAX_INPUT_CHARACTERS:
            raise ValueError(
                f"text input must not exceed {MAX_INPUT_CHARACTERS:,} characters"
            )
        texts.append(text)
    return texts


@dataclass
class _BatchJob:
    texts: list[str]
    completed: threading.Event = field(default_factory=threading.Event)
    embeddings: list[list[float]] | None = None
    error: BaseException | None = None


class DynamicBatcher:
    """Coalesce simultaneous HTTP requests into one serialized model call."""

    def __init__(self) -> None:
        self._queue: queue.Queue[_BatchJob] = queue.Queue(maxsize=64)
        self._deferred: _BatchJob | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="embedding-batcher",
            daemon=True,
        )
        self._thread.start()

    def encode(self, texts: list[str], timeout: float = JOB_TIMEOUT_SECONDS) -> list[list[float]]:
        if not texts:
            return []
        job = _BatchJob(texts=texts)
        try:
            self._queue.put(job, timeout=1)
        except queue.Full as error:
            raise RuntimeError("embedding worker queue is full") from error
        if not job.completed.wait(timeout):
            raise TimeoutError("embedding batch timed out")
        if job.error is not None:
            raise job.error
        assert job.embeddings is not None
        return job.embeddings

    def _collect(self, first: _BatchJob) -> list[_BatchJob]:
        jobs = [first]
        total_items = len(first.texts)
        deadline = time.perf_counter() + (DYNAMIC_BATCH_WINDOW_MS / 1000)
        while total_items < DYNAMIC_BATCH_MAX_ITEMS:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                job = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if total_items + len(job.texts) > DYNAMIC_BATCH_MAX_ITEMS:
                self._deferred = job
                break
            jobs.append(job)
            total_items += len(job.texts)
        return jobs

    def _run(self) -> None:
        while True:
            first = self._deferred
            if first is None:
                first = self._queue.get()
            else:
                self._deferred = None
            jobs = self._collect(first)
            flattened = [text for job in jobs for text in job.texts]
            try:
                started = time.perf_counter()
                with _INFERENCE_LOCK:
                    encoded = MODEL.encode(
                        flattened,
                        batch_size=MODEL_BATCH_SIZE,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                    )
                offset = 0
                for job in jobs:
                    end = offset + len(job.texts)
                    job.embeddings = [row.tolist() for row in encoded[offset:end]]
                    offset = end
                print(
                    json.dumps(
                        {
                            "event": "embedding_batch",
                            "http_requests": len(jobs),
                            "items": len(flattened),
                            "encode_ms": round(
                                (time.perf_counter() - started) * 1000, 3
                            ),
                        }
                    ),
                    flush=True,
                )
            except BaseException as error:  # noqa: BLE001 - hand off to request threads
                for job in jobs:
                    job.error = error
            finally:
                for job in jobs:
                    job.completed.set()


BATCHER = DynamicBatcher()


class EmbeddingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "any-embedding-microvm/2"

    def log_message(self, message_format: str, *args: Any) -> None:
        print(
            json.dumps(
                {
                    "event": "http_access",
                    "client": self.client_address[0],
                    "message": message_format % args,
                }
            ),
            flush=True,
        )

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            raise ValueError("request body must be between 1 byte and 6 MiB")
        payload = json.loads(self.rfile.read(content_length))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _drain_request_body(self) -> None:
        """Consume a hook payload so HTTP/1.1 connections remain reusable."""
        raw_content_length = self.headers.get("Content-Length")
        if raw_content_length is None:
            return
        try:
            remaining = int(raw_content_length)
        except ValueError:
            self.close_connection = True
            return
        if remaining < 0 or remaining > MAX_BODY_BYTES:
            self.close_connection = True
            return
        while remaining:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "model": MODEL_ID,
                    "dimensions": EXPECTED_DIMENSIONS,
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        lifecycle_paths = {
            "/aws/lambda-microvms/runtime/v1/ready",
            "/aws/lambda-microvms/runtime/v1/run",
            "/aws/lambda-microvms/runtime/v1/resume",
            "/aws/lambda-microvms/runtime/v1/suspend",
            "/aws/lambda-microvms/runtime/v1/terminate",
        }
        if self.path in lifecycle_paths:
            self._drain_request_body()
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return

        if self.path == "/aws/lambda-microvms/runtime/v1/validate":
            self._drain_request_body()
            with _INFERENCE_LOCK:
                validation = MODEL.encode(
                    [WARMUP_TEXT],
                    batch_size=1,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            if tuple(validation.shape) != (1, EXPECTED_DIMENSIONS):
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "invalid", "shape": list(validation.shape)},
                )
                return
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return

        if self.path not in {"/embed", "/v1/embeddings"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
            return

        try:
            payload = self._read_json()
            texts = _normalized_text_inputs(payload.get("input"))
            embeddings = BATCHER.encode(texts)
            total_tokens = sum(len(text.split()) for text in texts)
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {
                            "object": "embedding",
                            "embedding": embedding,
                            "index": index,
                        }
                        for index, embedding in enumerate(embeddings)
                    ],
                    "model": payload.get("model", MODEL_API_NAME),
                    "usage": {
                        "prompt_tokens": total_tokens,
                        "total_tokens": total_tokens,
                    },
                },
            )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"message": str(error), "type": "invalid_request_error"}},
            )
        except Exception as error:  # pragma: no cover - service boundary
            print(
                json.dumps(
                    {
                        "event": "embedding_error",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                ),
                flush=True,
            )
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"message": "embedding failed", "type": "server_error"}},
            )


class EmbeddingServer(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    server = EmbeddingServer(("0.0.0.0", PORT), EmbeddingHandler)
    print(json.dumps({"event": "http_listening", "port": PORT}), flush=True)
    server.serve_forever()
