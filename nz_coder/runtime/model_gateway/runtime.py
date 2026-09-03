"""Resolve one Provider, model identity, capability snapshot, and client owner."""
from __future__ import annotations

import inspect
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

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
    base_url: str | None = None
    credential_scope_id: str | None = None
    config_snapshot: Any = None


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
    provider_instance_id: str = ""
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _inflight_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )
    _inflight: dict[int, tuple[threading.Thread, Callable[[], None]]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _next_inflight_id: int = field(default=1, init=False, repr=False)
    _tainted: bool = field(default=False, init=False, repr=False)

    def _claim_close(self) -> bool:
        with self._close_lock:
            if self._closed:
                return False
            self._closed = True
            return True

    def begin_inflight(
        self,
        worker: threading.Thread,
        cancel: Callable[[], None],
    ) -> int:
        """Register one transport worker, rejecting concurrent orphan growth."""
        with self._inflight_lock:
            if self._inflight:
                raise RuntimeError("A previous Provider worker is still running")
            call_id = self._next_inflight_id
            self._next_inflight_id += 1
            self._inflight[call_id] = (worker, cancel)
            return call_id

    def finish_inflight(self, call_id: int) -> None:
        with self._inflight_lock:
            self._inflight.pop(int(call_id), None)
            if not self._inflight:
                self._tainted = False

    def mark_inflight_unsettled(self) -> None:
        with self._inflight_lock:
            self._tainted = bool(self._inflight)

    def inflight_status(self) -> dict[str, object]:
        """Return a secret-free lifecycle projection for diagnostics."""
        with self._inflight_lock:
            running = sum(1 for worker, _cancel in self._inflight.values() if worker.is_alive())
            return {
                "worker_still_running": running,
                "tainted": bool(self._tainted or running),
            }

    def cancel_inflight(self, *, grace_seconds: float = 0.25) -> dict[str, object]:
        """Best-effort cancel and settle every owned transport worker."""
        with self._inflight_lock:
            records = list(self._inflight.values())
        cleanup_failures = 0
        for _worker, cancel in records:
            try:
                cancel()
            except Exception:
                cleanup_failures += 1
        current = threading.current_thread()
        for worker, _cancel in records:
            if worker is current:
                continue
            worker.join(timeout=max(0.0, float(grace_seconds)))
        status = self.inflight_status()
        return {**status, "cleanup_failures": cleanup_failures}

    def close(self) -> None:
        """Close an internally created synchronous client at most once."""
        if not self._claim_close():
            return
        self.cancel_inflight()
        if not self.owns_client:
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
        self.cancel_inflight()
        if not self.owns_client:
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
    explicit_selection = (
        request.provider_name is not None and request.model_id is not None
    )
    selected = (
        None
        if explicit_selection
        else active_model_selection(
            request.workspace,
            config_snapshot=request.config_snapshot,
        )
    )
    provider_id = str(
        request.provider_name
        or (selected.provider if selected is not None else "")
    ).strip().lower()
    model_id = str(
        request.model_id
        or (selected.model_id if selected is not None else "")
    ).strip()
    variant = (
        request.variant
        if explicit_selection or request.variant is not None
        else selected.variant if selected is not None else None
    )
    if not provider_id:
        raise ValueError("Model provider must not be empty")
    if not model_id:
        raise ValueError("Model id must not be empty")

    from nz_coder.providers.configuration import provider_connection

    connection = provider_connection(
        provider_id,
        config_snapshot=request.config_snapshot,
    )
    configured_provider = request.provider is None
    provider = request.provider or (
        create_provider(provider_id, config_snapshot=request.config_snapshot)
        if provider_factory is create_provider
        else provider_factory(provider_id)
    )
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
    created_client = request.client is None
    client = request.client if request.client is not None else provider.create_client()
    owns_client = created_client if request.owns_client is None else request.owns_client
    provider_instance_id = _provider_instance_id(
        provider_id=provider_id,
        provider=provider,
        client=client,
        explicit_base_url=(
            request.base_url
            if request.base_url is not None
            else connection.base_url if configured_provider else None
        ),
        credential_scope_id=(
            request.credential_scope_id or connection.credential_scope_id
        ),
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
        provider_instance_id=provider_instance_id,
    )


_PROVIDER_PRIVATE_PROTOCOL_VERSION = "nz-provider-private-v2"


def _provider_instance_id(
    *,
    provider_id: str,
    provider: Any,
    client: Any,
    explicit_base_url: str | None,
    credential_scope_id: str | None,
) -> str:
    """Build an opaque identity without including credential material."""
    endpoint = explicit_base_url
    if endpoint is None:
        endpoint = getattr(provider, "base_url", None)
    if endpoint is None:
        endpoint = getattr(client, "base_url", None)
    normalized_endpoint = _normalize_provider_endpoint(endpoint)
    if normalized_endpoint is None:
        # Unknown or credential-bearing endpoint shapes cannot safely resume
        # private continuation state across runtime construction.
        normalized_endpoint = f"unscoped:{uuid.uuid4().hex}"
    scope = str(credential_scope_id or "").strip()
    if not scope:
        from nz_coder.providers.configuration import provider_connection

        scope = provider_connection(provider_id).credential_scope_id
    adapter = (
        f"{type(provider).__module__}.{type(provider).__qualname__}"
    ).casefold()
    payload = json.dumps(
        {
            "adapter": adapter,
            "endpoint": normalized_endpoint,
            "credential_scope": scope.casefold(),
            "protocol": _PROVIDER_PRIVATE_PROTOCOL_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"provider-instance-{hashlib.sha256(payload).hexdigest()[:32]}"


def _normalize_provider_endpoint(value: object) -> str | None:
    """Canonicalize a safe endpoint identity, rejecting secret-bearing forms."""
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    path = "/" + "/".join(segment for segment in parsed.path.split("/") if segment)
    if path == "/":
        path = ""
    return urlunsplit((scheme, authority, path, "", ""))
