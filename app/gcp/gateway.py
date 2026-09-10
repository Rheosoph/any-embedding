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
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, Response, Security
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


def _worker_audience(model_cfg: ModelConfig) -> str:
    """The audience of a worker's ID token is its base URL (no trailing slash)."""
    return model_cfg["worker_url"].rstrip("/")


_GCE_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1"
    "/instance/service-accounts/default/identity"
)


_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
_METADATA_FAILURE_CACHE_SECONDS = 10.0
_TOKEN_PREFETCH_TIMEOUT_SECONDS = 2.0

# Cloud Run's own idle timeout for HTTPS keepalives is well above this; the
# httpx default (5 s) forces a fresh TLS handshake to run.app on nearly every
# request under sparse traffic.
_WORKER_CONNECT_TIMEOUT_SECONDS = 10.0
_WORKER_CLIENT_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=100,
    keepalive_expiry=300.0,
)


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
            # Cache hits are the steady state; keep them out of INFO noise.
            logger.log(
                logging.DEBUG if outcome == "cache_hit" else logging.INFO,
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


async def _prefetch_id_tokens(
    provider: _IDTokenProvider, registry: dict[str, ModelConfig]
) -> None:
    """Warm the token cache so the first request per worker skips metadata.

    Best effort: a slow or absent metadata server must never delay or fail
    startup, so the whole batch is bounded by one short deadline.
    """
    audiences = sorted({_worker_audience(cfg) for cfg in registry.values()})
    if not audiences:
        return
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(provider.get_token(a) for a in audiences)),
            timeout=_TOKEN_PREFETCH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "id_token_prefetch outcome=timeout audiences=%d timeout_s=%.1f",
            len(audiences),
            _TOKEN_PREFETCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - startup must survive any failure
        logger.warning(
            "id_token_prefetch outcome=error audiences=%d error=%s",
            len(audiences),
            type(exc).__name__,
        )
    else:
        logger.info(
            "id_token_prefetch outcome=done audiences=%d elapsed_ms=%.2f",
            len(audiences),
            round((time.monotonic() - started) * 1000, 2),
        )


MODEL_REGISTRY: dict[str, ModelConfig] = {}

# --- Rate limiting (in-memory sliding window) --------------------------------

RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "300"))
# Only a trusted reverse proxy (Cloud Run) may vouch for the client address;
# exposed directly, X-Forwarded-For is caller-supplied and must be ignored.
TRUST_X_FORWARDED_FOR = os.environ.get("TRUST_X_FORWARDED_FOR", "false").strip().lower() in ("true", "1", "yes")
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_request_log: dict[str, deque[float]] = defaultdict(deque)
_request_log_swept_at = 0.0


def _client_ip(request: Request) -> str:
    """Identify the caller behind the Cloud Run proxy.

    Cloud Run appends the connecting client's address as the LAST entry of
    X-Forwarded-For; earlier entries are client-supplied and spoofable. The
    TCP peer (request.client) is the proxy itself there, so it is only a
    fallback. Without a trusted proxy the TCP peer is the client.
    """
    forwarded_for = request.headers.get("x-forwarded-for", "") if TRUST_X_FORWARDED_FOR else ""
    if forwarded_for:
        rightmost = forwarded_for.rsplit(",", 1)[-1].strip()
        if rightmost:
            return rightmost
    return request.client.host if request.client else "unknown"


def _prune_timestamps(timestamps: deque[float], now: float) -> None:
    while timestamps and now - timestamps[0] >= _RATE_LIMIT_WINDOW_SECONDS:
        timestamps.popleft()


def _sweep_request_log(now: float) -> None:
    """Drop idle clients at most once per window so the map cannot grow forever."""
    global _request_log_swept_at
    if now - _request_log_swept_at < _RATE_LIMIT_WINDOW_SECONDS:
        return
    _request_log_swept_at = now
    for client_ip, timestamps in list(_request_log.items()):
        _prune_timestamps(timestamps, now)
        if not timestamps:
            del _request_log[client_ip]


def _check_rate_limit(client_ip: str) -> None:
    """Enforce per-client sliding-window rate limit."""
    now = time.monotonic()
    _sweep_request_log(now)
    timestamps = _request_log[client_ip]
    _prune_timestamps(timestamps, now)
    if len(timestamps) >= RATE_LIMIT_RPM:
        if not timestamps:
            del _request_log[client_ip]
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    timestamps.append(now)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global MODEL_REGISTRY
    timeout = _worker_timeout_seconds()
    MODEL_REGISTRY = _load_model_registry()
    async with (
        httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=_WORKER_CONNECT_TIMEOUT_SECONDS),
            limits=_WORKER_CLIENT_LIMITS,
        ) as worker_client,
        httpx.AsyncClient(timeout=5.0) as metadata_client,
    ):
        _app.state.worker_client = worker_client
        _app.state.id_token_provider = _IDTokenProvider(metadata_client)
        _app.state.worker_timeout_seconds = timeout
        await _prefetch_id_tokens(_app.state.id_token_provider, MODEL_REGISTRY)
        logger.info("Gateway ready with %d model(s)", len(MODEL_REGISTRY))
        yield


app = FastAPI(title="any-embedding gateway", lifespan=lifespan)

# --- Audit logging -----------------------------------------------------------

audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)


def _audit_log(request: Request, model: str | None, status: int) -> None:
    """Emit structured audit log entry for every API call.

    Fields are rendered into the message because the default log format does
    not print 'extra' attributes; they are kept as attributes for structured
    handlers.
    """
    client_ip = _client_ip(request)
    audit_logger.info(
        "api_request client_ip=%s method=%s path=%s model=%s status=%s",
        client_ip,
        request.method,
        request.url.path,
        model,
        status,
        extra={
            "client_ip": client_ip,
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
) -> Response:
    _check_rate_limit(_client_ip(http_request))

    model_cfg = MODEL_REGISTRY.get(request.model)
    if model_cfg is None:
        available = list(MODEL_REGISTRY.keys())
        _audit_log(http_request, request.model, 400)
        raise HTTPException(
            status_code=400,
            detail=f"Model '{request.model}' not found. Available: {available}",
        )

    worker_url = _worker_audience(model_cfg)

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
    # The worker already produced a validated OpenAI-shaped body (float or
    # base64). Re-parsing it here cost ~100x the forwarding time and ~128 MB
    # per large request, so hand the bytes through untouched. Only the media
    # type is copied: Starlette computes content-length for the decoded body.
    return Response(
        content=resp.content,
        status_code=200,
        media_type=resp.headers.get("content-type", "application/json"),
    )
