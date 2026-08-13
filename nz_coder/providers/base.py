"""Provider contracts for model client creation and normalized completions."""
from __future__ import annotations

from typing import Any, Protocol

from nz_coder.providers.capabilities import ModelCapabilities


class ModelProvider(Protocol):
    """Minimal interface required by the agent runtime."""

    name: str

    def create_client(self) -> Any:
        """Create the provider SDK client."""

    def create_completion(self, client: Any, **kwargs: Any) -> Any:
        """Create a chat completion using an existing client."""

    def capabilities(self, model_id: str) -> ModelCapabilities:
        """Return immutable capability metadata for one model."""
