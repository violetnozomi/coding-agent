"""Bounded models.dev-compatible capability registry cache."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nz_coder import config
from nz_coder.runtime.workdir import current_workdir

_MAX_REGISTRY_BYTES = 10_000_000
_MAX_PROVIDERS = 500
_MAX_MODELS = 50_000
_REGISTRY_RELATIVE_PATH = Path(".nz-coder/models/registry.json")
_LOCK_RELATIVE_PATH = Path(".nz-coder/models/registry.lock")
_PROVIDER_ALIASES = {
    "alibaba": "dashscope",
    "anthropic": "anthropic",
    "cerebras": "cerebras",
    "deepseek": "deepseek",
    "google": "gemini",
    "groq": "groq",
    "mistral": "mistral",
    "moonshotai": "moonshot",
    "openai": "openai",
    "openrouter": "openrouter",
    "togetherai": "together",
    "xai": "xai",
    "zhipuai": "zhipu",
}


@dataclass(frozen=True)
class RegistrySyncResult:
    """Outcome of one registry freshness check or network refresh."""

    refreshed: bool
    provider_count: int
    model_count: int
    source: str


@dataclass(frozen=True)
class ModelPricing:
    """USD rates per one million tokens from a registry snapshot."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    context_over_200k: ModelPricing | None = None


@dataclass(frozen=True)
class RegistryModel:
    """One normalized registry model for CLI projection."""

    provider: str
    model_id: str
    name: str
    release_date: str
    api_model_id: str = ""
    adapter: str = ""
    endpoint: str = ""
    pricing: ModelPricing | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Model registry redirects are disabled")


def sync_model_registry(
    source_url: str | None = None,
    *,
    force: bool = False,
    workspace: Path | None = None,
    timeout_seconds: float = 10.0,
) -> RegistrySyncResult:
    """Refresh a models.dev-compatible registry under a cross-process lock."""
    root = (workspace or current_workdir()).resolve()
    source = str(
        source_url or getattr(config, "MODEL_REGISTRY_URL", "https://models.dev/api.json")
    ).strip()
    _validate_url(source)
    target = _registry_path(root)
    if not force and _is_fresh(target, source):
        return _result(load_registry_snapshot(root, strict=True), False)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _safe_child(root, root / _LOCK_RELATIVE_PATH)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        _lock_file(descriptor)
        if not force and _is_fresh(target, source):
            return _result(load_registry_snapshot(root, strict=True), False)
        raw = _get_json(source, timeout_seconds)
        snapshot = _normalize_registry(raw, source)
        _write_snapshot(target, snapshot)
        return _result(snapshot, True)
    finally:
        _unlock_file(descriptor)
        os.close(descriptor)


