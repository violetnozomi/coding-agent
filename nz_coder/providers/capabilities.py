"""Model capability registry and provider request normalization."""
from __future__ import annotations

import json
import math
import os
import stat
import threading
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from nz_coder.foundation import config
from nz_coder.runtime.process.workdir import current_workdir

_MAX_CATALOG_BYTES = 2_000_000
_CATALOG_CACHE: dict[Path, tuple[int, int, dict[str, Any]]] = {}
_CATALOG_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ModelCapabilities:
    """Immutable behavior and budget metadata for one configured model."""

    provider: str
    model_id: str
    family: str = "generic"
    prompt_family: str = "default"
    context_tokens: int = 100_000
    output_tokens: int = 8_000
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_reasoning: bool = False
    supports_image_input: bool = False
    supports_temperature: bool = True
    preserve_reasoning_content: bool = False
    max_tokens_parameter: str = "max_tokens"
    default_temperature: float | None = None
    available_variants: tuple[str, ...] = ()
    selected_variant: str | None = None
    variant_options_json: str = "{}"
    source: str = "fallback"


def _validated_capability_snapshot(
    capability: ModelCapabilities,
) -> ModelCapabilities:
    """Reject malformed adapter metadata before it reaches request policy."""
    for name, minimum in (("context_tokens", 0), ("output_tokens", 1)):
        value = getattr(capability, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            raise ValueError(
                f"Model capability '{name}' must be an integer >= {minimum}"
            )
    for name in (
        "supports_tools",
        "supports_streaming",
        "supports_reasoning",
        "supports_image_input",
        "supports_temperature",
        "preserve_reasoning_content",
    ):
        if not isinstance(getattr(capability, name), bool):
            raise ValueError(f"Model capability '{name}' must be boolean")
    temperature = capability.default_temperature
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
    ):
        raise ValueError(
            "Model capability 'default_temperature' must be finite or null"
        )
    if capability.max_tokens_parameter not in {
        "max_tokens",
        "max_completion_tokens",
    }:
        raise ValueError(
            "Model capability 'max_tokens_parameter' is unsupported"
        )
    return capability


@dataclass(frozen=True)
class _CapabilityRule:
    """Ordered registry rule matched against a normalized model id."""

    name: str
    any_terms: tuple[str, ...]
    family: str
    prompt_family: str
    context_tokens: int
    output_tokens: int
    required_terms: tuple[str, ...] = ()
    excluded_terms: tuple[str, ...] = ()
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_reasoning: bool = False
    supports_image_input: bool = False
    supports_temperature: bool = True
    preserve_reasoning_content: bool = False
    max_tokens_parameter: str = "max_tokens"
    default_temperature: float | None = None

    def matches(self, model_id: str) -> bool:
        return (
            all(term in model_id for term in self.required_terms)
            and any(term in model_id for term in self.any_terms)
            and not any(term in model_id for term in self.excluded_terms)
        )

    def build(self, provider: str, model_id: str) -> ModelCapabilities:
        return ModelCapabilities(
            provider=provider,
            model_id=model_id,
            family=self.family,
            prompt_family=self.prompt_family,
            context_tokens=self.context_tokens,
            output_tokens=self.output_tokens,
            supports_tools=self.supports_tools,
            supports_streaming=self.supports_streaming,
            supports_reasoning=self.supports_reasoning,
            supports_image_input=self.supports_image_input,
            supports_temperature=self.supports_temperature,
            preserve_reasoning_content=self.preserve_reasoning_content,
            max_tokens_parameter=self.max_tokens_parameter,
            default_temperature=self.default_temperature,
            source=f"builtin:{self.name}",
        )


