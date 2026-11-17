"""Small standard-library JSON and SSE HTTP transport for native providers."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Any, Callable, Iterable, Iterator
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nz_coder.foundation.json_safety import reject_nonstandard_json_constant


_MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_PROVIDER_ERROR_BYTES = 64 * 1024


class ProviderHTTPError(RuntimeError):
    """Retain transport evidence for shared classification and recovery."""

    def __init__(self, status_code: int, headers, body: str):
        super().__init__(f"Provider HTTP {status_code}: {body}")
        self.status_code = status_code
        self.headers = headers
        self.body = body


class OwnedStream(Iterator):
    """Retain both adapter and underlying resource until each is closed."""

    def __init__(self, iterator: Iterator, *, close_owner: Callable[[], None]):
        self._iterator = iterator
        self._close_owner = close_owner
        self._adapter_closed = False
        self._owner_closed = False
        self._cancelled = Event()

    def __iter__(self):
        return self

    def __next__(self):
        if self._cancelled.is_set() or self._adapter_closed:
            raise StopIteration
        item = next(self._iterator)
        if self._cancelled.is_set():
            raise StopIteration
        return item

    def request_cancel(self):
        self._cancelled.set()

    def close(self):
        errors = []
        if not self._adapter_closed:
            try:
                close = getattr(self._iterator, "close", None)
                if close is not None:
                    close()
                self._adapter_closed = True
            except BaseException as exc:
                errors.append(exc)
        if not self._owner_closed:
            try:
                self._close_owner()
                self._owner_closed = True
            except BaseException as exc:
                errors.append(exc)
        if len(errors) > 1:
            raise errors[0] from errors[1]
        if errors:
            raise errors[0]


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
        response = None
        opening = False

        def iterate():
            nonlocal response, opening
            opening = True
            try:
                response = self._open(url, headers, payload)
            finally:
                opening = False
            yield from self._iter_sse_data(response)

        def close_response():
            if opening:
                raise RuntimeError("Provider response is still opening")
            if response is not None:
                response.close()

        return OwnedStream(iterate(), close_owner=close_response)

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
            raise ProviderHTTPError(exc.code, exc.headers, body) from exc
        except URLError as exc:
            raise RuntimeError(f"Provider connection error: {exc.reason}") from exc

    @staticmethod
    def _iter_sse(response) -> Iterator[dict]:
        try:
            yield from UrllibTransport._iter_sse_data(response)
        finally:
            response.close()

    @staticmethod
    def _iter_sse_data(response) -> Iterator[dict]:
        data_lines: list[str] = []
        total_bytes = 0
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
