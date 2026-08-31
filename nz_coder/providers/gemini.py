"""Native Gemini generateContent provider with response normalization."""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Iterator
from urllib.parse import quote

from nz_coder.foundation import config
from nz_coder.protocol.attachments import attachment_base64, normalize_attachments
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


def _append_content(contents: list[dict], role: str, parts: list[dict]) -> None:
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})


def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    contents: list[dict] = []
    tool_names: dict[str, str] = {}

    for message in messages:
        role = message.get("role")
        if role == "system":
            text = _text(message.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "")
            function_response = {
                "name": tool_names.get(tool_call_id, "tool"),
                "response": {"output": _text(message.get("content"))},
            }
            if tool_call_id:
                function_response["id"] = tool_call_id
            _append_content(
                contents,
                "user",
                [{"functionResponse": function_response}],
            )
            attachments = normalize_attachments(message.get("_nz_attachments"))
            if attachments:
                _append_content(
                    contents,
                    "user",
                    [
                        {
                            "text": (
                                "The following images were returned by the preceding "
                                "tool result."
                            )
                        },
                        *[
                            {
                                "inlineData": {
                                    "mimeType": item["mime"],
                                    "data": attachment_base64(item),
                                }
                            }
                            for item in attachments
                        ],
                    ],
                )
            continue

        parts: list[dict] = []
        content_text = _text(message.get("content"))
        if content_text:
            parts.append({"text": content_text})
        if role == "user":
            for attachment in normalize_attachments(
                message.get("_nz_user_attachments")
            ):
                parts.append({
                    "inlineData": {
                        "mimeType": attachment["mime"],
                        "data": attachment_base64(attachment),
                    }
                })

        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                tool_call_id = str(tool_call.get("id") or "")
                name = str(function.get("name") or "")
                tool_names[tool_call_id] = name
                function_call = {
                    "name": name,
                    "args": _arguments(function.get("arguments")),
                }
                if tool_call_id:
                    function_call["id"] = tool_call_id
                part = {"functionCall": function_call}
                provider_extra = tool_call.get("provider_extra") or {}
                signature = provider_extra.get("thoughtSignature")
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            _append_content(contents, "model", parts)
        else:
            _append_content(contents, "user", parts)

    return "\n\n".join(system_parts), contents


def _convert_tools(tools: list[dict] | None) -> list[dict]:
    declarations = []
    for tool in tools or []:
        function = tool.get("function") or {}
        declarations.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "parameters": function.get(
                    "parameters",
                    {"type": "object", "properties": {}},
                ),
            }
        )
    return [{"functionDeclarations": declarations}] if declarations else []


def _convert_tool_config(tool_choice: Any) -> dict | None:
    if not tool_choice or tool_choice == "auto":
        return None
    if isinstance(tool_choice, str):
        mode = {"required": "ANY", "none": "NONE"}.get(tool_choice)
        if not mode:
            return None
        return {"functionCallingConfig": {"mode": mode}}
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") == "function":
        function = tool_choice.get("function") or {}
        name = function.get("name")
        if name:
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [name],
                }
            }
    return None


