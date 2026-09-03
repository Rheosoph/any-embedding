"""API Gateway service.

Lightweight router that:
- Validates the preshared API key
- Resolves the requested model to the correct worker Cloud Run URL
- Forwards the embedding request and returns the response
- Exposes the OpenAI-compatible /v1/embeddings endpoint
"""

import hmac
import logging
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


async def _get_id_token(audience: str) -> str | None:
    """Fetch a Google Cloud ID token from the metadata server (Cloud Run only)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _GCE_METADATA_URL,
                params={"audience": audience},
                headers={"Metadata-Flavor": "Google"},
            )
            resp.raise_for_status()
            return resp.text
    except Exception:
        return None


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
    MODEL_REGISTRY = _load_model_registry()
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

    headers: dict[str, str] = {"Content-Type": "application/json"}
    id_token = await _get_id_token(worker_url)
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{worker_url}/embed",
            content=request.model_dump_json(),
            headers=headers,
        )

    if resp.status_code != 200:
        _audit_log(http_request, request.model, resp.status_code)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    _audit_log(http_request, request.model, 200)
    return EmbeddingResponse(**resp.json())