# Specific rules precede broad families. Values are conservative local defaults;
# MAX_CONTEXT_TOKENS/MAX_OUTPUT_TOKENS and MODEL_CAPABILITIES_JSON can override them.
_RULES = (
    _CapabilityRule(
        name="gpt-5-chat",
        required_terms=("gpt-5",),
        any_terms=("-chat", "chat-latest"),
        family="gpt",
        prompt_family="gpt",
        context_tokens=128_000,
        output_tokens=16_384,
        supports_image_input=True,
    ),
    _CapabilityRule(
        name="codex",
        any_terms=("codex",),
        family="gpt-codex",
        prompt_family="codex",
        context_tokens=400_000,
        output_tokens=128_000,
        supports_reasoning=True,
        supports_image_input=True,
        supports_temperature=False,
        max_tokens_parameter="max_completion_tokens",
    ),
    _CapabilityRule(
        name="gpt-5",
        any_terms=("gpt-5",),
        family="gpt",
        prompt_family="gpt",
        context_tokens=400_000,
        output_tokens=128_000,
        supports_reasoning=True,
        supports_image_input=True,
        supports_temperature=False,
        max_tokens_parameter="max_completion_tokens",
    ),
    _CapabilityRule(
        name="openai-reasoning",
        any_terms=("o1", "o3", "o4"),
        family="gpt-reasoning",
        prompt_family="gpt",
        context_tokens=200_000,
        output_tokens=100_000,
        supports_reasoning=True,
        supports_temperature=False,
        max_tokens_parameter="max_completion_tokens",
    ),
    _CapabilityRule(
        name="openai-vision",
        any_terms=("gpt-4o", "gpt-4.1", "gpt-4-turbo"),
        family="gpt",
        prompt_family="gpt",
        context_tokens=128_000,
        output_tokens=16_384,
        supports_image_input=True,
    ),
    _CapabilityRule(
        name="gpt",
        any_terms=("gpt-",),
        family="gpt",
        prompt_family="gpt",
        context_tokens=128_000,
        output_tokens=16_384,
    ),
    _CapabilityRule(
        name="anthropic",
        any_terms=("claude",),
        family="claude",
        prompt_family="anthropic",
        context_tokens=200_000,
        output_tokens=64_000,
        supports_image_input=True,
    ),
    _CapabilityRule(
        name="gemini-image",
        required_terms=("gemini",),
        any_terms=("image",),
        family="gemini-image",
        prompt_family="gemini",
        context_tokens=32_768,
        output_tokens=32_768,
        supports_tools=False,
        supports_image_input=True,
    ),
    _CapabilityRule(
        name="gemini",
        any_terms=("gemini",),
        family="gemini",
        prompt_family="gemini",
        context_tokens=1_000_000,
        output_tokens=65_536,
        default_temperature=1.0,
        supports_image_input=True,
    ),
    _CapabilityRule(
        name="qwen-thinking",
        any_terms=("qwq", "qwen3-thinking", "qwen-thinking"),
        family="qwen",
        prompt_family="qwen",
        context_tokens=262_144,
        output_tokens=65_536,
        supports_reasoning=True,
        preserve_reasoning_content=True,
        default_temperature=0.55,
    ),
    _CapabilityRule(
        name="qwen-vision",
        any_terms=("qwen-vl", "qwen2-vl", "qwen2.5-vl"),
        family="qwen",
        prompt_family="qwen",
        context_tokens=131_072,
        output_tokens=32_768,
        supports_image_input=True,
        default_temperature=0.55,
    ),
    _CapabilityRule(
        name="qwen-plus",
        any_terms=("qwen-plus", "qwen-flash"),
        family="qwen",
        prompt_family="qwen",
        context_tokens=1_000_000,
        output_tokens=32_768,
        supports_reasoning=True,
        preserve_reasoning_content=True,
        default_temperature=0.55,
    ),
    _CapabilityRule(
        name="qwen",
        any_terms=("qwen",),
        family="qwen",
        prompt_family="qwen",
        context_tokens=131_072,
        output_tokens=32_768,
        preserve_reasoning_content=True,
        default_temperature=0.55,
    ),
    _CapabilityRule(
        name="deepseek-v4",
        any_terms=("deepseek-v4-flash", "deepseek-v4-pro"),
        family="deepseek",
        prompt_family="default",
        context_tokens=1_000_000,
        output_tokens=64_000,
        supports_reasoning=True,
        preserve_reasoning_content=True,
    ),
    _CapabilityRule(
        name="deepseek-reasoning",
        required_terms=("deepseek",),
        any_terms=("reasoner", "thinking", "-r1", "r1-"),
        family="deepseek",
        prompt_family="default",
        context_tokens=128_000,
        output_tokens=64_000,
        supports_reasoning=True,
        preserve_reasoning_content=True,
    ),
    _CapabilityRule(
        name="deepseek",
        any_terms=("deepseek",),
        family="deepseek",
        prompt_family="default",
        context_tokens=128_000,
        output_tokens=8_192,
        preserve_reasoning_content=True,
    ),
    _CapabilityRule(
        name="glm-reasoning",
        any_terms=("glm-4.6", "glm-4.7", "glm-5"),
        family="glm",
        prompt_family="default",
        context_tokens=200_000,
        output_tokens=98_304,
        supports_reasoning=True,
        preserve_reasoning_content=True,
        default_temperature=1.0,
    ),
    _CapabilityRule(
        name="kimi-thinking",
        required_terms=("kimi",),
        any_terms=("thinking", "k2.5", "k2p", "k2-5"),
        family="kimi",
        prompt_family="kimi",
        context_tokens=262_144,
        output_tokens=128_000,
        supports_reasoning=True,
        preserve_reasoning_content=True,
        default_temperature=1.0,
    ),
    _CapabilityRule(
        name="kimi",
        any_terms=("kimi",),
        family="kimi",
        prompt_family="kimi",
        context_tokens=262_144,
        output_tokens=64_000,
        default_temperature=0.6,
    ),
)


