"""Small standard-library JSON and SSE HTTP transport for native providers."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Any, Callable, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nz_coder.foundation.json_safety import reject_nonstandard_json_constant


_MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_PROVIDER_ERROR_BYTES = 64 * 1024


class _CompletionsProxy:
    def __init__(self, client: "NativeClient"):
        self._client = client

    def create(self, **kwargs):
        handler = self._client._completion_handler
        if handler is None:
            raise RuntimeError("Native provider client is not bound to a completion handler")
        return handler(**kwargs)


class _ChatProxy:
    def __init__(self, client: "NativeClient"):
        self.completions = _CompletionsProxy(client)


@dataclass
class NativeClient:
    """Native credentials plus an OpenAI-shape compatibility proxy."""

    api_key: str
    base_url: str
    _completion_handler: Callable[..., Any] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    chat: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.chat = _ChatProxy(self)

    def bind_completion(self, handler: Callable[..., Any]) -> None:
        """Bind legacy ``client.chat.completions.create`` callers."""
        self._completion_handler = handler


class UrllibTransport:
    """HTTP transport with explicit JSON errors and incremental SSE parsing."""

    def __init__(self, timeout_seconds: float = 300):
        if isinstance(timeout_seconds, bool):
            raise ValueError("Provider HTTP timeout must be a positive finite number")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "Provider HTTP timeout must be a positive finite number"
            ) from exc
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
            raise ValueError(
                "Provider HTTP timeout must be a positive finite number "
                "no greater than 600 seconds"
            )
        self.timeout_seconds = timeout

    def post_json(self, url: str, headers: dict[str, str], payload: dict) -> dict:
        response = self._open(url, headers, payload)
        with response:
            body = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise RuntimeError("Provider JSON response exceeds 64 MiB")
        try:
            parsed = json.loads(
                body.decode("utf-8"),
                parse_constant=reject_nonstandard_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Provider returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Provider returned a non-object JSON response")
        return parsed

    def post_sse(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict,
    ) -> Iterable[dict]:
        response = self._open(url, headers, payload)
        return self._iter_sse(response)

    def _open(self, url: str, headers: dict[str, str], payload: dict):
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Provider request payload must contain strict JSON") from exc
        request = Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            return urlopen(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            body = exc.read(_MAX_PROVIDER_ERROR_BYTES + 1).decode(
                "utf-8",
                errors="replace",
            )
            if len(body) > _MAX_PROVIDER_ERROR_BYTES:
                body = body[:_MAX_PROVIDER_ERROR_BYTES] + "[truncated]"
            raise RuntimeError(f"Provider HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Provider connection error: {exc.reason}") from exc

    @staticmethod
    def _iter_sse(response) -> Iterator[dict]:
        data_lines: list[str] = []
        total_bytes = 0
        try:
            for raw_line in response:
                total_bytes += len(raw_line)
                if total_bytes > _MAX_PROVIDER_RESPONSE_BYTES:
                    raise RuntimeError("Provider SSE response exceeds 64 MiB")
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    yield from UrllibTransport._decode_sse_data(data_lines)
                    data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            yield from UrllibTransport._decode_sse_data(data_lines)
        finally:
            response.close()

    @staticmethod
    def _decode_sse_data(data_lines: list[str]) -> Iterator[dict]:
        if not data_lines:
            return
        data = "\n".join(data_lines)
        if data == "[DONE]":
            return
        try:
            parsed: Any = json.loads(
                data,
                parse_constant=reject_nonstandard_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Provider returned invalid JSON SSE data: {exc}") from exc
        if isinstance(parsed, dict):
            yield parsed