class GeminiProvider:
    """Native adapter for Gemini ``generateContent`` REST endpoints."""

    name = "gemini"
    uses_capability_snapshot = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        transport=None,
    ):
        self.api_key = api_key
        self.base_url = base_url
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
        """Resolve native Gemini model capabilities."""
        return configured_model_capabilities(self.name, model_id)

    def create_completion(self, client: NativeClient, **kwargs):
        capabilities = kwargs.pop("_capabilities", None)
        if not isinstance(capabilities, ModelCapabilities):
            capabilities = self.capabilities(str(kwargs.get("model") or ""))
        system, contents = _convert_messages(list(kwargs.get("messages") or []))
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        tools = _convert_tools(kwargs.get("tools"))
        if tools:
            payload["tools"] = tools
            tool_config = _convert_tool_config(kwargs.get("tool_choice"))
            if tool_config:
                payload["toolConfig"] = tool_config

        generation_config: dict[str, Any] = {
            "maxOutputTokens": int(kwargs.get("max_tokens") or 8000),
        }
        if kwargs.get("temperature") is not None:
            generation_config["temperature"] = kwargs["temperature"]
        variant_options = variant_request_options(
            capabilities
        )
        thinking_config = variant_options.get("thinking_config")
        if isinstance(thinking_config, dict):
            generation_config["thinkingConfig"] = dict(thinking_config)
        payload["generationConfig"] = generation_config

        stream = bool(kwargs.get("stream"))
        url = self._content_url(client.base_url, str(kwargs["model"]), stream)
        headers = {"x-goog-api-key": client.api_key}
        if stream:
            events = self._transport.post_sse(url, headers, payload)
            return self._normalize_stream(events)
        response = self._transport.post_json(url, headers, payload)
        return self._normalize_response(response)

    @staticmethod
    def _content_url(base_url: str, model: str, stream: bool) -> str:
        base = base_url.rstrip("/")
        model_name = model.removeprefix("models/")
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{base}/models/{quote(model_name, safe='-_.')}:{method}"

    @staticmethod
    def _parts(payload: dict) -> list[dict]:
        if payload.get("error"):
            error = payload["error"]
            raise RuntimeError(f"Gemini API error: {error.get('message') or error}")
        candidates = payload.get("candidates") or []
        if not candidates:
            return []
        return list((candidates[0].get("content") or {}).get("parts") or [])

    @staticmethod
    def _normalize_parts(parts: list[dict]):
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[NormalizedToolCall] = []
        for part in parts:
            if "text" in part:
                target = reasoning_parts if part.get("thought") else text_parts
                target.append(str(part.get("text") or ""))
            function_call = part.get("functionCall")
            if function_call:
                index = len(tool_calls)
                call_id = str(function_call.get("id") or f"gemini-call-{index}")
                provider_extra = {}
                if part.get("thoughtSignature"):
                    provider_extra["thoughtSignature"] = part["thoughtSignature"]
                tool_calls.append(
                    NormalizedToolCall(
                        index=index,
                        id=call_id,
                        function=NormalizedFunction(
                            name=str(function_call.get("name") or ""),
                            arguments=json.dumps(
                                function_call.get("args") or {},
                                ensure_ascii=False,
                            ),
                        ),
                        provider_extra=provider_extra,
                    )
                )
        return "".join(text_parts), "".join(reasoning_parts), tool_calls

    @classmethod
    def _normalize_response(cls, payload: dict):
        text, reasoning, tool_calls = cls._normalize_parts(cls._parts(payload))
        candidate = next(iter(payload.get("candidates") or []), {})
        return completion(
            content=text,
            tool_calls=tool_calls,
            reasoning_content=reasoning,
            finish_reason=_gemini_finish_reason(
                candidate.get("finishReason"),
                has_tools=bool(tool_calls),
            ),
            usage=_gemini_usage(payload.get("usageMetadata")),
        )

    @classmethod
    def _normalize_stream(cls, events: Iterable[dict]) -> Iterator:
        for event in events:
            text, reasoning, tool_calls = cls._normalize_parts(cls._parts(event))
            candidate = next(iter(event.get("candidates") or []), {})
            finish_reason = _gemini_finish_reason(
                candidate.get("finishReason"),
                has_tools=bool(tool_calls),
            ) if candidate.get("finishReason") else ""
            usage = _gemini_usage(event.get("usageMetadata"))
            if text or reasoning or tool_calls or finish_reason or any(usage.values()):
                yield chunk(
                    content=text,
                    tool_calls=tool_calls,
                    reasoning_content=reasoning,
                    finish_reason=finish_reason,
                    usage=usage,
                )


def _gemini_finish_reason(value: Any, *, has_tools: bool = False) -> str:
    if has_tools:
        return "tool_calls"
    normalized = str(value or "").strip().upper()
    if normalized == "MAX_TOKENS":
        return "length"
    if normalized in {"", "STOP", "FINISH_REASON_UNSPECIFIED"}:
        return "stop"
    return "error"


def _gemini_usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    input_tokens = _usage_token(source.get("promptTokenCount"))
    output_tokens = _usage_token(source.get("candidatesTokenCount"))
    total_tokens = max(
        input_tokens + output_tokens,
        _usage_token(source.get("totalTokenCount")),
    )
    cache_read = _usage_token(source.get("cachedContentTokenCount"))
    result = {
        "input_tokens": input_tokens,
        "uncached_input_tokens": max(
            0,
            input_tokens - cache_read,
        ),
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    reasoning_tokens = _usage_token(source.get("thoughtsTokenCount"))
    if reasoning_tokens:
        result["reasoning_tokens"] = reasoning_tokens
    if cache_read:
        result["cache_read_input_tokens"] = cache_read
    return result


def _usage_token(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or number < 0 or number > 1_000_000_000_000:
        return 0
    return int(number)
