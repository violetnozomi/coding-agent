"""Native OpenAI Responses API adapter with provider-neutral normalization."""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Iterator

from openai import OpenAI

from nz_coder.foundation import config
from nz_coder.protocol.attachments import openai_chat_messages
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


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(mode="json", exclude_none=True)
        except TypeError:
            result = dump()
        return dict(result) if isinstance(result, dict) else {}
    return {}


def _reasoning_item(value: Any) -> dict[str, Any] | None:
    """Return the replay-safe subset of one Responses reasoning item."""
    data = _as_dict(value)
    if data.get("type") != "reasoning" or not isinstance(data.get("id"), str):
        return None
    result: dict[str, Any] = {"type": "reasoning", "id": data["id"]}
    for name in ("encrypted_content", "summary", "content", "status"):
        item = data.get(name)
        if item is not None:
            result[name] = item
    return result


def _tool_definition(spec: dict[str, Any]) -> dict[str, Any]:
    function = spec.get("function") if isinstance(spec, dict) else None
    if spec.get("type") != "function" or not isinstance(function, dict):
        raise ValueError("Responses API only accepts function tool definitions")
    name = function.get("name")
    parameters = function.get("parameters")
    if not isinstance(name, str) or not name:
        raise ValueError("Responses function tool name must be non-empty")
    if not isinstance(parameters, dict):
        raise ValueError(f"Responses function tool '{name}' parameters must be an object")
    result = {
        "type": "function",
        "name": name,
        "parameters": parameters,
    }
    description = function.get("description")
    if isinstance(description, str) and description:
        result["description"] = description
    if isinstance(function.get("strict"), bool):
        result["strict"] = function["strict"]
    return result


