"""OpenAI-compatible implementation of the model provider contract."""
from __future__ import annotations

from typing import Any, Callable

from openai import OpenAI

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.protocol.attachments import openai_chat_messages
from nz_coder.providers.capabilities import (
    ModelCapabilities,
    configured_model_capabilities,
    prepare_openai_request,
)


class OpenAICompatibleProvider:
    """Adapter for APIs exposing the OpenAI chat completions interface."""

    name = "openai-compatible"
    uses_capability_snapshot = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        provider_name: str = "openai-compatible",
        client_factory: Callable[..., Any] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.name = str(provider_name or "openai-compatible").strip().lower()
        self._client_factory = client_factory or OpenAI
        self._uses_default_client_factory = client_factory is None

    def create_client(self) -> Any:
        """Create a configured OpenAI-compatible client."""
        kwargs = {"api_key": self.api_key, "base_url": self.base_url}
        if self._uses_default_client_factory:
            kwargs["timeout"] = current_run_settings().provider_hard_timeout
        return self._client_factory(**kwargs)

    def create_completion(self, client: Any, **kwargs: Any) -> Any:
        """Delegate after capability-aware request field normalization."""
        capabilities = kwargs.pop("_capabilities", None)
        if not isinstance(capabilities, ModelCapabilities):
            capabilities = self.capabilities(str(kwargs.get("model") or ""))
        request = prepare_openai_request(capabilities, kwargs)
        request["messages"] = openai_chat_messages(
            list(request.get("messages") or [])
        )
        return client.chat.completions.create(**request)

    def capabilities(self, model_id: str) -> ModelCapabilities:
        """Resolve capabilities for an OpenAI-compatible model id."""
        return configured_model_capabilities(self.name, model_id)
