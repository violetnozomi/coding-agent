"""Secret-free inspection of the active Provider connection configuration."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import os
import threading
import uuid

from nz_coder.foundation import config
from nz_coder.foundation.workspace_trust import (
    ConfigSource,
    ConfigValidationError,
    WorkspaceTrustStore,
    default_trust_store_path,
)


_ANTHROPIC_NAMES = {"anthropic", "claude"}
_GEMINI_NAMES = {"gemini", "google"}
_OPENAI_RESPONSES_NAMES = {"codex", "openai-responses", "openai_responses"}
_CONNECTION_OVERRIDES: ContextVar[dict[str, tuple[str, str, str]]] = ContextVar(
    "nz_coder_provider_connection_overrides",
    default={},
)
_GENERATION_LOCK = threading.RLock()
_GENERATION_SALT = os.urandom(32)
_ENVIRONMENT_GENERATIONS: dict[str, tuple[bytes, str]] = {}


@dataclass(frozen=True)
class ProviderConnection:
    """Credential presence and endpoint for one selected Provider."""

    provider: str
    credential_name: str
    api_key: str
    base_url: str
    credential_scope_id: str
    credential_source: str
    endpoint_source: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())


def provider_connection(provider: str, *, config_snapshot=None) -> ProviderConnection:
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
            f"user-connect:{family}:{override[2]}",
            "user",
            "user",
        )
    def selected(key: str, default: str = "") -> str:
        if config_snapshot is not None:
            return config_snapshot.get(key, default)
        return str(getattr(config, key, default))

    def source(key: str) -> str:
        if config_snapshot is None:
            return "environment" if key in os.environ else "host-process"
        return config_snapshot.value(key).source.value

    shared_key = selected("API_KEY", "")
    shared_source = source("API_KEY")
    if normalized in _ANTHROPIC_NAMES:
        specific = selected("ANTHROPIC_API_KEY", shared_key)
        api_key = specific or shared_key
        connection = ProviderConnection(
            normalized,
            "ANTHROPIC_API_KEY (or API_KEY)",
            api_key,
            selected("ANTHROPIC_API_BASE_URL", "https://api.anthropic.com"),
            _environment_credential_scope("anthropic", api_key),
            source("ANTHROPIC_API_KEY") if specific else shared_source,
            source("ANTHROPIC_API_BASE_URL"),
        )
        return _enforce_endpoint_delegation(connection, "anthropic", config_snapshot)
    if normalized in _GEMINI_NAMES:
        specific = selected("GEMINI_API_KEY", shared_key)
        api_key = specific or shared_key
        connection = ProviderConnection(
            normalized,
            "GEMINI_API_KEY (or API_KEY)",
            api_key,
            selected(
                "GEMINI_API_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            _environment_credential_scope("gemini", api_key),
            source("GEMINI_API_KEY") if specific else shared_source,
            source("GEMINI_API_BASE_URL"),
        )
        return _enforce_endpoint_delegation(connection, "gemini", config_snapshot)
    if normalized in _OPENAI_RESPONSES_NAMES:
        specific = selected("OPENAI_API_KEY", shared_key)
        api_key = specific or shared_key
        connection = ProviderConnection(
            normalized,
            "OPENAI_API_KEY (or API_KEY)",
            api_key,
            selected("OPENAI_API_BASE_URL", "https://api.openai.com/v1"),
            _environment_credential_scope("openai-responses", api_key),
            source("OPENAI_API_KEY") if specific else shared_source,
            source("OPENAI_API_BASE_URL"),
        )
        return _enforce_endpoint_delegation(
            connection, "openai-responses", config_snapshot,
        )
    connection = ProviderConnection(
        normalized,
        "API_KEY",
        shared_key,
        selected("API_BASE_URL", "https://api.deepseek.com"),
        _environment_credential_scope("openai-compatible", shared_key),
        shared_source,
        source("API_BASE_URL"),
    )
    return _enforce_endpoint_delegation(
        connection, "openai-compatible", config_snapshot,
    )


_OFFICIAL_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openai-responses": "https://api.openai.com/v1",
    "openai-compatible": "https://api.deepseek.com",
}


def _endpoint_fingerprint(family: str, endpoint: str) -> str:
    payload = f"{family}\0{str(endpoint).strip().rstrip('/')}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _enforce_endpoint_delegation(
    connection: ProviderConnection,
    family: str,
    config_snapshot,
) -> ProviderConnection:
    if config_snapshot is None:
        return connection
    if (
        connection.endpoint_source != ConfigSource.TRUSTED_WORKSPACE.value
        or connection.credential_source not in {
            ConfigSource.ENVIRONMENT.value, ConfigSource.USER.value,
        }
        or connection.base_url.rstrip("/") == _OFFICIAL_ENDPOINTS[family].rstrip("/")
    ):
        return connection
    store = WorkspaceTrustStore(default_trust_store_path())
    trusted = store.is_trusted(
        config_snapshot.workspace,
        f"provider-endpoint:{family}",
        _endpoint_fingerprint(family, connection.base_url),
    )
    if not trusted:
        raise ConfigValidationError(
            "Workspace Provider endpoint requires explicit credential delegation"
        )
    return connection


def trust_provider_endpoint(
    provider: str,
    *,
    config_snapshot,
    trust_store: WorkspaceTrustStore | None = None,
) -> None:
    """Delegate owner credentials to the exact current workspace endpoint."""
    normalized = str(provider or "").strip().lower()
    family = _provider_family(normalized)
    endpoint_keys = {
        "anthropic": "ANTHROPIC_API_BASE_URL",
        "gemini": "GEMINI_API_BASE_URL",
        "openai-responses": "OPENAI_API_BASE_URL",
        "openai-compatible": "API_BASE_URL",
    }
    record = config_snapshot.value(endpoint_keys[family])
    if record.source is not ConfigSource.TRUSTED_WORKSPACE:
        raise ConfigValidationError(
            "Provider endpoint delegation requires a trusted-workspace endpoint"
        )
    (trust_store or WorkspaceTrustStore(default_trust_store_path())).trust(
        config_snapshot.workspace,
        f"provider-endpoint:{family}",
        _endpoint_fingerprint(family, record.value),
    )


def set_provider_connection_override(provider: str, api_key: str, base_url: str) -> None:
    """Set one context-local live connection after an explicit terminal login."""
    family = _provider_family(str(provider or "").strip().lower())
    current = dict(_CONNECTION_OVERRIDES.get())
    key = str(api_key)
    endpoint = str(base_url)
    previous = current.get(family)
    generation = (
        previous[2]
        if previous is not None and previous[:2] == (key, endpoint)
        else uuid.uuid4().hex
    )
    current[family] = (key, endpoint, generation)
    _CONNECTION_OVERRIDES.set(current)


def clear_provider_connection_overrides() -> None:
    """Clear live connection overrides in the current execution context."""
    _CONNECTION_OVERRIDES.set({})


def _environment_credential_scope(family: str, api_key: str) -> str:
    """Return a random generation that rotates when credential material does."""
    fingerprint = hmac.new(
        _GENERATION_SALT,
        str(api_key).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    with _GENERATION_LOCK:
        previous = _ENVIRONMENT_GENERATIONS.get(family)
        if previous is not None and hmac.compare_digest(previous[0], fingerprint):
            generation = previous[1]
        else:
            generation = uuid.uuid4().hex
            _ENVIRONMENT_GENERATIONS[family] = (fingerprint, generation)
    return f"environment:{family}:{generation}"


def _provider_family(provider: str) -> str:
    if provider in _ANTHROPIC_NAMES:
        return "anthropic"
    if provider in _GEMINI_NAMES:
        return "gemini"
    if provider in _OPENAI_RESPONSES_NAMES:
        return "openai-responses"
    return "openai-compatible"
