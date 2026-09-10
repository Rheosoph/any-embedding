"""Embedding worker service.

Loads exactly one sentence-transformers model and exposes an internal
/embed endpoint. Deployed as a separate Cloud Run service per model.
Supports text-only and multimodal (image+text) models.

Only the standard library may be imported above the prefetch section: the
prefetch threads must already be issuing reads while torch and
sentence-transformers are imported (each first file open on Cloud Run's
lazily streamed image filesystem is a remote fetch).
"""

import asyncio
import base64
import functools
import importlib.util
import inspect
import io
import ipaddress
import logging
import os
import socket
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

_BOOT_STARTED = time.perf_counter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- Environment ------------------------------------------------------------


def _env_int(name: str, default: str, minimum: int) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_bool(name: str, default: str) -> bool:
    raw = os.environ.get(name, default).strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


MODEL_NAME = os.environ.get("MODEL_NAME", "")
# "text" = text-only sentence-transformers, "image" = multimodal (CLIP-style)
MODEL_TYPE = os.environ.get("MODEL_TYPE", "text")
HF_HOME = os.environ.get("HF_HOME", "/app/models")
MODEL_PATH = os.environ.get("MODEL_PATH", "/app/model")
MODEL_BATCH_SIZE = _env_int("MODEL_BATCH_SIZE", "32", minimum=1)
MODEL_WARMUP = os.environ.get("MODEL_WARMUP", "false").lower() == "true"
# 0 = leave torch's own thread heuristics alone.
WORKER_CPU_LIMIT = _env_int("WORKER_CPU_LIMIT", "0", minimum=0)
# Cloud Run request concurrency; sizes the executor that runs /embed work.
WORKER_CONCURRENCY = _env_int("WORKER_CONCURRENCY", "4", minimum=1)
# How many requests may be inside model.encode at the same time.
WORKER_ENCODE_PARALLELISM = _env_int("WORKER_ENCODE_PARALLELISM", "1", minimum=1)
MODEL_DTYPE = _env_choice("MODEL_DTYPE", "float32", ("float32", "float16", "bfloat16"))
MODEL_MATMUL_PRECISION = _env_choice("MODEL_MATMUL_PRECISION", "highest", ("highest", "high", "medium"))
# 0 = keep the model's own max_seq_length.
MODEL_MAX_SEQ_LENGTH = _env_int("MODEL_MAX_SEQ_LENGTH", "0", minimum=0)
MODEL_PREFETCH = _env_bool("MODEL_PREFETCH", "true")
MODEL_PREFETCH_MAX_BYTES = _env_int("MODEL_PREFETCH_MAX_BYTES", str(8 * 1024**3), minimum=0)
IMPORT_MANIFEST_PATH = os.environ.get("IMPORT_MANIFEST_PATH", "/app/import_manifest.txt")


# --- Prefetch ---------------------------------------------------------------

_PREFETCH_CHUNK = 8 * 1024 * 1024
_PREFETCH_MANIFEST_MAX_FILE = 32 * 1024 * 1024
_PREFETCH_MANIFEST_WORKERS = 8
_CGROUP_MEMORY_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")


def _new_prefetch_stats() -> dict[str, int | float]:
    return {
        "model_files": 0, "model_skipped": 0, "model_bytes": 0, "model_ms": 0.0,
        "manifest_files": 0, "manifest_bytes": 0, "manifest_ms": 0.0,
    }


_PREFETCH_STATS = _new_prefetch_stats()
_PREFETCH_THREADS: list[threading.Thread] = []


def _cgroup_memory_limit() -> int | None:
    """Container memory limit in bytes, or None when unlimited/unknown."""
    for candidate in _CGROUP_MEMORY_FILES:
        try:
            raw = Path(candidate).read_text().strip()
        except OSError:
            continue
        if raw.isdigit():
            return int(raw)
    return None


def _prefetch_budget() -> int:
    budget = MODEL_PREFETCH_MAX_BYTES
    limit = _cgroup_memory_limit()
    if limit is not None:
        # Leave room for the process itself; pages beyond that would only be
        # evicted again before the model gets to use them.
        budget = min(budget, int(limit * 0.6))
    return budget


def _read_file(path: str, buffer: bytearray) -> int:
    """Read a file into the page cache, discarding its content."""
    total = 0
    with open(path, "rb", buffering=0) as handle:
        while count := handle.readinto(buffer):
            total += count
    return total


def _regular_files_by_size(root: str) -> list[tuple[int, str]]:
    files: list[tuple[int, str]] = []
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                files.append((info.st_size, path))
    files.sort(reverse=True)
    return files


