"""Model provider selection and built-in adapters."""
from __future__ import annotations

from typing import Any, Callable

from nz_coder.foundation import config
from nz_coder.providers.anthropic import AnthropicProvider
from nz_coder.providers.base import ModelProvider
from nz_coder.providers.capabilities import (
    ModelCapabilities,
    capabilities_for_provider,
    configured_model_capabilities,
    load_model_catalog_file,
    prepare_openai_request,
    prompt_family_guidance,
    resolve_model_capabilities,
    variant_request_options,
)
from nz_coder.providers.gemini import GeminiProvider
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.providers.openai_responses import OpenAIResponsesProvider
from nz_coder.providers.models import ModelSelection, active_model_selection
from nz_coder.providers.configuration import provider_connection
from nz_coder.providers.extensions import (
    ProviderExtensionNotFound,
    create_extension_provider,
    installed_provider_extensions,
)
from nz_coder.providers.registry import (
    registry_capability_catalog,
    registry_runtime_model,
    registry_status,
    sync_model_registry,
)

_OPENAI_COMPATIBLE_NAMES = {
    "alibaba-cn",
    "cerebras",
    "dashscope",
    "deepseek",
    "groq",
    "kimi",
    "mistral",
    "moonshot",
    "openai",
    "openai-compatible",
    "openai_compatible",
    "openrouter",
    "siliconflow",
    "together",
    "xai",
    "zhipu",
}
_ANTHROPIC_NAMES = {"anthropic", "claude"}
_GEMINI_NAMES = {"gemini", "google"}
_OPENAI_RESPONSES_NAMES = {"codex", "openai-responses", "openai_responses"}


def create_provider(
    name: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> ModelProvider:
    """Build a configured model provider from explicit values or config."""
    selected = (name or config.MODEL_PROVIDER).strip().lower()
    connection = provider_connection(selected)
    if selected in _OPENAI_RESPONSES_NAMES:
        return OpenAIResponsesProvider(
            api_key=connection.api_key if api_key is None else api_key,
            base_url=connection.base_url if base_url is None else base_url,
            provider_name=(
                "openai-responses"
                if selected == "openai_responses"
                else selected
            ),
            client_factory=client_factory,
        )
    if selected in _OPENAI_COMPATIBLE_NAMES:
        return OpenAICompatibleProvider(
            api_key=connection.api_key if api_key is None else api_key,
            base_url=connection.base_url if base_url is None else base_url,
            provider_name=(
                "openai-compatible"
                if selected == "openai_compatible"
                else selected
            ),
            client_factory=client_factory,
        )
    if selected in _ANTHROPIC_NAMES:
        return AnthropicProvider(
            api_key=connection.api_key if api_key is None else api_key,
            base_url=connection.base_url if base_url is None else base_url,
            api_version=config.ANTHROPIC_API_VERSION,
        )
    if selected in _GEMINI_NAMES:
        return GeminiProvider(
            api_key=connection.api_key if api_key is None else api_key,
            base_url=connection.base_url if base_url is None else base_url,
        )
    try:
        return create_extension_provider(
            selected,
            api_key=connection.api_key if api_key is None else api_key,
            base_url=connection.base_url if base_url is None else base_url,
            client_factory=client_factory,
        )
    except ProviderExtensionNotFound:
        pass
    names = (
        _OPENAI_COMPATIBLE_NAMES
        | _ANTHROPIC_NAMES
        | _GEMINI_NAMES
        | _OPENAI_RESPONSES_NAMES
    )
    choices = ", ".join(sorted(names))
    extensions = sorted(item.provider for item in installed_provider_extensions())
    if extensions:
        choices = f"{choices}; installed adapters: {', '.join(extensions)}"
    raise ValueError(
        f"Unknown model provider '{selected}'. Expected one of: {choices}",
    )


__all__ = [
    "ModelProvider",
    "ModelCapabilities",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "OpenAIResponsesProvider",
    "create_provider",
    "capabilities_for_provider",
    "configured_model_capabilities",
    "load_model_catalog_file",
    "prepare_openai_request",
    "prompt_family_guidance",
    "resolve_model_capabilities",
    "variant_request_options",
    "ModelSelection",
    "active_model_selection",
    "registry_capability_catalog",
    "registry_runtime_model",
    "registry_status",
    "sync_model_registry",
    "installed_provider_extensions",
]