def _tool_choice(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    function = value.get("function")
    if value.get("type") == "function" and isinstance(function, dict):
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Responses function tool choice requires a name")
        return {"type": "function", "name": name}
    return value


def _message_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Chat Completions history into Responses input items."""
    result: list[dict[str, Any]] = []
    replayed_reasoning_ids: set[str] = set()
    for message in openai_chat_messages(messages):
        role = str(message.get("role") or "")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Responses tool output requires tool_call_id")
            output = message.get("content", "")
            if not isinstance(output, str):
                output = str(output)
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )
            continue

        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"Responses API does not support message role '{role}'")
        content = message.get("content", "")
        if isinstance(content, list):
            normalized_content = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    normalized_content.append({
                        "type": "input_text",
                        "text": str(part.get("text") or ""),
                    })
                elif part.get("type") == "image_url":
                    image = part.get("image_url")
                    url = image.get("url") if isinstance(image, dict) else None
                    if isinstance(url, str) and url:
                        normalized_content.append({
                            "type": "input_image",
                            "image_url": url,
                        })
            content = normalized_content
        if content not in (None, ""):
            result.append({"role": role, "content": content})

        if role != "assistant":
            continue
        message_extra = message.get("provider_extra")
        if isinstance(message_extra, dict):
            message_reasoning = message_extra.get("openai_reasoning_items") or []
            if isinstance(message_reasoning, list):
                for item in message_reasoning:
                    safe = _reasoning_item(item)
                    if safe is None or safe["id"] in replayed_reasoning_ids:
                        continue
                    replayed_reasoning_ids.add(safe["id"])
                    result.append(safe)
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise ValueError("Responses assistant tool_calls must be an array")
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                raise ValueError("Responses assistant tool call must be an object")
            extra = tool_call.get("provider_extra")
            if isinstance(extra, dict):
                reasoning_items = extra.get("openai_reasoning_items") or []
                if isinstance(reasoning_items, list):
                    for item in reasoning_items:
                        safe = _reasoning_item(item)
                        if safe is None or safe["id"] in replayed_reasoning_ids:
                            continue
                        replayed_reasoning_ids.add(safe["id"])
                        result.append(safe)
            function = tool_call.get("function")
            call_id = tool_call.get("id")
            if not isinstance(function, dict) or not isinstance(call_id, str) or not call_id:
                raise ValueError("Responses assistant tool call is missing id/function")
            name = function.get("name")
            arguments = function.get("arguments", "")
            if isinstance(arguments, dict):
                arguments = json.dumps(
                    arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            if not isinstance(name, str) or not name or not isinstance(arguments, str):
                raise ValueError("Responses assistant tool call has invalid name/arguments")
            item = {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
            if isinstance(extra, dict) and isinstance(
                extra.get("openai_response_item_id"),
                str,
            ):
                item["id"] = extra["openai_response_item_id"]
            result.append(item)
    return result


def _response_error(response: Any) -> str:
    error = _field(response, "error")
    if error:
        return str(_field(error, "message", error))
    details = _field(response, "incomplete_details")
    if details:
        return str(_field(details, "reason", details))
    return ""


def _response_usage(response: Any) -> dict[str, Any]:
    usage = _field(response, "usage", {}) or {}
    input_tokens = max(0, int(_field(usage, "input_tokens", 0) or 0))
    output_tokens = max(0, int(_field(usage, "output_tokens", 0) or 0))
    total_tokens = max(
        0,
        int(_field(usage, "total_tokens", input_tokens + output_tokens) or 0),
    )
    input_details = _field(usage, "input_tokens_details", {}) or {}
    output_details = _field(usage, "output_tokens_details", {}) or {}
    cache_read = max(0, int(_field(input_details, "cached_tokens", 0) or 0))
    result = {
        "input_tokens": input_tokens,
        "uncached_input_tokens": max(0, input_tokens - cache_read),
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or input_tokens + output_tokens,
    }
    reasoning_tokens = max(
        0,
        int(_field(output_details, "reasoning_tokens", 0) or 0),
    )
    if cache_read:
        result["cache_read_input_tokens"] = cache_read
    if reasoning_tokens:
        result["reasoning_tokens"] = reasoning_tokens
    for key in ("cost", "cost_details", "costDetails", "raw"):
        reported = _field(usage, key)
        if reported is not None:
            result[key] = reported
    return result


def _response_finish_reason(response: Any, *, has_tools: bool = False) -> str:
    status = str(_field(response, "status", "completed") or "completed")
    if status == "incomplete":
        details = _field(response, "incomplete_details", {}) or {}
        if _field(details, "reason", "") == "max_output_tokens":
            return "length"
        return "error"
    if has_tools:
        return "tool_calls"
    return "stop"


def _normalize_response(response: Any):
    status = str(_field(response, "status", "completed") or "completed")
    if status in {"failed", "cancelled"}:
        raise RuntimeError(f"OpenAI Responses request {status}: {_response_error(response)}")

    text_parts: list[str] = []
    reasoning_items: list[dict[str, Any]] = []
    reasoning_summary: list[str] = []
    tool_calls: list[NormalizedToolCall] = []
    for output_index, item in enumerate(_field(response, "output", []) or []):
        item_type = _field(item, "type", "")
        if item_type == "reasoning":
            safe = _reasoning_item(item)
            if safe is not None:
                reasoning_items.append(safe)
            for summary in _field(item, "summary", []) or []:
                text = _field(summary, "text", "")
                if isinstance(text, str) and text:
                    reasoning_summary.append(text)
            continue
        if item_type == "message":
            for part in _field(item, "content", []) or []:
                if _field(part, "type", "") == "output_text":
                    text = _field(part, "text", "")
                    if isinstance(text, str):
                        text_parts.append(text)
            continue
        if item_type != "function_call":
            continue
        call_id = _field(item, "call_id", "") or _field(item, "id", "")
        extra: dict[str, Any] = {}
        response_item_id = _field(item, "id", "")
        if isinstance(response_item_id, str) and response_item_id:
            extra["openai_response_item_id"] = response_item_id
        if reasoning_items:
            extra["openai_reasoning_items"] = list(reasoning_items)
        tool_calls.append(
            NormalizedToolCall(
                index=output_index,
                id=str(call_id or ""),
                function=NormalizedFunction(
                    name=str(_field(item, "name", "") or ""),
                    arguments=str(_field(item, "arguments", "") or ""),
                ),
                provider_extra=extra,
            )
        )
    return completion(
        content="".join(text_parts),
        tool_calls=tool_calls,
        reasoning_content="".join(reasoning_summary),
        provider_extra=(
            {"openai_reasoning_items": reasoning_items}
            if reasoning_items
            else None
        ),
        finish_reason=_response_finish_reason(
            response,
            has_tools=bool(tool_calls),
        ),
        usage=_response_usage(response),
    )


def _normalize_stream(events: Iterable[Any]) -> Iterator[Any]:
    reasoning_items: list[dict[str, Any]] = []
    seen_tools: set[int] = set()
    argument_deltas: set[int] = set()
    for event in events:
        event_type = str(_field(event, "type", "") or "")
        output_index = int(_field(event, "output_index", 0) or 0)
        if event_type == "response.output_text.delta":
            yield chunk(content=str(_field(event, "delta", "") or ""))
        elif event_type == "response.reasoning_summary_text.delta":
            yield chunk(reasoning_content=str(_field(event, "delta", "") or ""))
        elif event_type == "response.output_item.added":
            item = _field(event, "item", {})
            if _field(item, "type", "") == "function_call":
                seen_tools.add(output_index)
                call_id = _field(item, "call_id", "") or _field(item, "id", "")
                extra: dict[str, Any] = {}
                item_id = _field(item, "id", "")
                if isinstance(item_id, str) and item_id:
                    extra["openai_response_item_id"] = item_id
                if reasoning_items:
                    extra["openai_reasoning_items"] = list(reasoning_items)
                yield chunk(
                    tool_calls=[
                        NormalizedToolCall(
                            index=output_index,
                            id=str(call_id or ""),
                            function=NormalizedFunction(
                                name=str(_field(item, "name", "") or ""),
                            ),
                            provider_extra=extra,
                        )
                    ]
                )
        elif event_type == "response.function_call_arguments.delta":
            argument_deltas.add(output_index)
            yield chunk(
                tool_calls=[
                    NormalizedToolCall(
                        index=output_index,
                        function=NormalizedFunction(
                            arguments=str(_field(event, "delta", "") or ""),
                        ),
                    )
                ]
            )
        elif event_type == "response.output_item.done":
            item = _field(event, "item", {})
            item_type = _field(item, "type", "")
            if item_type == "reasoning":
                safe = _reasoning_item(item)
                if safe is not None:
                    reasoning_items.append(safe)
                    yield chunk(
                        provider_extra={
                            "openai_reasoning_items": list(reasoning_items),
                        }
                    )
                continue
            if item_type != "function_call":
                continue
            extra: dict[str, Any] = {}
            item_id = _field(item, "id", "")
            if isinstance(item_id, str) and item_id:
                extra["openai_response_item_id"] = item_id
            if reasoning_items:
                extra["openai_reasoning_items"] = list(reasoning_items)
            if output_index in seen_tools:
                yield chunk(
                    tool_calls=[
                        NormalizedToolCall(
                            index=output_index,
                            provider_extra=extra,
                        )
                    ]
                )
                continue
            call_id = _field(item, "call_id", "") or _field(item, "id", "")
            arguments = "" if output_index in argument_deltas else str(
                _field(item, "arguments", "") or ""
            )
            yield chunk(
                tool_calls=[
                    NormalizedToolCall(
                        index=output_index,
                        id=str(call_id or ""),
                        function=NormalizedFunction(
                            name=str(_field(item, "name", "") or ""),
                            arguments=arguments,
                        ),
                        provider_extra=extra,
                    )
                ]
            )
        elif event_type == "response.completed":
            response = _field(event, "response", {})
            yield chunk(
                finish_reason=_response_finish_reason(
                    response,
                    has_tools=bool(seen_tools),
                ),
                usage=_response_usage(response),
            )
        elif event_type == "response.incomplete":
            response = _field(event, "response", {})
            finish_reason = _response_finish_reason(
                response,
                has_tools=bool(seen_tools),
            )
            if finish_reason == "length":
                yield chunk(
                    finish_reason=finish_reason,
                    usage=_response_usage(response),
                )
            else:
                raise RuntimeError(
                    "OpenAI Responses stream incomplete: "
                    f"{_response_error(response)}"
                )
        elif event_type == "response.failed":
            response = _field(event, "response", {})
            raise RuntimeError(
                "OpenAI Responses stream failed: "
                f"{_response_error(response)}"
            )
        elif event_type == "error":
            raise RuntimeError(
                f"OpenAI Responses stream error: {_field(event, 'message', event)}"
            )


class OpenAIResponsesProvider:
    """Adapter for OpenAI's agent-oriented Responses API."""

    name = "openai-responses"
    uses_capability_snapshot = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        provider_name: str = "openai-responses",
        client_factory: Callable[..., Any] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.name = str(provider_name or "openai-responses").strip().lower()
        self._client_factory = client_factory or OpenAI
        self._uses_default_client_factory = client_factory is None

    def create_client(self) -> Any:
        """Create the official OpenAI SDK client."""
        kwargs = {"api_key": self.api_key, "base_url": self.base_url}
        if self._uses_default_client_factory:
            kwargs["timeout"] = config.PROVIDER_HARD_TIMEOUT_SECONDS
        return self._client_factory(**kwargs)

    def create_completion(self, client: Any, **kwargs: Any) -> Any:
        """Translate the existing loop contract to Responses and normalize output."""
        capabilities = kwargs.pop("_capabilities", None)
        if not isinstance(capabilities, ModelCapabilities):
            capabilities = self.capabilities(str(kwargs.get("model") or ""))
        messages = kwargs.pop("messages", [])
        tools = kwargs.pop("tools", [])
        max_tokens = kwargs.pop("max_tokens", None)
        request: dict[str, Any] = {
            "model": kwargs.pop("model"),
            "input": _message_input(messages),
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if tools and capabilities.supports_tools:
            request["tools"] = [_tool_definition(spec) for spec in tools]
        if "tool_choice" in kwargs and capabilities.supports_tools:
            request["tool_choice"] = _tool_choice(kwargs.pop("tool_choice"))
        if max_tokens is not None:
            request["max_output_tokens"] = max_tokens
        if capabilities.supports_streaming and "stream" in kwargs:
            request["stream"] = bool(kwargs.pop("stream"))
        if capabilities.supports_temperature and "temperature" in kwargs:
            request["temperature"] = kwargs.pop("temperature")
        for name in ("parallel_tool_calls", "top_p"):
            if name in kwargs:
                request[name] = kwargs.pop(name)
        options = variant_request_options(capabilities)
        effort = options.get("reasoning_effort")
        if isinstance(effort, str) and effort:
            request["reasoning"] = {"effort": effort}
        if "top_p" in options:
            request["top_p"] = options["top_p"]
        if kwargs:
            names = ", ".join(sorted(kwargs))
            raise ValueError(f"Unsupported OpenAI Responses option(s): {names}")
        raw = client.responses.create(**request)
        if request.get("stream"):
            return _normalize_stream(raw)
        return _normalize_response(raw)

    def capabilities(self, model_id: str) -> ModelCapabilities:
        """Resolve model metadata while retaining this adapter identity."""
        return configured_model_capabilities(self.name, model_id)
