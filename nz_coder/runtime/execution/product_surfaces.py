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


_CAPABILITY_ROWS = (
    ("context_compaction", "implemented", "nz_coder/runtime/execution/runner.py"),
    ("events", "implemented", "nz_coder/runtime/execution/services.py"),
    ("guardrails", "implemented", "nz_coder/runtime/agent/admission.py"),
    ("mcp", "implemented", "nz_coder/mcp/client.py"),
    ("media_preflight", "partial", "nz_coder/capabilities/vision.py"),
    ("memory", "implemented", "nz_coder/runtime/execution/native_sdk.py"),
    ("permissions", "implemented", "nz_coder/runtime/agent/admission.py"),
    ("planning", "implemented", "nz_coder/runtime/agent/planning_runtime.py"),
    ("process_service", "implemented", "nz_coder/runtime/process/process_service.py"),
    ("recovery", "implemented", "nz_coder/runtime/session/lifecycle.py"),
    ("repo_intelligence", "implemented", "nz_coder/intelligence/retrieval_policy.py"),
    ("retrieval_policy", "implemented", "nz_coder/intelligence/retrieval_policy.py"),
    ("sessions", "implemented", "nz_coder/runtime/session/lifecycle.py"),
    ("skills", "implemented", "nz_coder/bundled_skills"),
    ("snapshots", "implemented", "nz_coder/runtime/execution/services.py"),
    ("subagents", "implemented", "nz_coder/runtime/agent/subagent.py"),
    ("tool_exposure", "implemented", "nz_coder/tools/__init__.py"),
    ("tracing", "implemented", "nz_coder/runtime/observability"),
    ("verification", "implemented", "nz_coder/runtime/verification"),
    ("web_search", "partial", "nz_coder/runtime/execution/services.py"),
    ("workflows", "implemented", "nz_coder/runtime/workflows"),
)

_CAPABILITY_PARITY_NOTE = (
    "InfCodeX/infcode-dev host parity requires external conformance evidence."
)


def capability_fingerprint(surface: ProductSurface | str) -> frozenset[str]:
    """Return the non-negotiable runtime capabilities for a product adapter."""
    ProductSurface(surface)
    return PRODUCT_CAPABILITY_FINGERPRINT


def capability_snapshot(surface: ProductSurface | str) -> list[dict[str, str]]:
    """Return a deterministic, host-neutral capability report for ``surface``.

    The report is assembled from immutable module data so each call returns
    independent dictionaries that callers may safely annotate or modify.
    """
    ProductSurface(surface)
    return [
        {
            "name": name,
            "status": status,
            "evidence": evidence,
            "parity_note": _CAPABILITY_PARITY_NOTE,
        }
        for name, status, evidence in _CAPABILITY_ROWS
    ]


__all__ = [
    "PRODUCT_CAPABILITY_FINGERPRINT", "ProductSurface", "capability_fingerprint",
    "capability_snapshot",
]
