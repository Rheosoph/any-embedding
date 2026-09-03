"""Backward-compatible import for the Google Cloud embedding worker.

New code should import :mod:`app.gcp.worker` directly.
"""

from app.gcp.worker import app, embed, get_model, health

__all__ = ["app", "embed", "get_model", "health"]
