"""MCP Streamable HTTP client with bounded responses and session cleanup."""
from __future__ import annotations

import json
import queue
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nz_coder.foundation.json_safety import reject_nonstandard_json_constant
from nz_coder.mcp.client import (
    MCPClient,
    MCPError,
    MCPRequestError,
    MCPTimeoutError,
    _NOTIFICATION_CLOSED,
)
from nz_coder.mcp.oauth import MCPAuthenticationRequired

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_SSE_LINE_BYTES = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects so credentials cannot be forwarded to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MCPHTTPClient(MCPClient):
    """Use MCP Streamable HTTP while preserving the stdio client's public API."""

    protocol_version = "2025-06-18"
    supported_protocol_versions = frozenset({"2025-03-26", "2025-06-18"})

    def __init__(
        self,
        *,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        startup_timeout_seconds: float = 30.0,
        tool_timeout_seconds: float = 30.0,
    ):
        super().__init__(
            name=name,
            command=(),
            cwd=".",
            startup_timeout_seconds=startup_timeout_seconds,
            tool_timeout_seconds=tool_timeout_seconds,
        )
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: str | None = None
        # MCP endpoints are explicit application configuration. Do not let
        # ambient HTTP_PROXY settings silently reroute credentials or sessions.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._request_lock = threading.Lock()
        self._event_response = None
        self._event_reader: threading.Thread | None = None
        self._transport_error_handler = None

    def start(self) -> dict[str, Any]:
        """Initialize the remote session and open its optional SSE event stream."""
        with self._state_lock:
            if self.server_info:
                return {
                    "serverInfo": dict(self.server_info),
                    "capabilities": dict(self.server_capabilities),
                }
            if self._closed:
                raise MCPError(f"MCP server '{self.name}' is closed")
        self._notification_reader = threading.Thread(
            target=self._dispatch_notifications,
            name=f"mcp-{self.name}-notifications",
            daemon=True,
        )
        self._notification_reader.start()
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "nz-coder", "version": "0.1"},
                },
                timeout=self.startup_timeout_seconds,
            )
            if not isinstance(result, dict):
                raise MCPError("MCP initialize result must be an object")
            negotiated = result.get("protocolVersion")
            if negotiated not in self.supported_protocol_versions:
                raise MCPError(
                    f"MCP server '{self.name}' negotiated unsupported protocol version"
                )
            self.protocol_version = negotiated
            self.server_info = dict(result.get("serverInfo") or {})
            self.server_capabilities = dict(result.get("capabilities") or {})
            self.notify("notifications/initialized", {})
            self._event_reader = threading.Thread(
                target=self._read_event_stream,
                name=f"mcp-{self.name}-events",
                daemon=True,
            )
            self._event_reader.start()
            return result
        except Exception:
            self.close()
            raise

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> Any:
        """POST one request and extract its correlated JSON-RPC response."""
        with self._state_lock:
            if self._closed:
                raise MCPError(f"MCP server '{self.name}' is closed")
            if self._transport_error is not None:
                raise self._transport_error
            request_id = self._next_id
            self._next_id += 1
        messages = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            },
            timeout=timeout,
        )
        response = None
        for message in messages:
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                response = message
            else:
                self._handle_message(message)
        if response is None:
            raise MCPError(
                f"MCP server '{self.name}' returned no response for '{method}'"
            )
        if "error" in response:
            error = response.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else error
            code = error.get("code") if isinstance(error, dict) else None
            raise MCPRequestError(
                f"MCP server '{self.name}' request '{method}' failed: {detail or error}",
                code=code if isinstance(code, int) else None,
            )
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """POST one JSON-RPC notification; a 202 response is valid."""
        messages = self._post(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            timeout=self.tool_timeout_seconds,
        )
        for message in messages:
            self._handle_message(message)

    def close(self) -> None:
        """Stop event delivery and best-effort DELETE the remote session."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            session_id = self.session_id
            event_response = self._event_response
        if event_response is not None:
            try:
                event_response.close()
            except Exception:
                pass
        if session_id:
            try:
                request = Request(
                    self.url,
                    headers=self._request_headers("application/json"),
                    method="DELETE",
                )
                response = self._opener.open(
                    request,
                    timeout=max(0.001, self.tool_timeout_seconds),
                )
                response.close()
            except Exception:
                pass
        try:
            self._notification_queue.put_nowait(_NOTIFICATION_CLOSED)
        except queue.Full:
            pass
        current = threading.current_thread()
        for thread in (self._event_reader, self._notification_reader):
            if thread is not None and thread is not current:
                thread.join(timeout=0.5)

    def _post(self, message: dict[str, Any], *, timeout: float) -> list[dict[str, Any]]:
        try:
            body = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MCPError("MCP request must contain valid JSON values") from exc
        request = Request(
            self.url,
            data=body,
            headers=self._request_headers("application/json, text/event-stream"),
            method="POST",
        )
        try:
            # Serialize POSTs so session establishment cannot race later calls.
            with self._request_lock:
                response = self._opener.open(request, timeout=max(0.001, float(timeout)))
                try:
                    self._capture_session(response)
                    status = getattr(response, "status", 200)
                    if status == 202:
                        return []
                    content_type = response.headers.get_content_type().lower()
                    if content_type == "application/json":
                        messages = self._decode_json_messages(
                            self._read_bounded(response)
                        )
                    elif content_type == "text/event-stream":
                        expected_id = message.get("id")
                        messages = self._read_sse_response(
                            response,
                            expected_id=expected_id,
                        )
                    else:
                        raise MCPError(
                            f"MCP server '{self.name}' returned unsupported "
                            f"content type '{content_type}'"
                        )
                finally:
                    response.close()
        except HTTPError as exc:
            if exc.code == 401:
                error = MCPAuthenticationRequired(
                    f"MCP server '{self.name}' requires authentication",
                    rejected_authorization=self.headers.get("Authorization", ""),
                )
                self._set_transport_error(error)
                raise error from exc
            raise MCPError(
                f"MCP server '{self.name}' HTTP request failed with status {exc.code}"
            ) from exc
        except TimeoutError as exc:
            raise MCPTimeoutError(
                f"MCP server '{self.name}' HTTP request timed out after {timeout:g}s"
            ) from exc
        except (OSError, URLError) as exc:
            raise MCPError(
                f"MCP server '{self.name}' HTTP transport failed: {type(exc).__name__}"
            ) from exc
        return messages

    def _read_event_stream(self) -> None:
        request = Request(
            self.url,
            headers=self._request_headers("text/event-stream"),
            method="GET",
        )
        try:
            response = self._opener.open(
                request,
                timeout=max(0.001, self.tool_timeout_seconds),
            )
            with self._state_lock:
                if self._closed:
                    response.close()
                    return
                self._event_response = response
            if response.headers.get_content_type().lower() != "text/event-stream":
                self._set_transport_error(
                    MCPError(
                        f"MCP server '{self.name}' GET returned unsupported content type"
                    )
                )
                return
            data_lines: list[bytes] = []
            event_size = 0
            while not self._closed:
                line = response.readline(_MAX_SSE_LINE_BYTES + 1)
                if len(line) > _MAX_SSE_LINE_BYTES:
                    raise MCPError(f"MCP server '{self.name}' SSE line exceeded limit")
                if not line:
                    break
                stripped = line.rstrip(b"\r\n")
                if not stripped:
                    self._dispatch_sse_data(data_lines)
                    data_lines = []
                    event_size = 0
                elif stripped.startswith(b"data:"):
                    data = stripped[5:].lstrip()
                    event_size += len(data) + 1
                    if event_size > _MAX_RESPONSE_BYTES:
                        raise MCPError(
                            f"MCP server '{self.name}' SSE event exceeded 10 MiB"
                        )
                    data_lines.append(data)
            self._dispatch_sse_data(data_lines)
        except HTTPError as exc:
            # GET is optional in Streamable HTTP; 404/405 means POST-only mode.
            if exc.code not in {404, 405} and not self._closed:
                error = (
                    MCPAuthenticationRequired(
                        f"MCP server '{self.name}' requires authentication",
                        rejected_authorization=self.headers.get("Authorization", ""),
                    )
                    if exc.code == 401
                    else MCPError(
                        f"MCP server '{self.name}' GET failed with status {exc.code}"
                    )
                )
                self._set_transport_error(
                    error
                )
        except Exception as exc:
            if not self._closed:
                self._set_transport_error(
                    MCPError(
                        f"MCP server '{self.name}' GET event stream failed: "
                        f"{type(exc).__name__}"
                    )
                )
        finally:
            with self._state_lock:
                response = self._event_response
                self._event_response = None
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _dispatch_sse_data(self, lines: list[bytes]) -> None:
        if not lines:
            return
        payload = b"\n".join(lines)
        if len(payload) > _MAX_RESPONSE_BYTES:
            return
        messages = self._decode_json_messages(payload)
        for message in messages:
            self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        if message.get("id") is None and isinstance(message.get("method"), str):
            self._queue_notification(
                message["method"],
                message.get("params") if isinstance(message.get("params"), dict) else {},
            )

    def _set_transport_error(self, error: MCPError) -> None:
        with self._state_lock:
            if not self._closed:
                self._transport_error = error
                handler = self._transport_error_handler
            else:
                handler = None
        if handler is not None:
            try:
                handler(error)
            except Exception:
                pass

    def set_transport_error_handler(self, handler) -> None:
        """Install a lifecycle callback and replay an already observed failure."""
        if handler is not None and not callable(handler):
            raise ValueError("MCP transport error handler must be callable")
        with self._state_lock:
            self._transport_error_handler = handler
            error = self._transport_error
        if handler is not None and error is not None:
            handler(error)

    def _request_headers(self, accept: str) -> dict[str, str]:
        headers = dict(self.headers)
        headers.update(
            {
                "Accept": accept,
                "Content-Type": "application/json",
                "MCP-Protocol-Version": self.protocol_version,
            }
        )
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _capture_session(self, response) -> None:
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id is not None:
            if (
                not session_id
                or len(session_id) > 1024
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in session_id)
            ):
                raise MCPError(f"MCP server '{self.name}' returned an invalid session ID")
            if self.session_id is not None and self.session_id != session_id:
                raise MCPError(f"MCP server '{self.name}' changed its session ID")
            self.session_id = session_id

    def _read_bounded(self, response) -> bytes:
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise MCPError(f"MCP server '{self.name}' response exceeded 10 MiB")
        return payload

    def _read_sse_response(
        self,
        response,
        *,
        expected_id: Any,
    ) -> list[dict[str, Any]]:
        """Read SSE incrementally and stop once the request response arrives."""
        messages: list[dict[str, Any]] = []
        data_lines: list[bytes] = []
        total_size = 0
        event_size = 0
        while True:
            line = response.readline(_MAX_SSE_LINE_BYTES + 1)
            if len(line) > _MAX_SSE_LINE_BYTES:
                raise MCPError(f"MCP server '{self.name}' SSE line exceeded limit")
            if not line:
                if data_lines:
                    messages.extend(self._decode_json_messages(b"\n".join(data_lines)))
                return messages
            total_size += len(line)
            if total_size > _MAX_RESPONSE_BYTES:
                raise MCPError(f"MCP server '{self.name}' response exceeded 10 MiB")
            stripped = line.rstrip(b"\r\n")
            if not stripped:
                if data_lines:
                    event_messages = self._decode_json_messages(b"\n".join(data_lines))
                    messages.extend(event_messages)
                    if expected_id is not None and any(
                        item.get("id") == expected_id
                        and ("result" in item or "error" in item)
                        for item in event_messages
                    ):
                        return messages
                data_lines = []
                event_size = 0
            elif stripped.startswith(b"data:"):
                data = stripped[5:].lstrip()
                event_size += len(data) + 1
                if event_size > _MAX_RESPONSE_BYTES:
                    raise MCPError(
                        f"MCP server '{self.name}' SSE event exceeded 10 MiB"
                    )
                data_lines.append(data)

    def _decode_json_messages(self, payload: bytes) -> list[dict[str, Any]]:
        try:
            value = json.loads(
                payload.decode("utf-8"),
                parse_constant=reject_nonstandard_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise MCPError(f"MCP server '{self.name}' returned invalid JSON") from exc
        values = value if isinstance(value, list) else [value]
        if not all(isinstance(item, dict) for item in values):
            raise MCPError(f"MCP server '{self.name}' returned an invalid JSON-RPC body")
        return values
