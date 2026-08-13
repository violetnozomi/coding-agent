"""Resolve one Provider, model identity, capability snapshot, and client owner."""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nz_coder.providers import create_provider
from nz_coder.providers.capabilities import (
    ModelCapabilities,
    capabilities_for_provider,
    configured_model_capabilities,
)
from nz_coder.providers.models import active_model_selection
from nz_coder.providers.registry import ModelPricing, RegistryModel, registry_runtime_model


@dataclass(frozen=True)
class ModelSelectionRequest:
    """Inputs used to resolve a complete model runtime."""

    provider_name: str | None = None
    model_id: str | None = None
    variant: str | None = None
    workspace: Path | None = None
    provider: Any = None
    client: Any = None
    owns_client: bool | None = None


@dataclass
class ResolvedModelRuntime:
    """One immutable model selection plus deterministic client ownership."""

    provider_id: str
    model_id: str
    request_model_id: str
    variant: str | None
    provider: Any
    client: Any
    capabilities: ModelCapabilities
    pricing: ModelPricing | None = None
    owns_client: bool = False
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def _claim_close(self) -> bool:
        with self._close_lock:
            if self._closed:
                return False
            self._closed = True
            return self.owns_client

    def close(self) -> None:
        """Close an internally created synchronous client at most once."""
        if not self._claim_close():
            return
        close = getattr(self.client, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            result.close()
            raise RuntimeError("Async model clients must be closed with aclose()")

    async def aclose(self) -> None:
        """Close an internally created sync or async client at most once."""
        if not self._claim_close():
            return
        close = getattr(self.client, "aclose", None)
        if not callable(close):
            close = getattr(self.client, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def resolve_model_runtime(
    request: ModelSelectionRequest,
    *,
    provider_factory: Callable[[str], Any] = create_provider,
    registry_resolver: Callable[[str, str, Path | None], RegistryModel | None] = (
        registry_runtime_model
    ),
) -> ResolvedModelRuntime:
    """Resolve selection, wire identity, capabilities, pricing, and ownership."""
    selected = active_model_selection(request.workspace)
    provider_id = str(request.provider_name or selected.provider).strip().lower()
    model_id = str(request.model_id or selected.model_id).strip()
    variant = request.variant if request.variant is not None else selected.variant
    if not provider_id:
        raise ValueError("Model provider must not be empty")
    if not model_id:
        raise ValueError("Model id must not be empty")

    provider = request.provider or provider_factory(provider_id)
    created_client = request.client is None
    client = request.client if request.client is not None else provider.create_client()
    owns_client = created_client if request.owns_client is None else request.owns_client

    provider_id = str(getattr(provider, "name", provider_id) or provider_id).strip().lower()
    registry_model = registry_resolver(provider_id, model_id, request.workspace)
    request_model_id = (
        str(registry_model.api_model_id or model_id)
        if registry_model is not None
        else model_id
    )
    capabilities = capabilities_for_provider(provider, model_id)
    if variant and variant in capabilities.available_variants:
        capabilities = configured_model_capabilities(
            provider_id,
            model_id,
            variant=variant,
        )
    return ResolvedModelRuntime(
        provider_id=provider_id,
        model_id=model_id,
        request_model_id=request_model_id,
        variant=str(variant).strip() if variant else None,
        provider=provider,
        client=client,
        capabilities=capabilities,
        pricing=registry_model.pricing if registry_model is not None else None,
        owns_client=bool(owns_client),
    )
