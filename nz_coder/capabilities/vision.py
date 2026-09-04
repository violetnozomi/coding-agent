"""Image-description preflight for models without native vision support.

The orchestration mirrors InfCode's durable image-describe lifecycle while
keeping provider access behind NZ-Coder's existing adapter contract.
"""
from __future__ import annotations

import asyncio
import copy
import html
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from nz_coder.runtime.core.run_settings import RunSettings, current_run_settings
from nz_coder.protocol.attachments import normalize_attachments
from nz_coder.providers import create_provider
from nz_coder.runtime.model_gateway import (
    ModelCall,
    ModelCallPurpose,
    ModelCallStatus,
    ModelSelectionRequest,
    ProductionModelGateway,
    resolve_model_runtime,
)
from nz_coder.foundation.async_utils import to_thread_settled
from nz_coder.protocol.public_error import to_public_error


DescribeCallable = Callable[[dict, str], Awaitable[str]]
ProgressCallable = Callable[[dict], None]

_INTERRUPTED_ERROR = "Interrupted before image description finished"


class ProviderImageDescriber:
    """Describe one image through an independently configured vision model."""

    def __init__(
        self,
        provider_name: str,
        model_id: str,
        *,
        provider: Any = None,
        client: Any = None,
        api_key: str = "",
        base_url: str = "",
        max_tokens: int = 1200,
        timeout_seconds: float = 600.0,
        observer: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.provider_name = str(provider_name or "").strip().lower()
        self.model_id = str(model_id or "").strip()
        self.provider = provider
        self.client = client
        self.api_key = str(api_key or "")
        self.base_url = str(base_url or "")
        self.max_tokens = max(64, int(max_tokens))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.observer = observer
        self._model_runtime = None

    @classmethod
    def configured(
        cls,
        *,
        observer: Callable[[str, dict], None] | None = None,
        run_settings: RunSettings | None = None,
    ) -> "ProviderImageDescriber":
        """Build the lazy descriptor selected by environment configuration."""
        settings = run_settings or current_run_settings()
        return cls(
            settings.image_provider,
            settings.image_model,
            api_key=settings.image_api_key,
            base_url=settings.image_base_url,
            max_tokens=settings.image_max_tokens,
            timeout_seconds=settings.provider_hard_timeout,
            observer=observer,
        )

    async def __call__(self, attachment: dict, prompt: str) -> str:
        """Return a plain-text description without blocking the event loop."""
        if not self.model_id:
            raise RuntimeError(
                "No image description model configured; set "
                "NZ_IMAGE_DESCRIBE_MODEL to a vision-capable model"
            )
        cancel_event = threading.Event()
        return await to_thread_settled(
            self._describe_sync,
            attachment,
            prompt,
            cancel_event,
            cancel_callback=cancel_event.set,
        )

    def _describe_sync(
        self,
        attachment: dict,
        prompt: str,
        cancel_event: threading.Event | None = None,
    ) -> str:
        if self.provider is None:
            self.provider = create_provider(
                self.provider_name,
                api_key=self.api_key or None,
                base_url=self.base_url or None,
            )
        if self._model_runtime is None:
            self._model_runtime = resolve_model_runtime(
                ModelSelectionRequest(
                    provider_name=self.provider_name,
                    model_id=self.model_id,
                    provider=self.provider,
                    client=self.client,
                )
            )
            self.client = self._model_runtime.client
        capabilities = self._model_runtime.capabilities
        if not capabilities.supports_image_input:
            raise RuntimeError(
                f"Configured image description model {self.provider_name}/"
                f"{self.model_id} is not marked as vision-capable"
            )
        normalized = normalize_attachments([attachment])[0]
        outcome = ProductionModelGateway(
            self._model_runtime,
            observer=self.observer,
        ).complete_sync(
            ModelCall(
                purpose=ModelCallPurpose.VISION,
                messages=[{
                    "role": "user",
                    "content": _description_prompt(normalized, prompt),
                    "_nz_user_attachments": [normalized],
                }],
                max_output_tokens=self.max_tokens,
                timeout_seconds=self.timeout_seconds,
                capability_options={"stream": False},
            ),
            cancel_event=cancel_event,
        )
        if outcome.status is not ModelCallStatus.COMPLETED:
            raise RuntimeError(outcome.error or outcome.status.value)
        text = outcome.content.strip()
        if not text:
            raise RuntimeError("Image description model returned empty text")
        return text

    def close(self) -> None:
        """Dispose an internally created vision client."""
        if self._model_runtime is not None:
            self._model_runtime.close()


async def describe_images(
    attachments: list[dict],
    *,
    source_ids: list[str],
    describe: DescribeCallable,
    prompt: str = "",
    on_progress: ProgressCallable | None = None,
) -> dict:
    """Describe images sequentially with durable per-item terminal state."""
    files = normalize_attachments(attachments)
    if len(files) != len(source_ids):
        raise ValueError("source_ids must match image attachments")
    items = [
        {
            "source_id": source_ids[index],
            "filename": file.get("filename") or f"image-{index + 1}",
            "mime": file["mime"],
            "status": "running",
        }
        for index, file in enumerate(files)
    ]
    state = {"status": "running", "items": items}
    _publish(on_progress, state)
    try:
        for index, file in enumerate(files):
            try:
                text = await describe(file, prompt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                items[index] = {
                    **items[index],
                    "status": "error",
                    "error": to_public_error(exc).message,
                }
            else:
                items[index] = {
                    **items[index],
                    "status": "completed",
                    "text": str(text)[:100_000],
                }
            _publish(on_progress, state)
    except asyncio.CancelledError:
        for index, item in enumerate(items):
            if item.get("status") == "running":
                items[index] = {
                    **item,
                    "status": "error",
                    "error": _INTERRUPTED_ERROR,
                }
        state["status"] = "interrupted"
        _publish(on_progress, state)
        raise
    state["status"] = "completed"
    _publish(on_progress, state)
    return copy.deepcopy(state)


def render_image_descriptions(items: list[dict]) -> str:
    """Render terminal item state as the source-user hint consumed by models."""
    blocks = []
    for item in items:
        filename = html.escape(str(item.get("filename") or "image"), quote=True)
        if item.get("status") == "completed":
            body = str(item.get("text") or "(No content could be extracted)")
        elif item.get("status") == "error":
            body = f"Image describe failed: {item.get('error') or 'unknown error'}"
        else:
            continue
        blocks.append(
            f'<image_describe filename="{filename}">\n'
            f"{body}\n"
            "</image_describe>"
        )
    return "\n\n".join(blocks)


def _default_prompt(attachment: dict) -> str:
    filename = attachment.get("filename") or "attached image"
    return (
        f"Describe {filename} precisely for a software-engineering agent. "
        "Transcribe visible text, errors, code, UI labels, diagrams, and spatial "
        "relationships. Do not guess details that are not visible."
    )


def _description_prompt(attachment: dict, query: str) -> str:
    base = _default_prompt(attachment)
    selected = str(query or "").strip()
    return f"{base}\n\nUser request: {selected}" if selected else base


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return ""
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message")
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    return "" if content is None else str(content)


def _publish(callback: ProgressCallable | None, state: dict) -> None:
    if callback is not None:
        callback(copy.deepcopy(state))