def load_registry_snapshot(
    workspace: Path | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Load the last valid normalized registry without network access."""
    root = (workspace or current_workdir()).resolve()
    target = _registry_path(root)
    try:
        data = _read_json(target)
        _validate_snapshot(data)
        return data
    except (OSError, ValueError):
        if strict:
            raise
        return {}


def registry_capability_catalog(workspace: Path | None = None) -> dict[str, Any]:
    """Project registry records into the exact capability catalog schema."""
    snapshot = load_registry_snapshot(workspace)
    models: dict[str, dict[str, Any]] = {}
    for provider, provider_record in (snapshot.get("providers") or {}).items():
        if not isinstance(provider_record, dict):
            continue
        for model_id, record in (provider_record.get("models") or {}).items():
            if isinstance(record, dict) and isinstance(record.get("capabilities"), dict):
                models[f"{provider}/{model_id}"] = dict(record["capabilities"])
    return {"models": models} if models else {}


def registry_models(workspace: Path | None = None) -> list[RegistryModel]:
    """List normalized registry identities for the offline CLI."""
    snapshot = load_registry_snapshot(workspace)
    result = []
    for provider, provider_record in (snapshot.get("providers") or {}).items():
        if not isinstance(provider_record, dict):
            continue
        for model_id, record in (provider_record.get("models") or {}).items():
            if not isinstance(record, dict):
                continue
            result.append(
                RegistryModel(
                    provider,
                    model_id,
                    str(record.get("name") or model_id),
                    str(record.get("release_date") or ""),
                    str(record.get("api_model_id") or model_id),
                    str(record.get("adapter") or ""),
                    str(record.get("endpoint") or ""),
                    _pricing_from_record(record.get("pricing")),
                )
            )
    return sorted(result, key=lambda item: (item.provider, item.model_id.lower()))


def registry_runtime_model(
    provider: str,
    model_id: str,
    workspace: Path | None = None,
) -> RegistryModel | None:
    """Resolve one logical registry model to its Provider wire identity."""
    wanted_provider = str(provider or "").strip().lower()
    wanted_model = str(model_id or "").strip()
    if not wanted_provider or not wanted_model:
        return None
    snapshot = load_registry_snapshot(workspace)
    provider_record = (snapshot.get("providers") or {}).get(wanted_provider)
    if not isinstance(provider_record, dict):
        return None
    record = (provider_record.get("models") or {}).get(wanted_model)
    if not isinstance(record, dict):
        return None
    return RegistryModel(
        wanted_provider,
        wanted_model,
        str(record.get("name") or wanted_model),
        str(record.get("release_date") or ""),
        str(record.get("api_model_id") or wanted_model),
        str(record.get("adapter") or ""),
        str(record.get("endpoint") or ""),
        _pricing_from_record(record.get("pricing")),
    )


def registry_status(workspace: Path | None = None) -> dict[str, Any]:
    """Return secret-free registry cache metadata."""
    snapshot = load_registry_snapshot(workspace)
    if not snapshot:
        return {"available": False}
    result = _result(snapshot, False)
    return {
        "available": True,
        "source": result.source,
        "fetched_at": str(snapshot.get("fetched_at") or ""),
        "provider_count": result.provider_count,
        "model_count": result.model_count,
        "fresh": _is_fresh(
            _registry_path((workspace or current_workdir()).resolve()),
            result.source,
        ),
    }


def _normalize_registry(raw: dict[str, Any], source: str) -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    model_count = 0
    from nz_coder.providers.extensions import installed_provider_extensions

    extension_names = {item.provider for item in installed_provider_extensions()}
    if len(raw) > _MAX_PROVIDERS:
        raise ValueError("Model registry contains too many providers")
    for source_id, source_provider in raw.items():
        if not isinstance(source_id, str) or not isinstance(source_provider, dict):
            continue
        source_provider_id = source_id.strip().lower()
        provider = _PROVIDER_ALIASES.get(source_provider_id)
        if provider is None and source_provider_id in extension_names:
            provider = source_provider_id
        if not provider:
            continue
        source_models = source_provider.get("models")
        if not isinstance(source_models, dict):
            continue
        normalized_models: dict[str, dict[str, Any]] = {}
        for map_id, source_model in source_models.items():
            if not isinstance(map_id, str) or not isinstance(source_model, dict):
                continue
            model_id = map_id.strip()
            api_model_id = str(source_model.get("id") or map_id).strip()
            if not _valid_model_id(model_id) or not _valid_model_id(api_model_id):
                continue
            capabilities = _normalize_capabilities(source_model)
            if not capabilities:
                continue
            model_provider = source_model.get("provider")
            if not isinstance(model_provider, dict):
                model_provider = {}
            adapter = _bounded_string(
                model_provider.get("npm") or source_provider.get("npm"),
                500,
            )
            endpoint = _bounded_string(
                model_provider.get("api") or source_provider.get("api"),
                2_000,
            )
            normalized_models[model_id] = {
                "name": str(source_model.get("name") or model_id)[:500],
                "release_date": str(source_model.get("release_date") or "")[:50],
                "api_model_id": api_model_id,
                "adapter": adapter,
                "endpoint": endpoint,
                "capabilities": capabilities,
                **(
                    {"pricing": pricing}
                    if (pricing := _normalize_pricing(source_model.get("cost")))
                    is not None
                    else {}
                ),
            }
            model_count += 1
            if model_count > _MAX_MODELS:
                raise ValueError("Model registry contains too many models")
        if normalized_models:
            providers[provider] = {
                "name": str(source_provider.get("name") or source_id)[:300],
                "models": normalized_models,
            }
    if not providers:
        raise ValueError("Model registry contains no supported provider models")
    return {
        "version": 1,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }


def _normalize_capabilities(model: dict[str, Any]) -> dict[str, Any]:
    limit = model.get("limit")
    if not isinstance(limit, dict):
        return {}
    context = _positive_int(limit.get("context"))
    output = _positive_int(limit.get("output"))
    if context is None or output is None:
        return {}
    result: dict[str, Any] = {
        "context_tokens": context,
        "output_tokens": output,
    }
    family = model.get("family")
    if isinstance(family, str) and family.strip():
        result["family"] = family.strip()[:200]
    for source_name, target_name in (
        ("tool_call", "supports_tools"),
        ("reasoning", "supports_reasoning"),
        ("temperature", "supports_temperature"),
    ):
        value = model.get(source_name)
        if isinstance(value, bool):
            result[target_name] = value
    modalities = model.get("modalities")
    if isinstance(modalities, dict) and isinstance(modalities.get("input"), list):
        result["supports_image_input"] = "image" in modalities["input"]
    return result


def _normalize_pricing(value: Any) -> dict[str, Any] | None:
    """Validate models.dev USD-per-million pricing without inventing rates."""
    if not isinstance(value, dict):
        return None
    input_rate = _nonnegative_rate(value.get("input"))
    output_rate = _nonnegative_rate(value.get("output"))
    cache_read = _nonnegative_rate(value.get("cache_read", 0.0))
    cache_write = _nonnegative_rate(value.get("cache_write", 0.0))
    if (
        input_rate is None
        or output_rate is None
        or cache_read is None
        or cache_write is None
    ):
        return None
    result: dict[str, Any] = {
        "input": input_rate,
        "output": output_rate,
        "cache_read": cache_read,
        "cache_write": cache_write,
    }
    over = value.get("context_over_200k")
    if isinstance(over, dict):
        over_input = _nonnegative_rate(over.get("input"))
        over_output = _nonnegative_rate(over.get("output"))
        over_cache_read = _nonnegative_rate(over.get("cache_read", 0.0))
        over_cache_write = _nonnegative_rate(over.get("cache_write", 0.0))
        if all(
            item is not None
            for item in (
                over_input,
                over_output,
                over_cache_read,
                over_cache_write,
            )
        ):
            result["context_over_200k"] = {
                "input": over_input,
                "output": over_output,
                "cache_read": over_cache_read,
                "cache_write": over_cache_write,
            }
    return result


def _pricing_from_record(value: Any) -> ModelPricing | None:
    normalized = _normalize_pricing(value)
    if normalized is None:
        return None
    over = normalized.get("context_over_200k")
    return ModelPricing(
        input=normalized["input"],
        output=normalized["output"],
        cache_read=normalized["cache_read"],
        cache_write=normalized["cache_write"],
        context_over_200k=(
            ModelPricing(
                input=over["input"],
                output=over["output"],
                cache_read=over["cache_read"],
                cache_write=over["cache_write"],
            )
            if isinstance(over, dict)
            else None
        ),
    )


def _nonnegative_rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 1_000_000:
        return None
    return number


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 100_000_000:
        return None
    return int(number)


def _valid_model_id(value: str) -> bool:
    return bool(
        value
        and len(value) <= 300
        and all(character >= " " and character != "\x7f" for character in value)
    )


def _bounded_string(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if any(character < " " or character == "\x7f" for character in text):
        return ""
    return text[:limit]


def _validate_snapshot(data: Any) -> None:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Invalid model registry snapshot version")
    if not isinstance(data.get("source"), str) or not isinstance(data.get("providers"), dict):
        raise ValueError("Invalid model registry snapshot")


def _result(snapshot: dict[str, Any], refreshed: bool) -> RegistrySyncResult:
    providers = snapshot.get("providers") or {}
    count = sum(
        len(record.get("models") or {})
        for record in providers.values()
        if isinstance(record, dict)
    )
    return RegistrySyncResult(
        refreshed,
        len(providers),
        count,
        str(snapshot.get("source") or ""),
    )


def _is_fresh(target: Path, source: str) -> bool:
    try:
        info = target.stat()
        data = _read_json(target)
        _validate_snapshot(data)
    except (OSError, ValueError):
        return False
    ttl = max(0, int(getattr(config, "MODEL_REGISTRY_TTL_SECONDS", 300)))
    return data.get("source") == source and time.time() - info.st_mtime < ttl


def _get_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "nz-coder"})
    try:
        with opener.open(request, timeout=max(0.1, float(timeout_seconds))) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > _MAX_REGISTRY_BYTES:
                raise RuntimeError("Model registry response is too large")
            payload = response.read(_MAX_REGISTRY_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Model registry HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Model registry connection error: {exc.reason}") from exc
    if len(payload) > _MAX_REGISTRY_BYTES:
        raise RuntimeError("Model registry response is too large")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Model registry returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Model registry returned a non-object JSON response")
    return data


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Model registry URL must not contain credentials, query, or fragment")
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and (parsed.hostname or "").lower() in {
        "127.0.0.1", "localhost", "::1",
    }:
        return
    raise ValueError("Model registry requires HTTPS or a loopback HTTP endpoint")


def _registry_path(root: Path) -> Path:
    configured = Path(getattr(config, "MODEL_REGISTRY_PATH", str(_REGISTRY_RELATIVE_PATH)))
    if configured.is_absolute():
        raise ValueError("MODEL_REGISTRY_PATH must be workspace-relative")
    return _safe_child(root, root / configured)


def _safe_child(root: Path, target: Path) -> Path:
    root = root.resolve()
    resolved = target.parent.resolve(strict=False) / target.name
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Model registry path escapes workspace: {target}") from exc
    if resolved.is_symlink():
        raise ValueError(f"Model registry file must not be a symbolic link: {target}")
    return resolved


def _read_json(target: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(target, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_REGISTRY_BYTES:
            raise ValueError("Invalid model registry cache file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(_MAX_REGISTRY_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid model registry cache: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Model registry cache must contain an object")
    return data


def _write_snapshot(target: Path, snapshot: dict[str, Any]) -> None:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_REGISTRY_BYTES:
        raise ValueError("Normalized model registry exceeds the size limit")
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


def _lock_file(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except ImportError:
        return


def _unlock_file(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except ImportError:
        return
