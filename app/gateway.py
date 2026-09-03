"""Backward-compatible import for the Google Cloud gateway.

New code should import :mod:`app.gcp.gateway` directly.
"""

from app.gcp.gateway import app, create_embeddings, health, list_models

__all__ = ["app", "create_embeddings", "health", "list_models"]
