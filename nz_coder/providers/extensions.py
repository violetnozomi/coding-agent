"""Discover and load explicitly selected, installed Provider adapters."""
from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata as importlib_metadata
import re
from typing import Any, Callable, Iterable

from nz_coder.providers.base import ModelProvider


ENTRY_POINT_GROUP = "nz_coder.providers"
PROVIDER_API_VERSION = 1
_MAX_ENTRY_POINTS = 100
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


class ProviderExtensionNotFound(ValueError):
    """Raised when an explicitly selected Provider has no installed adapter."""


@dataclass(frozen=True)
class InstalledProviderExtension:
    """Secret-free metadata for one installed Provider entry point."""

    provider: str
    target: str
    distribution: str = ""


def installed_provider_extensions() -> tuple[InstalledProviderExtension, ...]:
    """List installed adapters without importing or executing their code."""
    result = []
    for entry_point in _provider_entry_points():
        provider = _normalize_provider_id(getattr(entry_point, "name", ""))
        if not provider:
            continue
        distribution = getattr(entry_point, "dist", None)
        result.append(
            InstalledProviderExtension(
                provider=provider,
                target=str(getattr(entry_point, "value", "")),
                distribution=str(getattr(distribution, "name", "") or ""),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.provider, item.target)))


def has_provider_extension(provider: str) -> bool:
    """Return whether exactly one installed entry point owns ``provider``."""
    normalized = _required_provider_id(provider)
    return sum(
        1
        for item in installed_provider_extensions()
        if item.provider == normalized
    ) == 1


def create_extension_provider(
    provider: str,
    *,
    api_key: str,
    base_url: str,
    client_factory: Callable[..., Any] | None = None,
) -> ModelProvider:
    """Load one explicitly selected adapter and validate its runtime contract."""
    normalized = _required_provider_id(provider)
    matches = [
        entry_point
        for entry_point in _provider_entry_points()
        if _normalize_provider_id(getattr(entry_point, "name", "")) == normalized
    ]
    if not matches:
        raise ProviderExtensionNotFound(
            f"No installed Provider adapter for '{normalized}'"
        )
    if len(matches) > 1:
        targets = ", ".join(sorted(str(item.value) for item in matches))
        raise ValueError(
            f"Multiple installed Provider adapters claim '{normalized}': {targets}"
        )
    entry_point = matches[0]
    try:
        factory = entry_point.load()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to import Provider adapter '{normalized}': {exc}"
        ) from exc
    if not callable(factory):
        raise TypeError(f"Provider adapter '{normalized}' entry point is not callable")
    version = getattr(factory, "nz_coder_provider_api_version", PROVIDER_API_VERSION)
    if version != PROVIDER_API_VERSION:
        raise RuntimeError(
            f"Provider adapter '{normalized}' uses API version {version!r}; "
            f"expected {PROVIDER_API_VERSION}"
        )
    try:
        adapter = factory(
            provider_name=normalized,
            api_key=api_key,
            base_url=base_url,
            client_factory=client_factory,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Provider adapter '{normalized}' initialization failed: {exc}"
        ) from exc
    _validate_adapter(normalized, adapter)
    return adapter


def _provider_entry_points() -> tuple[Any, ...]:
    points = importlib_metadata.entry_points()
    if hasattr(points, "select"):
        selected: Iterable[Any] = points.select(group=ENTRY_POINT_GROUP)
    elif isinstance(points, dict):
        selected = points.get(ENTRY_POINT_GROUP, ())
    else:
        selected = (
            point
            for point in points
            if getattr(point, "group", None) == ENTRY_POINT_GROUP
        )
    result = tuple(selected)
    if len(result) > _MAX_ENTRY_POINTS:
        raise RuntimeError(
            f"Too many installed Provider adapters ({len(result)} > {_MAX_ENTRY_POINTS})"
        )
    return result


def _validate_adapter(provider: str, adapter: Any) -> None:
    if adapter is None:
        raise TypeError(f"Provider adapter '{provider}' returned None")
    name = getattr(adapter, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise TypeError(f"Provider adapter '{provider}' must expose a non-empty name")
    for method in ("create_client", "create_completion", "capabilities"):
        if not callable(getattr(adapter, method, None)):
            raise TypeError(
                f"Provider adapter '{provider}' must implement callable {method}()"
            )


def _required_provider_id(provider: str) -> str:
    normalized = _normalize_provider_id(provider)
    if not normalized:
        raise ValueError(f"Invalid Provider adapter id: {provider!r}")
    return normalized


def _normalize_provider_id(provider: Any) -> str:
    if not isinstance(provider, str):
        return ""
    normalized = provider.strip().lower()
    return normalized if _PROVIDER_ID.fullmatch(normalized) else ""


__all__ = [
    "ENTRY_POINT_GROUP",
    "PROVIDER_API_VERSION",
    "InstalledProviderExtension",
    "ProviderExtensionNotFound",
    "create_extension_provider",
    "has_provider_extension",
    "installed_provider_extensions",
]