def resolve_model_capabilities(
    provider: str,
    model_id: str,
    *,
    context_tokens: int | None = None,
    output_tokens: int | None = None,
    overrides: str | dict[str, Any] | None = None,
    catalog: str | dict[str, Any] | None = None,
    registry: str | dict[str, Any] | None = None,
    variant: str | None = None,
) -> ModelCapabilities:
    """Resolve one model, apply an exact local record, then select a variant."""
    normalized_provider = str(provider or "openai-compatible").strip().lower()
    normalized_model = str(model_id or "").strip()
    model_key = normalized_model.lower()
    from nz_coder.runtime.core.run_settings import current_run_settings

    run_settings = current_run_settings()
    capability = next(
        (rule.build(normalized_provider, normalized_model) for rule in _RULES if rule.matches(model_key)),
        ModelCapabilities(
            provider=normalized_provider,
            model_id=normalized_model,
            context_tokens=run_settings.max_context_tokens,
            output_tokens=run_settings.max_output_tokens,
        ),
    )

    if normalized_provider in {"anthropic", "claude"}:
        capability = replace(capability, provider="anthropic", prompt_family="anthropic")
    elif normalized_provider in {"gemini", "google"}:
        capability = replace(capability, provider="gemini", prompt_family="gemini")

    capability, variants = _apply_exact_catalog_record(
        capability,
        registry,
        source_label="registry",
    )
    capability, catalog_variants = _apply_exact_catalog_record(
        capability,
        catalog,
    )
    if catalog_variants is not None:
        variants = catalog_variants

    if context_tokens is not None:
        capability = replace(capability, context_tokens=max(0, int(context_tokens)))
    if output_tokens is not None:
        capability = replace(capability, output_tokens=max(1, int(output_tokens)))
    if overrides:
        capability = _apply_overrides(capability, overrides)
    if variants is None:
        variants = _builtin_variants(capability)
    return _select_variant(capability, variants, variant)


