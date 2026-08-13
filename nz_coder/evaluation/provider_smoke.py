"""Opt-in live smoke tests for configured model providers.

No request is sent unless ``--confirm-live`` is supplied.  The checks exercise
plain text, a complete tool-call round trip, and streaming normalization.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import time
from typing import Any, Callable

from nz_coder import config
from nz_coder.providers import create_provider

_CHECK_NAMES = ("text", "tool", "stream")
_SMOKE_TOOL = {
    "type": "function",
    "function": {
        "name": "provider_smoke_echo",
        "description": "Return the supplied value unchanged.",
        "parameters": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        },
    },
}


@dataclass(frozen=True)
class SmokeCheckResult:
    """Result of one live provider capability check."""

    name: str
    ok: bool
    elapsed_seconds: float
    detail: str


@dataclass(frozen=True)
class ProviderSmokeReport:
    """Safe-to-print report containing no credentials or response bodies."""

    provider: str
    model: str
    checks: list[SmokeCheckResult]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


def _tool_call_dict(tool_call: Any) -> dict:
    if isinstance(tool_call, dict):
        payload = tool_call
    elif hasattr(tool_call, "model_dump"):
        payload = tool_call.model_dump()
    else:
        function = getattr(tool_call, "function", None)
        payload = {
            "id": getattr(tool_call, "id", ""),
            "type": "function",
            "function": {
                "name": getattr(function, "name", ""),
                "arguments": getattr(function, "arguments", ""),
            },
        }
    function = payload.get("function") or {}
    normalized = {
        "id": str(payload.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(function.get("name") or ""),
            "arguments": function.get("arguments") or "{}",
        },
    }
    provider_extra = payload.get("provider_extra")
    if isinstance(provider_extra, dict) and provider_extra:
        normalized["provider_extra"] = dict(provider_extra)
    return normalized


def _message_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("provider returned no choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise RuntimeError("provider returned no assistant message")
    return str(getattr(message, "content", "") or "")


def _run_check(name: str, check: Callable[[], str]) -> SmokeCheckResult:
    started = time.perf_counter()
    try:
        detail = check()
    except Exception as exc:
        return SmokeCheckResult(
            name=name,
            ok=False,
            elapsed_seconds=time.perf_counter() - started,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return SmokeCheckResult(
        name=name,
        ok=True,
        elapsed_seconds=time.perf_counter() - started,
        detail=detail,
    )


def run_provider_smoke(
    provider,
    *,
    model: str,
    checks: tuple[str, ...] = _CHECK_NAMES,
) -> ProviderSmokeReport:
    """Run selected live checks against an already configured provider."""
    unknown = sorted(set(checks) - set(_CHECK_NAMES))
    if unknown:
        raise ValueError(f"Unknown smoke checks: {', '.join(unknown)}")
    if not model.strip():
        raise ValueError("model must not be empty")

    client = provider.create_client()
    base_messages = [
        {
            "role": "system",
            "content": "You are a provider connectivity smoke test.",
        },
    ]

    def text_check() -> str:
        response = provider.create_completion(
            client,
            model=model,
            messages=base_messages + [
                {
                    "role": "user",
                    "content": "Reply with exactly NZ_PROVIDER_SMOKE_OK",
                },
            ],
            max_tokens=64,
        )
        content = _message_text(response)
        if not content.strip():
            raise RuntimeError("provider returned empty text")
        return f"non_empty_text chars={len(content)}"

    def tool_check() -> str:
        messages = base_messages + [
            {
                "role": "user",
                "content": (
                    "Call provider_smoke_echo exactly once with value "
                    "'NZ_TOOL_SMOKE_OK'. After receiving the tool result, "
                    "reply with the echoed value."
                ),
            },
        ]
        forced_tool_choice = True
        try:
            response = provider.create_completion(
                client,
                model=model,
                messages=messages,
                tools=[_SMOKE_TOOL],
                tool_choice={
                    "type": "function",
                    "function": {"name": "provider_smoke_echo"},
                },
                max_tokens=256,
            )
        except Exception as exc:
            if "tool_choice" not in str(exc).lower():
                raise
            forced_tool_choice = False
            response = provider.create_completion(
                client,
                model=model,
                messages=messages,
                tools=[_SMOKE_TOOL],
                max_tokens=256,
            )
        message = response.choices[0].message
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if not tool_calls:
            raise RuntimeError("provider returned no tool call")
        tool_call = _tool_call_dict(tool_calls[0])
        if tool_call["function"]["name"] != "provider_smoke_echo":
            raise RuntimeError("provider called the wrong tool")
        arguments = tool_call["function"]["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict) or not arguments.get("value"):
            raise RuntimeError("provider returned invalid tool arguments")

        follow_up = messages + [
            {
                "role": "assistant",
                "content": str(getattr(message, "content", "") or ""),
                "tool_calls": [tool_call],
            },
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps(
                    {"echo": arguments["value"]},
                    ensure_ascii=False,
                ),
            },
        ]
        final_response = provider.create_completion(
            client,
            model=model,
            messages=follow_up,
            max_tokens=1024,
        )
        final_message = final_response.choices[0].message
        final_text = str(getattr(final_message, "content", "") or "")
        if not final_text.strip():
            reasoning = str(
                getattr(final_message, "reasoning_content", "") or ""
            )
            repeated_calls = list(
                getattr(final_message, "tool_calls", None) or []
            )
            raise RuntimeError(
                "provider returned empty tool-result follow-up "
                f"reasoning_chars={len(reasoning)} "
                f"tool_calls={len(repeated_calls)}"
            )
        return (
            "tool_call_and_result_round_trip "
            f"forced_tool_choice={forced_tool_choice}"
        )

    def stream_check() -> str:
        stream = provider.create_completion(
            client,
            model=model,
            messages=base_messages + [
                {
                    "role": "user",
                    "content": "Reply with exactly NZ_STREAM_SMOKE_OK",
                },
            ],
            max_tokens=64,
            stream=True,
        )
        parts = []
        for response_chunk in stream:
            choices = getattr(response_chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", "") if delta is not None else ""
            if content:
                parts.append(str(content))
        content = "".join(parts)
        if not content.strip():
            raise RuntimeError("provider returned no streamed text")
        return f"streamed_text chars={len(content)}"

    runners = {
        "text": text_check,
        "tool": tool_check,
        "stream": stream_check,
    }
    results = [_run_check(name, runners[name]) for name in checks]
    return ProviderSmokeReport(
        provider=str(getattr(provider, "name", type(provider).__name__)),
        model=model,
        checks=results,
    )


def _parse_checks(value: str) -> tuple[str, ...]:
    checks = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    unknown = sorted(set(checks) - set(_CHECK_NAMES))
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown checks: {', '.join(unknown)}",
        )
    if not checks:
        raise argparse.ArgumentTypeError("at least one check is required")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run opt-in live text/tool/stream checks for a model provider.",
    )
    parser.add_argument("--provider", default=config.MODEL_PROVIDER)
    parser.add_argument("--model", default=config.MODEL_ID)
    parser.add_argument("--checks", type=_parse_checks, default=_CHECK_NAMES)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.confirm_live:
        print(
            "Dry run only: no API request sent. "
            f"provider={args.provider} model={args.model} "
            f"checks={','.join(args.checks)}. "
            "Add --confirm-live to execute billable requests."
        )
        return 0

    provider = create_provider(args.provider)
    report = run_provider_smoke(
        provider,
        model=args.model,
        checks=args.checks,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"provider={report.provider} model={report.model} ok={report.ok}")
        for check in report.checks:
            print(
                f"- {check.name}: ok={check.ok} "
                f"elapsed={check.elapsed_seconds:.2f}s detail={check.detail}"
            )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
