"""API Gateway service.

Lightweight router that:
- Validates the preshared API key
- Resolves the requested model to the correct worker Cloud Run URL
- Forwards the embedding request and returns the response
- Exposes the OpenAI-compatible /v1/embeddings endpoint
"""

import asyncio
import base64
import hmac
import json
import logging
import math
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app.shared.models import (
    EmbeddingRequest,
    EmbeddingResponse,
    ErrorResponse,
    HealthResponse,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Auth -------------------------------------------------------------------

API_KEY = os.environ.get("API_KEY", "")
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


async def verify_api_key(
    request: Request,
    api_key: str | None = Security(api_key_header),
) -> str:
    """Accept both 'Bearer <key>' and raw key in the Authorization header."""
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY not configured on server")

    if api_key is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    key = api_key.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


# --- Model routing -----------------------------------------------------------

ModelConfig = dict  # alias for readability


def _load_model_registry() -> dict[str, ModelConfig]:
    """Build model→worker-URL mapping.

    Worker URLs are passed as env vars by Terraform:
      WORKER_URL_<sanitized_model_name>=https://...
    The config is loaded to know which models exist and their prefixes.
    """
    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    path = Path(config_path)
    if not path.exists():
        logger.warning("Config not found at %s – no models registered", config_path)
        return {}

    with open(path) as f:
        cfg = yaml.safe_load(f)

    registry: dict[str, ModelConfig] = {}
    for m in cfg.get("models", []):
        name = m["name"]
        env_key = "WORKER_URL_" + name.replace("-", "_").replace(".", "_").upper()
        worker_url = os.environ.get(env_key, "")
        if not worker_url:
            logger.warning("No worker URL for model %s (expected env %s)", name, env_key)
            continue
        registry[name] = {
            **m,
            "worker_url": worker_url,
        }
        logger.info("Registered model %s → %s", name, worker_url)

    return registry


_GCE_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1"
    "/instance/service-accounts/default/identity"
)


_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
_METADATA_FAILURE_CACHE_SECONDS = 10.0


def _worker_timeout_seconds() -> float:
    """Reject invalid deadlines when the service starts."""
    try:
        timeout = float(os.environ.get("WORKER_TIMEOUT_SECONDS", "120"))
    except ValueError as exc:
        raise ValueError("WORKER_TIMEOUT_SECONDS must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("WORKER_TIMEOUT_SECONDS must be a finite positive number")
    return timeout


def _token_expiry(token: str) -> float | None:
    """Read expiry for caching only; Cloud Run validates the token signature."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = float(claims["exp"])
        return expiry if math.isfinite(expiry) else None
    except (ValueError, KeyError, IndexError, TypeError, OverflowError):
        return None


class _IDTokenProvider:
    """Reuse metadata tokens per audience and coalesce concurrent refreshes."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.tokens: dict[str, tuple[str, float]] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.metadata_unavailable_until = 0.0

    def _cached_token(self, audience: str) -> str | None:
        cached = self.tokens.get(audience)
        if cached and cached[1] > time.time() + _TOKEN_EXPIRY_MARGIN_SECONDS:
            return cached[0]
        self.tokens.pop(audience, None)
        return None

    async def get_token(self, audience: str) -> str | None:
        started = time.monotonic()
        outcome = "interrupted"
        try:
            async with self.locks[audience]:
                cached = self._cached_token(audience)
                if cached:
                    outcome = "cache_hit"
                    return cached
                if time.monotonic() < self.metadata_unavailable_until:
                    outcome = "metadata_backoff"
                    return None
                try:
                    resp = await self.client.get(
                        _GCE_METADATA_URL,
                        params={"audience": audience},
                        headers={"Metadata-Flavor": "Google"},
                    )
                    resp.raise_for_status()
                except httpx.HTTPError:
                    # Local workers need no Cloud Run token. Bound the penalty
                    # while still retrying metadata after transient failures.
                    self.metadata_unavailable_until = (
                        time.monotonic() + _METADATA_FAILURE_CACHE_SECONDS
                    )
                    outcome = "metadata_unavailable"
                    return None
                token = resp.text
                expiry = _token_expiry(token)
                if expiry is not None and expiry > time.time() + _TOKEN_EXPIRY_MARGIN_SECONDS:
                    self.tokens[audience] = (token, expiry)
                    outcome = "refreshed"
                else:
                    outcome = "uncacheable"
                return token
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            logger.info(
                "id_token_lookup outcome=%s elapsed_ms=%.2f",
                outcome,
                elapsed_ms,
                extra={
                    "elapsed_ms": elapsed_ms,
                    "outcome": outcome,
                },
            )


async def _get_id_token(audience: str) -> str | None:
    return await app.state.id_token_provider.get_token(audience)


MODEL_REGISTRY: dict[str, ModelConfig] = {}

# --- Rate limiting (in-memory sliding window) --------------------------------

RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "300"))
_request_log: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> None:
    """Enforce per-IP sliding-window rate limit."""
    now = time.monotonic()
    window = 60.0
    timestamps = _request_log[client_ip]
    # Prune expired entries
    _request_log[client_ip] = [t for t in timestamps if now - t < window]
    if len(_request_log[client_ip]) >= RATE_LIMIT_RPM:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    _request_log[client_ip].append(now)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global MODEL_REGISTRY
    timeout = _worker_timeout_seconds()
    MODEL_REGISTRY = _load_model_registry()
    async with (
        httpx.AsyncClient(timeout=timeout) as worker_client,
        httpx.AsyncClient(timeout=5.0) as metadata_client,
    ):
        _app.state.worker_client = worker_client
        _app.state.id_token_provider = _IDTokenProvider(metadata_client)
        _app.state.worker_timeout_seconds = timeout
        logger.info("Gateway ready with %d model(s)", len(MODEL_REGISTRY))
        yield


