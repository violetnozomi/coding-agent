"""Workspace model discovery, bounded caching, and active selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nz_coder.foundation import config
from nz_coder.foundation.json_safety import reject_nonstandard_json_constant
from nz_coder.foundation.workspace_trust import (
    WorkspaceTrustStore,
    default_trust_store_path,
    load_config_snapshot,
)
from nz_coder.providers.capabilities import (
    ModelCapabilities,
    configured_model_capabilities,
    load_model_catalog_file,
)
from nz_coder.runtime.process.workdir import current_workdir

_MAX_RESPONSE_BYTES = 2_000_000
_MAX_STATE_BYTES = 2_000_000
_MAX_DISCOVERY_PAGES = 20
_MAX_DISCOVERED_MODELS = 10_000
_MAX_DISCOVERY_TIMEOUT_SECONDS = 300.0
_CACHE_RELATIVE_PATH = Path(".nz-coder/models/catalog.json")
_SELECTION_RELATIVE_PATH = Path(".nz-coder/models/selection.json")
_OPENAI_NAMES = {
    "alibaba-cn", "cerebras", "codex", "dashscope", "deepseek", "groq",
    "kimi", "mistral", "moonshot", "openai", "openai-compatible",
    "openai-responses", "openai_compatible", "openai_responses", "openrouter",
    "siliconflow", "together", "xai", "zhipu",
}
_SELECTABLE_PROVIDER_NAMES = _OPENAI_NAMES | {"anthropic", "claude", "gemini", "google"}


@dataclass(frozen=True)
class ModelSelection:
    """One workspace-owned provider/model choice."""

    provider: str
    model_id: str
    variant: str | None = None
    source: str = "configuration"


@dataclass(frozen=True)
class DiscoveredModel:
    """One model returned by a provider discovery endpoint."""

    provider: str
    model_id: str
    display_name: str = ""
    owned_by: str = ""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Model discovery redirects are disabled")


def active_model_selection(workspace: Path | None = None) -> ModelSelection:
    """Return the workspace selection, falling back to environment config."""
    root = (workspace or current_workdir()).resolve()
    selection_path = root / _SELECTION_RELATIVE_PATH
    data = _read_state(selection_path, required=False)
    if data is not None:
        fingerprint = _state_fingerprint(data)
        try:
            trusted = (
                WorkspaceTrustStore(default_trust_store_path()).is_trusted(
                    root,
                    "workspace-model-selection",
                    fingerprint,
                )
                or load_config_snapshot(root).control_plane_trusted
            )
        except (OSError, ValueError):
            trusted = False
        if not trusted:
            data = None
    if data is not None:
        provider = _nonempty_string(data.get("provider"), "selection provider")
        model_id = _nonempty_string(data.get("model_id"), "selection model_id")
        variant = data.get("variant")
        if variant is not None and (not isinstance(variant, str) or not variant.strip()):
            raise ValueError("Model selection variant must be a non-empty string")
        return ModelSelection(provider, model_id, variant, "workspace")
    return ModelSelection(
        str(config.MODEL_PROVIDER).strip().lower(),
        str(config.MODEL_ID).strip(),
        str(config.MODEL_VARIANT).strip() or None,
    )


def save_model_selection(
    provider: str,
    model_id: str,
    *,
    variant: str | None = None,
    workspace: Path | None = None,
) -> ModelSelection:
    """Atomically persist a validated workspace model selection."""
    root = (workspace or current_workdir()).resolve()
    normalized_provider = _nonempty_string(provider, "provider").lower()
    normalized_model = _nonempty_string(model_id, "model_id")
    if normalized_provider not in _SELECTABLE_PROVIDER_NAMES:
        from nz_coder.providers.extensions import has_provider_extension

        if not has_provider_extension(normalized_provider):
            raise ValueError(f"Unknown model provider '{normalized_provider}'")
    normalized_variant = None
    if variant is not None:
        normalized_variant = _nonempty_string(variant, "variant")
    capability = configured_model_capabilities(normalized_provider, normalized_model)
    if normalized_variant and normalized_variant not in capability.available_variants:
        choices = ", ".join(capability.available_variants) or "none"
        raise ValueError(
            f"Unknown variant '{normalized_variant}' for {normalized_provider}/{normalized_model}; "
            f"available: {choices}"
        )
    payload = {
        "version": 1,
        "provider": normalized_provider,
        "model_id": normalized_model,
        "variant": normalized_variant,
    }
    _write_state(root / _SELECTION_RELATIVE_PATH, payload)
    WorkspaceTrustStore(default_trust_store_path()).trust(
        root,
        "workspace-model-selection",
        _state_fingerprint(payload),
    )
    return ModelSelection(normalized_provider, normalized_model, normalized_variant, "workspace")


def clear_model_selection(workspace: Path | None = None) -> bool:
    """Remove the workspace selection and restore environment-backed defaults."""
    root = (workspace or current_workdir()).resolve()
    target = _safe_state_path(root, root / _SELECTION_RELATIVE_PATH)
    removed = False
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    else:
        removed = True
    trust_removed = WorkspaceTrustStore(default_trust_store_path()).remove(
        root,
        "workspace-model-selection",
    )
    return removed or trust_removed


def discover_models(
    provider: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    workspace: Path | None = None,
    timeout_seconds: float = 20.0,
) -> list[DiscoveredModel]:
    """Explicitly query one configured provider and replace its cache entry."""
    timeout = _validated_timeout(timeout_seconds)
    selected = (provider or active_model_selection(workspace).provider).strip().lower()
    key, base = _provider_connection(selected, api_key, base_url)
    url, headers, parser = _discovery_request(selected, base, key)
    models: list[DiscoveredModel] = []
    for _ in range(_MAX_DISCOVERY_PAGES):
        payload = _get_json(url, headers, timeout)
        models.extend(parser(selected, payload))
        if len(models) > _MAX_DISCOVERED_MODELS:
            raise RuntimeError("Model discovery returned too many models")
        next_url = _next_page_url(selected, url, payload)
        if not next_url:
            break
        url = next_url
    else:
        raise RuntimeError("Model discovery exceeded the pagination limit")
    models = _deduplicate(models)
    if not models:
        raise RuntimeError(f"Provider '{selected}' returned no usable models")
    _update_cache(selected, models, workspace=workspace)
    return models


def cached_models(
    provider: str | None = None,
    *,
    workspace: Path | None = None,
) -> list[DiscoveredModel]:
    """Read cached discovery results without network access."""
    root = (workspace or current_workdir()).resolve()
    data = _read_state(root / _CACHE_RELATIVE_PATH, required=False) or {}
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return []
    wanted = provider.strip().lower() if provider else None
    result: list[DiscoveredModel] = []
    for provider_name, record in providers.items():
        if wanted and provider_name != wanted:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("models"), list):
            continue
        for item in record["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("model_id"), str):
                continue
            result.append(
                DiscoveredModel(
                    provider=provider_name,
                    model_id=item["model_id"],
                    display_name=str(item.get("display_name") or ""),
                    owned_by=str(item.get("owned_by") or ""),
                )
            )
    return sorted(result, key=lambda item: (item.provider, item.model_id.lower()))


def configured_catalog_models(workspace: Path | None = None) -> list[DiscoveredModel]:
    """Enumerate exact local catalog keys for offline listing."""
    catalog: Any = getattr(config, "MODEL_CATALOG_JSON", "")
    if catalog:
        try:
            catalog = json.loads(catalog) if isinstance(catalog, str) else dict(catalog)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid MODEL_CATALOG_JSON: {exc}") from exc
    elif getattr(config, "MODEL_CATALOG_PATH", ""):
        catalog = load_model_catalog_file(
            config.MODEL_CATALOG_PATH,
            workspace=workspace,
        )
    else:
        return []
    models = catalog.get("models", catalog) if isinstance(catalog, dict) else {}
    if not isinstance(models, dict):
        raise ValueError("MODEL_CATALOG_JSON 'models' must be an object")
    result = []
    for key in models:
        if not isinstance(key, str) or "/" not in key:
            continue
        provider, model_id = key.split("/", 1)
        if provider and model_id:
            result.append(DiscoveredModel(provider.lower(), model_id))
    return sorted(result, key=lambda item: (item.provider, item.model_id.lower()))


def model_details(model: DiscoveredModel) -> ModelCapabilities:
    """Resolve cached/discovered ids through the exact local capability policy."""
    return configured_model_capabilities(model.provider, model.model_id)


def cache_status(workspace: Path | None = None) -> dict[str, str]:
    """Return provider discovery timestamps without exposing credentials."""
    root = (workspace or current_workdir()).resolve()
    data = _read_state(root / _CACHE_RELATIVE_PATH, required=False) or {}
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return {}
    return {
        name: str(record.get("fetched_at") or "")
        for name, record in providers.items()
        if isinstance(name, str) and isinstance(record, dict)
    }


def _provider_connection(
    provider: str,
    api_key: str | None,
    base_url: str | None,
) -> tuple[str, str]:
    if provider in _SELECTABLE_PROVIDER_NAMES:
        from nz_coder.providers.configuration import provider_connection

        connection = provider_connection(provider)
        return (
            connection.api_key if api_key is None else api_key,
            connection.base_url if base_url is None else base_url,
        )
    raise ValueError(f"Provider '{provider}' does not support model discovery")


def _discovery_request(provider: str, base_url: str, api_key: str):
    if not api_key:
        raise ValueError(f"No API key configured for provider '{provider}'")
    base = base_url.rstrip("/")
    _validate_discovery_url(base)
    if provider in {"anthropic", "claude"}:
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        return url, {
            "x-api-key": api_key,
            "anthropic-version": config.ANTHROPIC_API_VERSION,
        }, _parse_anthropic
    if provider in {"gemini", "google"}:
        url = f"{base}/models"
        return url, {"x-goog-api-key": api_key}, _parse_gemini
    url = f"{base}/models"
    return url, {"Authorization": f"Bearer {api_key}"}, _parse_openai


def _validate_discovery_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc:
        return
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return
    raise ValueError("Model discovery requires HTTPS or a loopback HTTP endpoint")


def _get_json(url: str, headers: dict[str, str], timeout_seconds: float) -> dict:
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, headers={"Accept": "application/json", **headers}, method="GET")
    try:
        with opener.open(request, timeout=max(0.1, float(timeout_seconds))) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("Model discovery response is too large")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Model discovery HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Model discovery connection error: {exc.reason}") from exc
    if len(body) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("Model discovery response is too large")
    try:
        data = json.loads(
            body.decode("utf-8"),
            parse_constant=reject_nonstandard_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Model discovery returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Model discovery returned a non-object JSON response")
    return data


def _next_page_url(provider: str, url: str, payload: dict) -> str | None:
    token = None
    parameter = None
    if provider in {"gemini", "google"}:
        token = payload.get("nextPageToken")
        parameter = "pageToken"
    elif payload.get("has_more") is True:
        token = payload.get("last_id")
        parameter = "after_id" if provider in {"anthropic", "claude"} else "after"
    if not isinstance(token, str) or not token or not parameter:
        return None
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[parameter] = token
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _parse_openai(provider: str, payload: dict) -> list[DiscoveredModel]:
    return _parse_items(provider, payload.get("data"), "id", "owned_by")


def _parse_anthropic(provider: str, payload: dict) -> list[DiscoveredModel]:
    return _parse_items(provider, payload.get("data"), "id", "display_name")


def _parse_gemini(provider: str, payload: dict) -> list[DiscoveredModel]:
    result = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        model_id = str(item.get("name") or "").removeprefix("models/").strip()
        if model_id:
            result.append(
                DiscoveredModel(provider, model_id, str(item.get("displayName") or ""))
            )
    return _deduplicate(result)


def _parse_items(
    provider: str,
    items: Any,
    id_key: str,
    metadata_key: str,
) -> list[DiscoveredModel]:
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get(id_key) or "").strip()
        if not model_id:
            continue
        metadata = str(item.get(metadata_key) or "")
        result.append(
            DiscoveredModel(
                provider,
                model_id,
                metadata if metadata_key == "display_name" else "",
                metadata if metadata_key == "owned_by" else "",
            )
        )
    return _deduplicate(result)


def _deduplicate(models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    unique = {(item.provider, item.model_id): item for item in models}
    return sorted(unique.values(), key=lambda item: item.model_id.lower())


def _update_cache(
    provider: str,
    models: list[DiscoveredModel],
    *,
    workspace: Path | None,
) -> None:
    root = (workspace or current_workdir()).resolve()
    target = root / _CACHE_RELATIVE_PATH
    data = _read_state(target, required=False) or {"version": 1, "providers": {}}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers[provider] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "models": [asdict(item) for item in models],
    }
    _write_state(target, {"version": 1, "providers": providers})


def _safe_state_path(root: Path, target: Path) -> Path:
    root = root.resolve()
    resolved = target.parent.resolve(strict=False) / target.name
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Model state path escapes workspace: {target}") from exc
    if resolved.is_symlink():
        raise ValueError(f"Model state file must not be a symbolic link: {target}")
    return resolved


def _read_state(target: Path, *, required: bool) -> dict | None:
    root = current_workdir().resolve() if not target.is_absolute() else None
    if root is not None:
        target = root / target
    workspace = target
    for _ in _CACHE_RELATIVE_PATH.parts:
        workspace = workspace.parent
    target = _safe_state_path(workspace, target)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        if required:
            raise
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATE_BYTES:
            raise ValueError(f"Invalid model state file: {target}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(_MAX_STATE_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        data = json.loads(
            payload.decode("utf-8"),
            parse_constant=reject_nonstandard_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid model state file '{target}': {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Model state file '{target}' must contain an object")
    return data


def _write_state(target: Path, payload: dict) -> None:
    workspace = target
    for _ in _CACHE_RELATIVE_PATH.parts:
        workspace = workspace.parent
    target = _safe_state_path(workspace, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_STATE_BYTES:
        raise ValueError("Model state exceeds the size limit")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Model {name} must be a non-empty string")
    return value.strip()


def _state_fingerprint(payload: dict) -> str:
    """Return a content identity without exposing provider configuration."""
    import hashlib

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Model discovery timeout must be a positive finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "Model discovery timeout must be a positive finite number"
        ) from exc
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > _MAX_DISCOVERY_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "Model discovery timeout must be a positive finite number "
            f"no greater than {_MAX_DISCOVERY_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout
