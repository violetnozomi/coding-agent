"""Native Anthropic Messages API provider with OpenAI-shape normalization."""
from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from nz_coder import config
from nz_coder.attachments import attachment_base64, normalize_attachments
from nz_coder.providers.http import NativeClient, UrllibTransport
from nz_coder.providers.capabilities import (
    ModelCapabilities,
    configured_model_capabilities,
    variant_request_options,
)
from nz_coder.providers.normalized import (
    NormalizedFunction,
    NormalizedToolCall,
    chunk,
    completion,
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(value)


def _arguments(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _finish_reason(value: Any, *, has_tools: bool = False) -> str:
    if has_tools or value == "tool_use":
        return "tool_calls"
    if value == "max_tokens":
        return "length"
    if value in {"end_turn", "stop_sequence", "pause_turn", None, ""}:
        return "stop"
    return "error"


def _usage(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    input_tokens = max(0, int(source.get("input_tokens") or 0))
    output_tokens = max(0, int(source.get("output_tokens") or 0))
    result = {
        "input_tokens": input_tokens,
        "uncached_input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    cache_read = max(0, int(source.get("cache_read_input_tokens") or 0))
    cache_write = max(0, int(source.get("cache_creation_input_tokens") or 0))
    if cache_read:
        result["cache_read_input_tokens"] = cache_read
    if cache_write:
        result["cache_creation_input_tokens"] = cache_write
    for key in ("cost", "cost_details", "costDetails", "raw"):
        if key in source:
            result[key] = source[key]
    return result


def _append_message(messages: list[dict], role: str, blocks: list[dict]) -> None:
    """Merge adjacent same-role blocks as required by the Messages API."""
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    converted: list[dict] = []

    for message in messages:
        role = message.get("role")
        if role == "system":
            text = _text(message.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "")
            attachments = normalize_attachments(message.get("_nz_attachments"))
            text = _text(message.get("content"))
            content: str | list[dict] = text
            if attachments:
                content = [
                    *([{"type": "text", "text": text}] if text else []),
                    *[
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": item["mime"],
                                "data": attachment_base64(item),
                            },
                        }
                        for item in attachments
                    ],
                ]
            block = {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": content,
            }
            if str(message.get("content") or "").startswith("Error:"):
                block["is_error"] = True
            _append_message(converted, "user", [block])
            continue

        blocks: list[dict] = []
        content_text = _text(message.get("content"))
        if content_text:
            blocks.append({"type": "text", "text": content_text})
        if role == "user":
            for attachment in normalize_attachments(
                message.get("_nz_user_attachments")
            ):
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": attachment["mime"],
                        "data": attachment_base64(attachment),
                    },
                })

        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                tool_call_id = str(tool_call.get("id") or "")
                name = str(function.get("name") or "")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call_id,
                        "name": name,
                        "input": _arguments(function.get("arguments")),
                    }
                )
            _append_message(converted, "assistant", blocks)
        else:
            _append_message(converted, "user", blocks)

    return "\n\n".join(system_parts), converted


def _convert_tools(tools: list[dict] | None) -> list[dict]:
    converted = []
    for tool in tools or []:
        function = tool.get("function") or {}
        converted.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get(
                    "parameters",
                    {"type": "object", "properties": {}},
                ),
            }
        )
    return converted


def _convert_tool_choice(tool_choice: Any) -> dict | None:
    if not tool_choice:
        return None
    if isinstance(tool_choice, str):
        mapping = {
            "auto": {"type": "auto"},
            "required": {"type": "any"},
            "none": {"type": "none"},
        }
        return mapping.get(tool_choice)
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") == "function":
        function = tool_choice.get("function") or {}
        name = function.get("name")
        return {"type": "tool", "name": name} if name else None
    return tool_choice


class AnthropicProvider:
    """Native adapter for ``POST /v1/messages``."""

    name = "anthropic"
    uses_capability_snapshot = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        api_version: str = "2023-06-01",
        transport=None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.api_version = api_version
        self._transport = transport or UrllibTransport(
            timeout_seconds=config.PROVIDER_HARD_TIMEOUT_SECONDS
        )

    def create_client(self) -> NativeClient:
        client = NativeClient(api_key=self.api_key, base_url=self.base_url)
        client.bind_completion(
            lambda **kwargs: self.create_completion(client, **kwargs),
        )
        return client

    def capabilities(self, model_id: str) -> ModelCapabilities:
        """Resolve native Anthropic model capabilities."""
        return configured_model_capabilities(self.name, model_id)

    def create_completion(self, client: NativeClient, **kwargs):
        capabilities = kwargs.pop("_capabilities", None)
        if not isinstance(capabilities, ModelCapabilities):
            capabilities = self.capabilities(str(kwargs.get("model") or ""))
        system, messages = _convert_messages(list(kwargs.get("messages") or []))
        payload = {
            "model": kwargs["model"],
            "max_tokens": int(kwargs.get("max_tokens") or 8000),
            "messages": messages,
        }
        if system:
            payload["system"] = system
        tools = _convert_tools(kwargs.get("tools"))
        if tools:
            payload["tools"] = tools
            tool_choice = _convert_tool_choice(kwargs.get("tool_choice"))
            if tool_choice:
                payload["tool_choice"] = tool_choice
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]
        variant_options = variant_request_options(
            capabilities
        )
        thinking = variant_options.get("thinking")
        if isinstance(thinking, dict):
            payload["thinking"] = dict(thinking)
        effort = variant_options.get("effort")
        if isinstance(effort, str) and effort:
            payload["output_config"] = {"effort": effort}

        stream = bool(kwargs.get("stream"))
        if stream:
            payload["stream"] = True
        url = self._messages_url(client.base_url)
        headers = {
            "x-api-key": client.api_key,
            "anthropic-version": self.api_version,
        }
        if stream:
            events = self._transport.post_sse(url, headers, payload)
            return self._normalize_stream(events)
        response = self._transport.post_json(url, headers, payload)
        return self._normalize_response(response)

    @staticmethod
    def _messages_url(base_url: str) -> str:
        base = base_url.rstrip("/")
        return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"

    @staticmethod
    def _normalize_response(payload: dict):
        if payload.get("type") == "error":
            error = payload.get("error") or {}
            raise RuntimeError(f"Anthropic API error: {error.get('message') or error}")

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[NormalizedToolCall] = []
        for block in payload.get("content") or []:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block_type == "thinking":
                reasoning_parts.append(str(block.get("thinking") or ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    NormalizedToolCall(
                        index=len(tool_calls),
                        id=str(block.get("id") or ""),
                        function=NormalizedFunction(
                            name=str(block.get("name") or ""),
                            arguments=json.dumps(
                                block.get("input") or {},
                                ensure_ascii=False,
                            ),
                        ),
                    )
                )
        return completion(
            content="".join(text_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts),
            finish_reason=_finish_reason(
                payload.get("stop_reason"),
                has_tools=bool(tool_calls),
            ),
            usage=_usage(payload.get("usage")),
        )

    @staticmethod
    def _normalize_stream(events: Iterable[dict]) -> Iterator:
        tool_indexes: dict[int, int] = {}
        next_tool_index = 0
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        for event in events:
            event_type = event.get("type")
            if event_type == "error":
                error = event.get("error") or {}
                raise RuntimeError(
                    f"Anthropic stream error: {error.get('message') or error}",
                )
            if event_type == "message_start":
                usage = _usage((event.get("message") or {}).get("usage"))
                input_tokens = usage["input_tokens"]
                output_tokens = usage["output_tokens"]
                cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                cache_write_tokens = usage.get("cache_creation_input_tokens", 0)
                stream_usage = {
                    "input_tokens": input_tokens,
                    "uncached_input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
                if cache_read_tokens:
                    stream_usage["cache_read_input_tokens"] = cache_read_tokens
                if cache_write_tokens:
                    stream_usage["cache_creation_input_tokens"] = cache_write_tokens
                yield chunk(usage=stream_usage)
                continue
            if event_type == "message_delta":
                usage = _usage(event.get("usage"))
                if usage["input_tokens"]:
                    input_tokens = usage["input_tokens"]
                if usage["output_tokens"]:
                    output_tokens = usage["output_tokens"]
                if usage.get("cache_read_input_tokens"):
                    cache_read_tokens = usage["cache_read_input_tokens"]
                if usage.get("cache_creation_input_tokens"):
                    cache_write_tokens = usage["cache_creation_input_tokens"]
                delta = event.get("delta") or {}
                stream_usage = {
                    "input_tokens": input_tokens,
                    "uncached_input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
                if cache_read_tokens:
                    stream_usage["cache_read_input_tokens"] = cache_read_tokens
                if cache_write_tokens:
                    stream_usage["cache_creation_input_tokens"] = cache_write_tokens
                yield chunk(
                    finish_reason=_finish_reason(
                        delta.get("stop_reason"),
                        has_tools=bool(tool_indexes),
                    ),
                    usage=stream_usage,
                )
                continue
            if event_type == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    block_index = int(event.get("index") or 0)
                    tool_indexes[block_index] = next_tool_index
                    yield chunk(
                        tool_calls=[
                            NormalizedToolCall(
                                index=next_tool_index,
                                id=str(block.get("id") or ""),
                                function=NormalizedFunction(
                                    name=str(block.get("name") or ""),
                                ),
                            )
                        ]
                    )
                    next_tool_index += 1
                continue
            if event_type != "content_block_delta":
                continue

            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                yield chunk(content=str(delta.get("text") or ""))
            elif delta_type == "thinking_delta":
                yield chunk(reasoning_content=str(delta.get("thinking") or ""))
            elif delta_type == "input_json_delta":
                block_index = int(event.get("index") or 0)
                tool_index = tool_indexes.get(block_index, block_index)
                yield chunk(
                    tool_calls=[
                        NormalizedToolCall(
                            index=tool_index,
                            function=NormalizedFunction(
                                arguments=str(delta.get("partial_json") or ""),
                            ),
                        )
                    ]
                )