def configured_model_capabilities(
    provider: str | None = None,
    model_id: str | None = None,
    *,
    variant: str | None = None,
) -> ModelCapabilities:
    """Resolve capabilities from the active immutable configuration epoch."""
    from nz_coder.foundation.workspace_trust import active_config_snapshot, ConfigSource
    from nz_coder.runtime.core.run_settings import current_run_settings

    snapshot = active_config_snapshot()
    settings = current_run_settings()
    if snapshot is not None:
        context_record = snapshot.value("MAX_CONTEXT_TOKENS")
        output_record = snapshot.value("MAX_OUTPUT_TOKENS")
        context_override = (
            settings.max_context_tokens
            if context_record.source is not ConfigSource.DEFAULT else None
        )
        output_override = (
            settings.max_output_tokens
            if output_record.source is not ConfigSource.DEFAULT else None
        )
        catalog: str | dict[str, Any] | None = snapshot.get("MODEL_CATALOG_JSON", "")
        catalog_path = snapshot.get("MODEL_CATALOG_PATH", "")
        configured_model_id = snapshot.get("MODEL_ID", "deepseek-v4-flash")
        resolved_model_id = model_id or configured_model_id
        resolved_provider = provider or snapshot.get("MODEL_PROVIDER", "openai-compatible")
        configured_variant = snapshot.get("MODEL_VARIANT", "")
        overrides = snapshot.get("MODEL_CAPABILITIES_JSON", "")
    else:
        context_override = (
            config.MAX_CONTEXT_TOKENS if os.environ.get("MAX_CONTEXT_TOKENS") else None
        )
        output_override = (
            config.MAX_OUTPUT_TOKENS if os.environ.get("MAX_OUTPUT_TOKENS") else None
        )
        catalog = getattr(config, "MODEL_CATALOG_JSON", "")
        catalog_path = getattr(config, "MODEL_CATALOG_PATH", "")
        configured_model_id = config.MODEL_ID
        resolved_model_id = model_id or configured_model_id
        resolved_provider = provider or config.MODEL_PROVIDER
        configured_variant = getattr(config, "MODEL_VARIANT", "")
        overrides = getattr(config, "MODEL_CAPABILITIES_JSON", "")
    if not catalog and catalog_path:
        catalog = load_model_catalog_file(catalog_path)
    from nz_coder.providers.registry import registry_capability_catalog

    registry = registry_capability_catalog()
    selected_variant = variant
    if selected_variant is None and resolved_model_id == configured_model_id:
        selected_variant = configured_variant or None
    return resolve_model_capabilities(
        resolved_provider,
        resolved_model_id,
        context_tokens=context_override,
        output_tokens=output_override,
        overrides=overrides,
        catalog=catalog,
        registry=registry,
        variant=selected_variant,
    )