app = FastAPI(title="any-embedding gateway", lifespan=lifespan)

# --- Audit logging -----------------------------------------------------------

audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)


def _audit_log(request: Request, model: str | None, status: int) -> None:
    """Emit structured audit log entry for every API call."""
    audit_logger.info(
        "api_request",
        extra={
            "client_ip": request.client.host if request.client else "unknown",
            "method": request.method,
            "path": request.url.path,
            "model": model,
            "status": status,
        },
    )


# --- Endpoints ---------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/v1/models")
async def list_models(
    _key: str = Security(verify_api_key),
) -> dict:
    """List available models (OpenAI-compatible)."""
    data = [
        {
            "id": name,
            "object": "model",
            "owned_by": "any-embedding",
            "permissions": [],
        }
        for name in MODEL_REGISTRY
    ]
    return {"object": "list", "data": data}


@app.post(
    "/v1/embeddings",
    response_model=EmbeddingResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
async def create_embeddings(
    http_request: Request,
    request: EmbeddingRequest,
    _key: str = Security(verify_api_key),
) -> EmbeddingResponse:
    _check_rate_limit(http_request.client.host if http_request.client else "unknown")

    model_cfg = MODEL_REGISTRY.get(request.model)
    if model_cfg is None:
        available = list(MODEL_REGISTRY.keys())
        _audit_log(http_request, request.model, 400)
        raise HTTPException(
            status_code=400,
            detail=f"Model '{request.model}' not found. Available: {available}",
        )

    worker_url = model_cfg["worker_url"].rstrip("/")

    started = time.monotonic()
    worker_started: float | None = None
    status: int | None = None
    try:
        # HTTPX timeouts apply to individual I/O operations. This deadline also
        # bounds token acquisition, pool waits, and the full response transfer.
        async with asyncio.timeout(http_request.app.state.worker_timeout_seconds):
            headers: dict[str, str] = {"Content-Type": "application/json"}
            id_token = await _get_id_token(worker_url)
            if id_token:
                headers["Authorization"] = f"Bearer {id_token}"

            worker_started = time.monotonic()
            resp = await http_request.app.state.worker_client.post(
                f"{worker_url}/embed",
                content=request.model_dump_json(),
                headers=headers,
            )
            status = resp.status_code
    except (TimeoutError, httpx.TimeoutException) as exc:
        status = 504
        _audit_log(http_request, request.model, status)
        raise HTTPException(status_code=status, detail="Embedding worker timed out") from exc
    except httpx.RequestError as exc:
        status = 502
        _audit_log(http_request, request.model, status)
        raise HTTPException(status_code=status, detail="Embedding worker unavailable") from exc
    finally:
        finished = time.monotonic()
        elapsed_ms = round((finished - started) * 1000, 2)
        worker_elapsed_ms = (
            round((finished - worker_started) * 1000, 2)
            if worker_started is not None else None
        )
        logger.info(
            "worker_forward model=%s status=%s elapsed_ms=%.2f worker_elapsed_ms=%s",
            request.model,
            status,
            elapsed_ms,
            worker_elapsed_ms,
            extra={
                "model": request.model,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "worker_elapsed_ms": worker_elapsed_ms,
            },
        )

    if resp.status_code != 200:
        _audit_log(http_request, request.model, resp.status_code)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    _audit_log(http_request, request.model, 200)
    return EmbeddingResponse(**resp.json())
