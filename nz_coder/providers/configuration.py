"""Secret-free inspection of the active Provider connection configuration."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from nz_coder import config


_ANTHROPIC_NAMES = {"anthropic", "claude"}
_GEMINI_NAMES = {"gemini", "google"}
_OPENAI_RESPONSES_NAMES = {"codex", "openai-responses", "openai_responses"}
_CONNECTION_OVERRIDES: ContextVar[dict[str, tuple[str, str]]] = ContextVar(
    "nz_coder_provider_connection_overrides",
    default={},
)


@dataclass(frozen=True)
class ProviderConnection:
    """Credential presence and endpoint for one selected Provider."""

    provider: str
    credential_name: str
    api_key: str
    base_url: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())


def provider_connection(provider: str) -> ProviderConnection:
    """Return the same credential/base selection used by Provider adapters."""
    normalized = str(provider or "").strip().lower()
    family = _provider_family(normalized)
    override = _CONNECTION_OVERRIDES.get().get(family)
    if override is not None:
        credential_names = {
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openai-responses": "OPENAI_API_KEY",
            "openai-compatible": "API_KEY",
        }
        return ProviderConnection(
            normalized,
            credential_names[family],
            override[0],
            override[1],
        )
    if normalized in _ANTHROPIC_NAMES:
        return ProviderConnection(
            normalized,
            "ANTHROPIC_API_KEY (or API_KEY)",
            config.ANTHROPIC_API_KEY,
            config.ANTHROPIC_API_BASE_URL,
        )
    if normalized in _GEMINI_NAMES:
        return ProviderConnection(
            normalized,
            "GEMINI_API_KEY (or API_KEY)",
            config.GEMINI_API_KEY,
            config.GEMINI_API_BASE_URL,
        )
    if normalized in _OPENAI_RESPONSES_NAMES:
        return ProviderConnection(
            normalized,
            "OPENAI_API_KEY (or API_KEY)",
            config.OPENAI_API_KEY,
            config.OPENAI_API_BASE_URL,
        )
    return ProviderConnection(
        normalized,
        "API_KEY",
        config.API_KEY,
        config.API_BASE_URL,
    )


def set_provider_connection_override(provider: str, api_key: str, base_url: str) -> None:
    """Set one context-local live connection after an explicit terminal login."""
    family = _provider_family(str(provider or "").strip().lower())
    current = dict(_CONNECTION_OVERRIDES.get())
    current[family] = (str(api_key), str(base_url))
    _CONNECTION_OVERRIDES.set(current)


def clear_provider_connection_overrides() -> None:
    """Clear live connection overrides in the current execution context."""
    _CONNECTION_OVERRIDES.set({})


def _provider_family(provider: str) -> str:
    if provider in _ANTHROPIC_NAMES:
        return "anthropic"
    if provider in _GEMINI_NAMES:
        return "gemini"
    if provider in _OPENAI_RESPONSES_NAMES:
        return "openai-responses"
    return "openai-compatible"