def _prefetch_model_dir(root: str, budget: int, stats: dict[str, int | float]) -> None:
    started = time.perf_counter()
    try:
        buffer = bytearray(_PREFETCH_CHUNK)
        remaining = budget
        for size, path in _regular_files_by_size(root):
            if size > remaining:
                stats["model_skipped"] += 1
                continue
            try:
                count = _read_file(path, buffer)
            except OSError as exc:
                logger.debug("model prefetch skipped %s: %s", path, exc)
                continue
            remaining -= count
            stats["model_files"] += 1
            stats["model_bytes"] += count
    except OSError as exc:
        logger.debug("model prefetch aborted: %s", exc)
    finally:
        stats["model_ms"] = (time.perf_counter() - started) * 1000


def _read_manifest_entry(path: str) -> int:
    try:
        info = os.stat(path)
        if info.st_size > _PREFETCH_MANIFEST_MAX_FILE or not stat.S_ISREG(info.st_mode):
            return 0
        return _read_file(path, bytearray(min(info.st_size, _PREFETCH_CHUNK) or 1))
    except OSError:
        return 0


def _prefetch_import_manifest(manifest_path: str, stats: dict[str, int | float]) -> None:
    started = time.perf_counter()
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            paths = [line.strip() for line in handle if line.strip()]
        # The manifest lists modules in import order, so the importing main
        # thread rides behind fetches this pool has already issued.
        with ThreadPoolExecutor(_PREFETCH_MANIFEST_WORKERS, thread_name_prefix="prefetch") as pool:
            for count in pool.map(_read_manifest_entry, paths):
                if count:
                    stats["manifest_files"] += 1
                    stats["manifest_bytes"] += count
    except OSError as exc:
        logger.debug("import manifest prefetch aborted: %s", exc)
    finally:
        stats["manifest_ms"] = (time.perf_counter() - started) * 1000


def _start_prefetch() -> list[threading.Thread]:
    """Warm the page cache for the model directory and the import manifest."""
    threads: list[threading.Thread] = []
    if os.path.isdir(MODEL_PATH):
        threads.append(threading.Thread(
            target=_prefetch_model_dir, args=(MODEL_PATH, _prefetch_budget(), _PREFETCH_STATS),
            name="prefetch-model", daemon=True,
        ))
    if os.path.isfile(IMPORT_MANIFEST_PATH):
        threads.append(threading.Thread(
            target=_prefetch_import_manifest, args=(IMPORT_MANIFEST_PATH, _PREFETCH_STATS),
            name="prefetch-imports", daemon=True,
        ))
    for thread in threads:
        thread.start()
    return threads


if MODEL_PREFETCH:
    _PREFETCH_THREADS = _start_prefetch()


def write_import_manifest(path: str) -> int:
    """Write the files behind every imported module, one absolute path per line.

    Compiled .pyc files are preferred because that is what the interpreter
    opens at import time. The transformers model sources are appended because
    transformers scans them at import even though they are never imported.
    """
    paths: dict[str, None] = {}
    for module in list(sys.modules.values()):
        # Read __dict__ rather than getattr: transformers exposes lazy module
        # proxies whose __getattr__ imports submodules on any attribute miss,
        # which would pull in optional extras (torchvision) and inflate this
        # manifest with modules the worker never imports.
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for candidate in (namespace.get("__cached__"), namespace.get("__file__")):
            if isinstance(candidate, str) and os.path.isfile(candidate):
                paths[os.path.abspath(candidate)] = None
                break
    spec = importlib.util.find_spec("transformers")
    for location in (spec.submodule_search_locations or []) if spec else []:
        for source in sorted(Path(location, "models").rglob("*.py")):
            paths[os.path.abspath(source)] = None
    Path(path).write_text("".join(f"{line}\n" for line in paths), encoding="utf-8")
    return len(paths)


# --- Runtime ----------------------------------------------------------------

import numpy as np  # noqa: E402
import orjson  # noqa: E402
import torch  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from huggingface_hub import snapshot_download  # noqa: E402
from huggingface_hub.errors import LocalEntryNotFoundError  # noqa: E402
from PIL import Image  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

from app.shared.models import (  # noqa: E402
    EmbeddingInput,
    EmbeddingRequest,
    ErrorResponse,
    HealthResponse,
)

if WORKER_CPU_LIMIT > 0:
    torch.set_num_threads(WORKER_CPU_LIMIT)

_TORCH_DTYPES: dict[str, torch.dtype | None] = {
    "float32": None, "float16": torch.float16, "bfloat16": torch.bfloat16,
}

_model: SentenceTransformer | None = None
_executor = ThreadPoolExecutor(max_workers=max(2, WORKER_CONCURRENCY), thread_name_prefix="embed")
_encode_semaphore: asyncio.Semaphore | None = None


