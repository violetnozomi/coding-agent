"""Unified Provider resolution and model-call policy for Agent Core."""
from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ModelCall": "models",
    "ModelCallOutcome": "models",
    "ModelCallPurpose": "models",
    "ModelCallStatus": "models",
    "ModelStreamEvent": "models",
    "ModelSelectionRequest": "runtime",
    "NormalizedUsage": "usage",
    "OpenAIClientBridgeProvider": "gateway",
    "ProductionModelGateway": "gateway",
    "ResolvedModelRuntime": "runtime",
    "extract_provider_reported_cost": "usage",
    "normalize_usage": "usage",
    "resolve_usage_cost": "usage",
    "resolve_model_runtime": "runtime",
}

__all__ = [
    "ModelCall",
    "ModelCallOutcome",
    "ModelCallPurpose",
    "ModelCallStatus",
    "ModelStreamEvent",
    "ModelSelectionRequest",
    "NormalizedUsage",
    "OpenAIClientBridgeProvider",
    "ProductionModelGateway",
    "ResolvedModelRuntime",
    "extract_provider_reported_cost",
    "normalize_usage",
    "resolve_usage_cost",
    "resolve_model_runtime",
]


def __getattr__(name: str):  # noqa: ANN202
    """Resolve gateway exports without importing Provider machinery eagerly."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy gateway exports in interactive discovery."""
    return sorted(set(globals()).union(__all__))