def load_model_catalog_file(
    path: str | Path,
    *,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Load one bounded JSON catalog without escaping the active workspace."""
    root = (workspace or current_workdir()).resolve()
    target = (root / Path(path)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Model catalog path escapes workspace: {path}") from exc
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(target, flags)
    except OSError as exc:
        raise ValueError(f"Cannot read model catalog '{path}': {exc}") from exc
    try:
        opened_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValueError(f"Model catalog '{path}' must be a regular file")
        size = opened_stat.st_size
        if size > _MAX_CATALOG_BYTES:
            raise ValueError(
                f"Model catalog '{path}' exceeds {_MAX_CATALOG_BYTES} bytes"
            )
        fingerprint = (opened_stat.st_mtime_ns, size)
        with _CATALOG_CACHE_LOCK:
            cached = _CATALOG_CACHE.get(target)
            if cached is not None and cached[:2] == fingerprint:
                return deepcopy(cached[2])
        with os.fdopen(file_descriptor, "rb", closefd=False) as catalog_file:
            payload = catalog_file.read(_MAX_CATALOG_BYTES + 1)
        if len(payload) > _MAX_CATALOG_BYTES:
            raise ValueError(
                f"Model catalog '{path}' exceeds {_MAX_CATALOG_BYTES} bytes"
            )
        data = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid model catalog '{path}': {exc}") from exc
    finally:
        os.close(file_descriptor)
    if not isinstance(data, dict):
        raise ValueError(f"Model catalog '{path}' must contain a JSON object")
    with _CATALOG_CACHE_LOCK:
        _CATALOG_CACHE[target] = (fingerprint[0], fingerprint[1], data)
    return deepcopy(data)


def capabilities_for_provider(provider: Any, model_id: str) -> ModelCapabilities:
    """Read adapter-owned capabilities, with compatibility for injected fakes."""
    resolver = getattr(provider, "capabilities", None)
    if callable(resolver):
        value = resolver(model_id)
        if isinstance(value, ModelCapabilities):
            return _validated_capability_snapshot(value)
    value = getattr(provider, "model_capabilities", None)
    if isinstance(value, ModelCapabilities):
        return _validated_capability_snapshot(value)
    return _validated_capability_snapshot(
        configured_model_capabilities(getattr(provider, "name", None), model_id)
    )


def prepare_openai_request(
    capabilities: ModelCapabilities,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Normalize OpenAI-compatible request fields from declared capabilities."""
    request = dict(kwargs)
    messages = request.get("messages")
    if capabilities.preserve_reasoning_content and isinstance(messages, list):
        request["messages"] = [
            {
                **message,
                "reasoning_content": str(
                    message.get("reasoning_content") or ""
                ),
            }
            if isinstance(message, dict) and message.get("role") == "assistant"
            else message
            for message in messages
        ]
    if not capabilities.supports_tools:
        request.pop("tools", None)
        request.pop("tool_choice", None)
    if not capabilities.supports_streaming:
        request.pop("stream", None)
    if not capabilities.supports_temperature:
        request.pop("temperature", None)
    elif capabilities.default_temperature is not None:
        request.setdefault("temperature", capabilities.default_temperature)

    target = capabilities.max_tokens_parameter
    if target != "max_tokens" and "max_tokens" in request and target not in request:
        request[target] = request.pop("max_tokens")
    options = variant_request_options(capabilities)
    extra_body = options.pop("extra_body", None)
    if isinstance(extra_body, dict):
        merged = dict(request.get("extra_body") or {})
        merged.update(extra_body)
        request["extra_body"] = merged
    for key in ("reasoning_effort", "top_p"):
        if key in options:
            request[key] = options[key]
    return request


def variant_request_options(capabilities: ModelCapabilities) -> dict[str, Any]:
    """Decode the immutable request options for the selected model variant."""
    try:
        value = json.loads(capabilities.variant_options_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def prompt_family_guidance(capabilities: ModelCapabilities) -> str:
    """Return the translated InfCode production contract for one model family."""
    guidance = {
        "anthropic": (
            "- Be concise and objective; investigate uncertainty instead of agreeing reflexively.\n"
            "- Prefer editing existing files. Never create a file unless it is necessary.\n"
            "- Use todo state frequently for non-trivial work and complete items immediately.\n"
            "- Preserve native structured tool-call/result ordering and never expose hidden reasoning."
        ),
        "gemini": (
            "- Inspect surrounding code, tests, manifests, and conventions before changing code.\n"
            "- Never assume a library is available; verify established usage first.\n"
            "- Follow understand, plan, implement, targeted tests, then project lint/typecheck.\n"
            "- Use strict JSON function arguments and preserve provider tool metadata across turns."
        ),
        "codex": (
            "- Work autonomously through tools and infer safe defaults after reading the repository.\n"
            "- Prefer small verified patches; use apply_patch for focused edits, not generated files.\n"
            "- Preserve dirty-worktree changes and never run destructive Git commands without approval.\n"
            "- Run independent read-only tool calls in parallel and dependent or writing calls sequentially.\n"
            "- Ask exactly one targeted question only when materially blocked; never ask whether to proceed."
        ),
        "gpt": (
            "- Drive the task through tools, keep strict JSON arguments, and do not expose hidden reasoning.\n"
            "- Inspect before editing, preserve existing conventions, and verify the smallest relevant scope.\n"
            "- Continue until the requested outcome is complete or a concrete external blocker remains."
        ),
        "kimi": (
            "- Treat ambiguous action-oriented requests as tasks and make real changes through tools.\n"
            "- Use the same language as the user unless explicitly asked otherwise.\n"
            "- Parallelize independent tool calls; use each result to decide whether to continue, finish, or ask.\n"
            "- For coding work, understand, implement, run tests, and iterate on failures."
        ),
        "qwen": (
            "- Keep tool arguments strict JSON and continue from tool results without repeating the full plan.\n"
            "- Inspect repository evidence before editing and finish with targeted verification."
        ),
        "default": (
            "- Be concise, direct, and action-oriented; minimize narration outside tool use.\n"
            "- Inspect relevant files before editing and prefer the smallest change that solves the task.\n"
            "- Use tools for software-engineering work, verify changes, and report only the relevant outcome.\n"
            "- Do not invent URLs, repository facts, command output, or verification evidence."
        ),
    }.get(capabilities.prompt_family, "")
    if not guidance:
        return ""
    return (
        "## Model-family guidance (InfCode production contract)\n"
        f"- Family: {capabilities.prompt_family}\n"
        f"- {guidance}"
    )


def _apply_overrides(
    capability: ModelCapabilities,
    overrides: str | dict[str, Any],
) -> ModelCapabilities:
    if isinstance(overrides, str):
        try:
            data = json.loads(overrides)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid MODEL_CAPABILITIES_JSON: {exc}") from exc
    else:
        data = dict(overrides)
    if not isinstance(data, dict):
        raise ValueError("MODEL_CAPABILITIES_JSON must decode to an object")

    allowed = {field.name for field in fields(ModelCapabilities)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"Unknown model capability override(s): {', '.join(unknown)}")
    protected = {
        "provider",
        "model_id",
        "source",
        "available_variants",
        "selected_variant",
        "variant_options_json",
    }
    if protected & set(data):
        names = ", ".join(sorted(protected & set(data)))
        raise ValueError(f"Model capability identity fields cannot be overridden: {names}")

    changes = dict(data)
    for name in ("family", "prompt_family"):
        if name in changes and not isinstance(changes[name], str):
            raise ValueError(f"Model capability '{name}' must be a string")
    for name in ("context_tokens", "output_tokens"):
        if name in changes:
            minimum = 0 if name == "context_tokens" else 1
            changes[name] = max(minimum, int(changes[name]))
    for name in (
        "supports_tools",
        "supports_streaming",
        "supports_reasoning",
        "supports_image_input",
        "supports_temperature",
        "preserve_reasoning_content",
    ):
        if name in changes and not isinstance(changes[name], bool):
            raise ValueError(f"Model capability '{name}' must be boolean")
    if "default_temperature" in changes and changes["default_temperature"] is not None:
        changes["default_temperature"] = float(changes["default_temperature"])
    if changes.get("max_tokens_parameter", "max_tokens") not in {
        "max_tokens",
        "max_completion_tokens",
    }:
        raise ValueError("max_tokens_parameter must be max_tokens or max_completion_tokens")
    changes["source"] = f"{capability.source}+override"
    return replace(capability, **changes)


def _apply_exact_catalog_record(
    capability: ModelCapabilities,
    catalog: str | dict[str, Any] | None,
    *,
    source_label: str = "catalog",
) -> tuple[ModelCapabilities, dict[str, dict[str, Any]] | None]:
    """Overlay one provider/model record from an explicit local catalog."""
    if not catalog:
        return capability, None
    if isinstance(catalog, str):
        try:
            data = json.loads(catalog)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid MODEL_CATALOG_JSON: {exc}") from exc
    else:
        data = dict(catalog)
    if not isinstance(data, dict):
        raise ValueError("MODEL_CATALOG_JSON must decode to an object")
    models = data.get("models", data)
    if not isinstance(models, dict):
        raise ValueError("MODEL_CATALOG_JSON 'models' must be an object")
    provider = capability.provider
    key = f"{provider}/{capability.model_id}"
    record = models.get(key)
    if record is None:
        return capability, None
    if not isinstance(record, dict):
        raise ValueError(f"Model catalog record '{key}' must be an object")

    has_variants = "variants" in record
    variants = record.get("variants") if has_variants else {}
    if not has_variants:
        normalized_variants = None
    else:
        normalized_variants = {}
    if not isinstance(variants, dict):
        raise ValueError(f"Model catalog record '{key}' variants must be an object")
    if normalized_variants is not None:
        if any(not isinstance(name, str) or not name for name in variants):
            raise ValueError(
                f"Model catalog record '{key}' variant names must be non-empty strings"
            )
        normalized_variants = {
            name: _validate_variant_options(
                key,
                provider,
                name,
                options,
            )
            for name, options in variants.items()
        }
    changes = {name: value for name, value in record.items() if name != "variants"}
    updated = _apply_overrides(capability, changes) if changes else capability
    return replace(updated, source=f"{source_label}:{key}"), normalized_variants


def _validate_variant_options(
    model_key: str,
    provider: str,
    variant: str,
    options: Any,
) -> dict[str, Any]:
    if not variant:
        raise ValueError(f"Model catalog '{model_key}' has an empty variant name")
    if not isinstance(options, dict):
        raise ValueError(
            f"Model catalog '{model_key}' variant '{variant}' must be an object"
        )
    if any(not isinstance(name, str) for name in options):
        raise ValueError(f"Model variant '{variant}' option names must be strings")
    openai_options = {"reasoning_effort", "top_p", "extra_body"}
    allowed_by_provider = {
        "openai-compatible": openai_options,
        "anthropic": {"thinking", "effort"},
        "gemini": {"thinking_config"},
    }
    allowed = allowed_by_provider.get(provider, openai_options)
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise ValueError(
            f"Unknown option(s) for model variant '{variant}': {', '.join(unknown)}"
        )
    if "reasoning_effort" in options and not isinstance(
        options["reasoning_effort"],
        str,
    ):
        raise ValueError(f"Model variant '{variant}' reasoning_effort must be a string")
    if "top_p" in options:
        top_p = options["top_p"]
        if (
            isinstance(top_p, bool)
            or not isinstance(top_p, (int, float))
            or not math.isfinite(float(top_p))
            or not 0 <= float(top_p) <= 1
        ):
            raise ValueError(f"Model variant '{variant}' top_p must be between 0 and 1")
    if "extra_body" in options:
        extra_body = options["extra_body"]
        if not isinstance(extra_body, dict):
            raise ValueError(f"Model variant '{variant}' extra_body must be an object")
        if any(not isinstance(name, str) for name in extra_body):
            raise ValueError(
                f"Model variant '{variant}' extra_body option names must be strings"
            )
        unknown_extra = sorted(set(extra_body) - {"enable_thinking"})
        if unknown_extra:
            raise ValueError(
                f"Unknown extra_body option(s) for model variant '{variant}': "
                f"{', '.join(unknown_extra)}"
            )
        if "enable_thinking" in extra_body and not isinstance(
            extra_body["enable_thinking"],
            bool,
        ):
            raise ValueError(
                f"Model variant '{variant}' enable_thinking must be boolean"
            )
    if "thinking" in options:
        _validate_anthropic_thinking(variant, options["thinking"])
    if "effort" in options:
        effort = options["effort"]
        if not isinstance(effort, str) or effort not in {
            "low",
            "medium",
            "high",
            "max",
        }:
            raise ValueError(f"Model variant '{variant}' effort is invalid")
    if "thinking_config" in options:
        _validate_gemini_thinking(variant, options["thinking_config"])
    try:
        json.dumps(options, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Model catalog '{model_key}' variant '{variant}' must be JSON-safe"
        ) from exc
    return dict(options)


def _validate_anthropic_thinking(variant: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"Model variant '{variant}' thinking must be an object")
    if any(not isinstance(name, str) for name in value):
        raise ValueError(f"Model variant '{variant}' thinking option names must be strings")
    unknown = sorted(set(value) - {"type", "budget_tokens"})
    if unknown:
        raise ValueError(
            f"Unknown thinking option(s) for model variant '{variant}': {', '.join(unknown)}"
        )
    thinking_type = value.get("type")
    if not isinstance(thinking_type, str) or thinking_type not in {
        "adaptive",
        "enabled",
        "disabled",
    }:
        raise ValueError(f"Model variant '{variant}' thinking type is invalid")
    if "budget_tokens" in value and (
        isinstance(value["budget_tokens"], bool)
        or not isinstance(value["budget_tokens"], int)
        or value["budget_tokens"] < 1
    ):
        raise ValueError(f"Model variant '{variant}' thinking budget_tokens must be positive")


def _validate_gemini_thinking(variant: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"Model variant '{variant}' thinking_config must be an object")
    if any(not isinstance(name, str) for name in value):
        raise ValueError(
            f"Model variant '{variant}' thinking_config option names must be strings"
        )
    allowed = {"includeThoughts", "thinkingBudget", "thinkingLevel"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"Unknown thinking_config option(s) for model variant '{variant}': {', '.join(unknown)}"
        )
    if "includeThoughts" in value and not isinstance(value["includeThoughts"], bool):
        raise ValueError(f"Model variant '{variant}' includeThoughts must be boolean")
    if "thinkingBudget" in value and (
        isinstance(value["thinkingBudget"], bool)
        or not isinstance(value["thinkingBudget"], int)
        or value["thinkingBudget"] < 0
    ):
        raise ValueError(f"Model variant '{variant}' thinkingBudget must be non-negative")
    if "thinkingLevel" in value:
        thinking_level = value["thinkingLevel"]
        if not isinstance(thinking_level, str) or thinking_level not in {
            "minimal",
            "low",
            "medium",
            "high",
        }:
            raise ValueError(f"Model variant '{variant}' thinkingLevel is invalid")


def _builtin_variants(capability: ModelCapabilities) -> dict[str, dict[str, Any]]:
    model = capability.model_id.lower()
    if capability.provider == "anthropic" and capability.supports_reasoning:
        return {
            effort: {"thinking": {"type": "adaptive"}, "effort": effort}
            for effort in ("low", "medium", "high")
        }
    if capability.provider == "gemini" and capability.supports_reasoning:
        if "2.5" in model:
            return {
                "high": {"thinking_config": {"includeThoughts": True, "thinkingBudget": 16_000}},
                "max": {"thinking_config": {"includeThoughts": True, "thinkingBudget": 24_576}},
            }
        return {
            effort: {"thinking_config": {"includeThoughts": True, "thinkingLevel": effort}}
            for effort in ("low", "high")
        }
    if capability.family == "qwen" and capability.supports_reasoning:
        return {
            "instant": {"extra_body": {"enable_thinking": False}},
            "thinking": {"extra_body": {"enable_thinking": True}},
        }
    if capability.supports_reasoning and capability.family in {
        "gpt",
        "gpt-codex",
        "gpt-reasoning",
    }:
        return {
            effort: {"reasoning_effort": effort}
            for effort in ("low", "medium", "high")
        }
    return {}


def _select_variant(
    capability: ModelCapabilities,
    variants: dict[str, dict[str, Any]],
    selected: str | None,
) -> ModelCapabilities:
    names = tuple(sorted(variants))
    if not selected:
        return replace(capability, available_variants=names)
    name = str(selected).strip()
    if not capability.supports_reasoning:
        raise ValueError(
            f"Model {capability.provider}/{capability.model_id} does not support reasoning variants"
        )
    if name not in variants:
        choices = ", ".join(names) or "none"
        raise ValueError(
            f"Unknown model variant '{name}' for {capability.provider}/{capability.model_id}; available: {choices}"
        )
    options_json = json.dumps(
        variants[name],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return replace(
        capability,
        available_variants=names,
        selected_variant=name,
        variant_options_json=options_json,
    )