def _resolve_model_source() -> str:
    """Prefer the baked local model directory so workers can start offline."""
    local_model_dir = Path(MODEL_PATH)
    if local_model_dir.is_dir() and any(local_model_dir.iterdir()):
        logger.info("Using baked model directory for %s from %s", MODEL_NAME, local_model_dir)
        return str(local_model_dir)

    try:
        snapshot_path = snapshot_download(
            repo_id=MODEL_NAME,
            cache_dir=HF_HOME,
            local_files_only=True,
        )
    except LocalEntryNotFoundError:
        logger.warning("No baked model or local snapshot found for %s, falling back to repo id", MODEL_NAME)
        return MODEL_NAME

    logger.info("Using local snapshot for %s from %s", MODEL_NAME, snapshot_path)
    return snapshot_path


def _supports_model_kwargs() -> bool:
    # sentence-transformers 2.7.0 (pinned for the gte models) predates model_kwargs.
    return "model_kwargs" in inspect.signature(SentenceTransformer.__init__).parameters


def _construct_model(model_source: str, device: str) -> SentenceTransformer:
    dtype = _TORCH_DTYPES[MODEL_DTYPE]
    kwargs: dict = {"trust_remote_code": True, "device": device}
    via_kwargs = dtype is not None and _supports_model_kwargs()
    if via_kwargs:
        kwargs["model_kwargs"] = {"torch_dtype": dtype}
    model = SentenceTransformer(model_source, **kwargs)
    if dtype is not None and not via_kwargs:
        model.to(dtype=dtype)
    return model


def _model_dtype(model: SentenceTransformer) -> str:
    try:
        return str(next(model.parameters()).dtype).removeprefix("torch.")
    except (AttributeError, StopIteration, TypeError):
        return MODEL_DTYPE


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        started = time.perf_counter()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda" and MODEL_MATMUL_PRECISION != "highest":
            torch.set_float32_matmul_precision(MODEL_MATMUL_PRECISION)
        logger.info("Loading model: %s (type=%s, device=%s)", MODEL_NAME, MODEL_TYPE, device)
        model_source = _resolve_model_source()
        model = _construct_model(model_source, device)
        if MODEL_MAX_SEQ_LENGTH > 0 and getattr(model, "max_seq_length", None) is not None:
            model.max_seq_length = min(model.max_seq_length, MODEL_MAX_SEQ_LENGTH)
        _model = model
        logger.info("Model loaded: %s on %s in %.1f ms (dtype=%s, max_seq_length=%s)",
                    MODEL_NAME, device, (time.perf_counter() - started) * 1000,
                    _model_dtype(model), getattr(model, "max_seq_length", None))
    return _model


_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.google.internal."})


