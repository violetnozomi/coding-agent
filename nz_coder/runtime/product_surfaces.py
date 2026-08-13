"""Declared capability contract shared by every NZ-Coder product surface."""
from __future__ import annotations

from enum import Enum


class ProductSurface(str, Enum):
    """Stable names for user-facing execution adapters."""

    INTERACTIVE = "interactive"
    HEADLESS = "headless"
    SDK = "sdk"
    HTTP = "http"


PRODUCT_CAPABILITY_FINGERPRINT = frozenset({
    "mcp",
    "skills",
    "memory",
    "tool_exposure",
    "permissions",
    "guardrails",
    "planning",
    "verification",
    "snapshots",
    "media_preflight",
    "subagents",
    "workflows",
    "events",
    "sessions",
    "tracing",
    "context_compaction",
    "recovery",
    "repo_intelligence",
    "retrieval_policy",
    "process_service",
    "web_search",
})


def capability_fingerprint(surface: ProductSurface | str) -> frozenset[str]:
    """Return the non-negotiable runtime capabilities for a product adapter."""
    ProductSurface(surface)
    return PRODUCT_CAPABILITY_FINGERPRINT


__all__ = [
    "PRODUCT_CAPABILITY_FINGERPRINT", "ProductSurface", "capability_fingerprint",
]
