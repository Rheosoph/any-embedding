"""Embedding worker service.

Loads exactly one sentence-transformers model and exposes an internal
/embed endpoint. Deployed as a separate Cloud Run service per model.
Supports text-only and multimodal (image+text) models.
"""

import asyncio
import base64
import io
import ipaddress
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import torch
from fastapi import FastAPI
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError
from PIL import Image

from sentence_transformers import SentenceTransformer

from app.shared.models import (
    EmbeddingInput,
    EmbeddingObject,
    EmbeddingRequest,
    EmbeddingResponse,
    ErrorResponse,
    HealthResponse,
    Usage,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("MODEL_NAME", "")
# "text" = text-only sentence-transformers, "image" = multimodal (CLIP-style)
MODEL_TYPE = os.environ.get("MODEL_TYPE", "text")
HF_HOME = os.environ.get("HF_HOME", "/app/models")
MODEL_PATH = os.environ.get("MODEL_PATH", "/app/model")

_model: SentenceTransformer | None = None


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


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("Loading model: %s (type=%s, device=%s)", MODEL_NAME, MODEL_TYPE, device)
        model_source = _resolve_model_source()
        _model = SentenceTransformer(model_source, trust_remote_code=True, device=device)
        logger.info("Model loaded: %s on %s", MODEL_NAME, device)
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not MODEL_NAME:
        raise RuntimeError("MODEL_NAME environment variable is required")
    get_model()
    yield


app = FastAPI(title="any-embedding worker", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", model=MODEL_NAME)


@app.post("/embed", response_model=EmbeddingResponse, responses={400: {"model": ErrorResponse}})
async def embed(request: EmbeddingRequest) -> EmbeddingResponse:
    """Compute embeddings. Called internally by the gateway."""
    model = get_model()

    inputs = _normalize_inputs(request.input)

    embeddings = await asyncio.to_thread(model.encode, inputs, normalize_embeddings=True)

    data = [
        EmbeddingObject(embedding=emb.tolist(), index=i)
        for i, emb in enumerate(embeddings)
    ]

    # Approximate token count (only for text inputs)
    total_tokens = sum(len(t.split()) for t in inputs if isinstance(t, str))

    return EmbeddingResponse(
        data=data,
        model=request.model,
        usage=Usage(prompt_tokens=total_tokens, total_tokens=total_tokens),
    )