def _is_safe_url(url: str) -> bool:
    """Block SSRF attempts targeting internal/cloud metadata endpoints."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return False
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        return False
    try:
        for info in socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return False
    except socket.gaierror:
        return False
    return True


def _load_image(image_input: "EmbeddingInput") -> Image.Image:
    """Load a PIL Image from an EmbeddingInput with type='image'."""
    img = image_input.image
    if img is None:
        raise ValueError("Image input requires 'image' field")

    if img.image_base64:
        data = base64.b64decode(img.image_base64)
        return Image.open(io.BytesIO(data)).convert("RGB")

    if img.image_url:
        import httpx  # only needed for image URLs; keep it off the boot path

        parsed = urlparse(img.image_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported image URL scheme: {parsed.scheme}")
        if not _is_safe_url(img.image_url):
            raise ValueError("URL targets a blocked internal address")
        resp = httpx.get(img.image_url, timeout=30.0, follow_redirects=False)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")

    raise ValueError("Image input requires either 'image_url' or 'image_base64'")


def _normalize_inputs(raw_input: "str | EmbeddingInput | list") -> list[str | Image.Image]:
    """Convert the flexible input format into a flat list of str or PIL Image."""
    items: list = raw_input if isinstance(raw_input, list) else [raw_input]
    result: list[str | Image.Image] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, EmbeddingInput):
            if item.type == "text":
                if item.text is None:
                    raise ValueError("Text input requires 'text' field")
                result.append(item.text)
            elif item.type == "image":
                result.append(_load_image(item))
            else:
                raise ValueError(f"Unknown input type: {item.type}")
        elif isinstance(item, dict):
            result.append(_normalize_inputs(EmbeddingInput(**item))[0])
        else:
            raise ValueError(f"Unexpected input item type: {type(item)}")
    return result


def _encode(model: SentenceTransformer, inputs: list, batch_size: int) -> np.ndarray:
    # Ask for a tensor and convert it here: sentence-transformers 2.7.0 (the
    # gte pins) cannot convert bfloat16 outputs to numpy itself, and one
    # device-to-host copy of the stacked result beats one per batch.
    with torch.inference_mode():
        embeddings = model.encode(
            inputs, batch_size=batch_size,
            normalize_embeddings=True, show_progress_bar=False,
            convert_to_tensor=True,
        )
    if isinstance(embeddings, torch.Tensor):
        embeddings = embeddings.detach().float().cpu().numpy()
    return np.asarray(embeddings)


def _serialize_response(
    embeddings: np.ndarray, model_name: str, encoding_format: str, total_tokens: int
) -> bytes:
    """Serialize the OpenAI-shaped response with orjson.

    Rows are cast to float64 first: orjson prints float32 arrays with the
    shortest float32 repr, which differs from the historical tolist() output.
    """
    if encoding_format == "float":
        rows: list = list(np.ascontiguousarray(embeddings, dtype=np.float64))
    elif encoding_format == "base64":
        rows = [base64.b64encode(row.astype("<f4").tobytes()).decode("ascii") for row in embeddings]
    else:
        raise ValueError("encoding_format must be 'float' or 'base64'")
    payload = {
        "object": "list",
        "data": [{"object": "embedding", "embedding": row, "index": i} for i, row in enumerate(rows)],
        "model": model_name,
        "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    }
    return orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY)


def _get_encode_semaphore() -> asyncio.Semaphore:
    global _encode_semaphore
    if _encode_semaphore is None:
        _encode_semaphore = asyncio.Semaphore(WORKER_ENCODE_PARALLELISM)
    return _encode_semaphore


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _encode_semaphore
    if not MODEL_NAME:
        raise RuntimeError("MODEL_NAME environment variable is required")
    _encode_semaphore = asyncio.Semaphore(WORKER_ENCODE_PARALLELISM)
    model = get_model()
    logger.info(
        "model_prefetch enabled=%s model_files=%d model_skipped=%d model_bytes=%d model_ms=%.1f "
        "manifest_files=%d manifest_bytes=%d manifest_ms=%.1f pending_threads=%d",
        MODEL_PREFETCH, _PREFETCH_STATS["model_files"], _PREFETCH_STATS["model_skipped"],
        _PREFETCH_STATS["model_bytes"], _PREFETCH_STATS["model_ms"],
        _PREFETCH_STATS["manifest_files"], _PREFETCH_STATS["manifest_bytes"],
        _PREFETCH_STATS["manifest_ms"], sum(thread.is_alive() for thread in _PREFETCH_THREADS),
    )
    if MODEL_WARMUP:
        started = time.perf_counter()
        warmup_input = Image.new("RGB", (224, 224)) if MODEL_TYPE == "image" else "Embedding warmup"
        await asyncio.get_running_loop().run_in_executor(
            _executor, functools.partial(_encode, model, [warmup_input], 1)
        )
        logger.info("Model warmup completed: %s in %.1f ms", MODEL_NAME,
                    (time.perf_counter() - started) * 1000)
    logger.info("Worker ready: %s boot_to_ready_ms=%.1f", MODEL_NAME,
                (time.perf_counter() - _BOOT_STARTED) * 1000)
    yield


app = FastAPI(title="any-embedding worker", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(ValueError)
async def _invalid_request(_request: Request, exc: ValueError) -> Response:
    body = {"error": {"message": str(exc), "type": "invalid_request_error"}}
    return Response(content=orjson.dumps(body), status_code=400, media_type="application/json")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", model=MODEL_NAME)


@app.post("/embed", responses={400: {"model": ErrorResponse}})
async def embed(request: EmbeddingRequest) -> Response:
    """Compute embeddings. Called internally by the gateway."""
    # Image downloads, tokenization and result conversion must not block the
    # event loop while other requests or health probes are being handled.
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    inputs = await loop.run_in_executor(_executor, _normalize_inputs, request.input)
    prepared = time.perf_counter()
    # Only the model call is serialized; input preparation of other requests
    # (image downloads, decoding) keeps overlapping with it.
    async with _get_encode_semaphore():
        body, encode_ms = await loop.run_in_executor(_executor, _embed, request, inputs)
    logger.info(
        "embedding_complete model=%s items=%d prepare_ms=%.1f encode_ms=%.1f total_ms=%.1f",
        MODEL_NAME, len(inputs), (prepared - started) * 1000,
        encode_ms, (time.perf_counter() - started) * 1000,
    )
    return Response(content=body, media_type="application/json")


def _embed(request: EmbeddingRequest, inputs: list) -> tuple[bytes, float]:
    model = get_model()
    started = time.perf_counter()
    embeddings = _encode(model, inputs, MODEL_BATCH_SIZE)
    encode_ms = (time.perf_counter() - started) * 1000

    # Approximate token count (only for text inputs)
    total_tokens = sum(len(t.split()) for t in inputs if isinstance(t, str))
    body = _serialize_response(embeddings, request.model, request.encoding_format, total_tokens)
    return body, encode_ms
