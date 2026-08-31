"""Secure workspace credential persistence for the interactive provider dialog."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from nz_coder.providers.configuration import set_provider_connection_override
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.runtime.process.workdir import current_workdir


@dataclass(frozen=True)
class ProviderConnectSpec:
    """Environment contract and default endpoint for one provider family."""

    provider: str
    label: str
    credential_name: str
    endpoint_name: str
    default_endpoint: str


_SPECS = (
    ProviderConnectSpec(
        "openai-compatible", "OpenAI-compatible", "API_KEY", "API_BASE_URL",
        "https://api.openai.com/v1",
    ),
    ProviderConnectSpec(
        "openai-responses", "OpenAI Responses", "OPENAI_API_KEY",
        "OPENAI_API_BASE_URL", "https://api.openai.com/v1",
    ),
    ProviderConnectSpec(
        "anthropic", "Anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_API_BASE_URL",
        "https://api.anthropic.com",
    ),
    ProviderConnectSpec(
        "gemini", "Google Gemini", "GEMINI_API_KEY", "GEMINI_API_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta",
    ),
    ProviderConnectSpec(
        "deepseek", "DeepSeek", "API_KEY", "API_BASE_URL", "https://api.deepseek.com",
    ),
    ProviderConnectSpec(
        "openai", "OpenAI Chat Completions", "API_KEY", "API_BASE_URL",
        "https://api.openai.com/v1",
    ),
    ProviderConnectSpec(
        "openrouter", "OpenRouter", "API_KEY", "API_BASE_URL",
        "https://openrouter.ai/api/v1",
    ),
    ProviderConnectSpec(
        "dashscope", "DashScope", "API_KEY", "API_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    ProviderConnectSpec(
        "kimi", "Kimi", "API_KEY", "API_BASE_URL", "https://api.moonshot.cn/v1",
    ),
    ProviderConnectSpec(
        "moonshot", "Moonshot", "API_KEY", "API_BASE_URL", "https://api.moonshot.cn/v1",
    ),
    ProviderConnectSpec(
        "siliconflow", "SiliconFlow", "API_KEY", "API_BASE_URL",
        "https://api.siliconflow.cn/v1",
    ),
    ProviderConnectSpec(
        "groq", "Groq", "API_KEY", "API_BASE_URL",
        "https://api.groq.com/openai/v1",
    ),
    ProviderConnectSpec(
        "together", "Together AI", "API_KEY", "API_BASE_URL",
        "https://api.together.xyz/v1",
    ),
    ProviderConnectSpec(
        "mistral", "Mistral", "API_KEY", "API_BASE_URL",
        "https://api.mistral.ai/v1",
    ),
    ProviderConnectSpec(
        "cerebras", "Cerebras", "API_KEY", "API_BASE_URL",
        "https://api.cerebras.ai/v1",
    ),
    ProviderConnectSpec(
        "xai", "xAI", "API_KEY", "API_BASE_URL", "https://api.x.ai/v1",
    ),
    ProviderConnectSpec(
        "zhipu", "Zhipu", "API_KEY", "API_BASE_URL",
        "https://open.bigmodel.cn/api/paas/v4",
    ),
)


def provider_connect_specs() -> tuple[ProviderConnectSpec, ...]:
    return _SPECS


def provider_connect_spec(provider: str) -> ProviderConnectSpec:
    normalized = str(provider).strip().lower().replace("_", "-")
    aliases = {
        "codex": "openai-responses",
        "claude": "anthropic",
        "google": "gemini",
    }
    normalized = aliases.get(normalized, normalized)
    for spec in _SPECS:
        if spec.provider == normalized:
            return spec
    raise ValueError(f"Unsupported provider connection '{provider}'")


def save_provider_connection(
    provider: str,
    api_key: str,
    base_url: str,
    *,
    workspace: Path | None = None,
) -> ProviderConnectSpec:
    """Atomically save one provider credential and apply it to this process."""
    spec = provider_connect_spec(provider)
    key = str(api_key).strip()
    endpoint = str(base_url).strip().rstrip("/")
    if not key or any(character in key for character in "\r\n\0"):
        raise ValueError("API key must be non-empty and single-line")
    _validate_endpoint(endpoint)
    root = (workspace or current_workdir()).resolve()
    target = root / ".env"
    if target.is_symlink():
        raise ValueError("Workspace .env must not be a symbolic link")
    resolved_parent = target.parent.resolve(strict=True)
    resolved_parent.relative_to(root)
    target = resolved_parent / target.name
    lines = _read_env_lines(target)
    replacements = {
        spec.credential_name: key,
        spec.endpoint_name: endpoint,
    }
    updated: list[str] = []
    consumed: set[str] = set()
    for line in lines:
        name = _assignment_name(line)
        if name in replacements:
            if name not in consumed:
                updated.append(f"{name}={_encode_env_value(replacements[name])}")
                consumed.add(name)
            continue
        updated.append(line)
    for name, value in replacements.items():
        if name not in consumed:
            updated.append(f"{name}={_encode_env_value(value)}")
    _atomic_write(target, "\n".join(updated).rstrip("\n") + "\n")

    set_provider_connection_override(spec.provider, key, endpoint)
    return spec


def _validate_endpoint(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc:
        return
    loopback = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and parsed.netloc and loopback:
        return
    raise ValueError("Provider endpoint requires HTTPS or a loopback HTTP URL")


def _read_env_lines(path: Path) -> list[str]:
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError("Workspace .env is unexpectedly large")
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _assignment_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    name = stripped.split("=", 1)[0].strip()
    if name.startswith("export "):
        name = name[7:].strip()
    return name or None


def _encode_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._~:/?&=+,@%\-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _atomic_write(
    path: Path,
    content: str,
    *,
    os_name: str | None = None,
) -> None:
    selected_os = os.name if os_name is None else os_name
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp = Path(temp_name)
    try:
        temporary_security = harden_private_path(temp)
        if selected_os == "nt" and not temporary_security.hardened:
            raise PermissionError(
                "Could not apply owner-private Windows ACL to credential file"
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        temp.replace(path)
        path.chmod(0o600)
        harden_private_path(path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise
