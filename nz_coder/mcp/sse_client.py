"""Legacy MCP HTTP+SSE transport used after Streamable HTTP fallback."""
from __future__ import annotations

import json
import queue
import socket
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nz_coder.mcp.client import (
    MCPClient,
    MCPError,
    _NOTIFICATION_CLOSED,
)
from nz_coder.mcp.oauth import MCPAuthenticationRequired


_MAX_LINE_BYTES = 1024 * 1024
_MAX_EVENT_BYTES = 10 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MCPLegacySSEClient(MCPClient):
    """Correlate JSON-RPC responses delivered on a legacy SSE GET stream."""

    protocol_version = "2024-11-05"

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
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._endpoint = ""
        self._endpoint_ready = threading.Event()
        self._event_response = None
        self._event_reader: threading.Thread | None = None
        self._transport_error_handler = None

    def start(self) -> dict[str, Any]:
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
            name=f"mcp-{self.name}-legacy-notifications",
            daemon=True,
        )
        self._event_reader = threading.Thread(
            target=self._read_stream,
            name=f"mcp-{self.name}-legacy-sse",
            daemon=True,
        )
        self._notification_reader.start()
        self._event_reader.start()
        if not self._endpoint_ready.wait(self.startup_timeout_seconds):
            self.close()
            raise MCPError(f"MCP server '{self.name}' legacy SSE endpoint timed out")
        with self._state_lock:
            error = self._transport_error
        if error is not None:
            self.close()
            raise error
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
            self.server_info = dict(result.get("serverInfo") or {})
            self.server_capabilities = dict(result.get("capabilities") or {})
            self.notify("notifications/initialized", {})
            return result
        except Exception:
            self.close()
            raise

    def _send(self, message: dict[str, Any]) -> None:
        if not self._endpoint_ready.wait(self.startup_timeout_seconds):
            raise MCPError(f"MCP server '{self.name}' legacy endpoint is unavailable")
        with self._state_lock:
            if self._closed:
                raise MCPError(f"MCP server '{self.name}' is closed")
            endpoint = self._endpoint
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            headers={**self.headers, "Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._write_lock:
                response = self._opener.open(
                    request,
                    timeout=max(0.001, self.tool_timeout_seconds),
                )
                try:
                    if getattr(response, "status", 200) not in {200, 202, 204}:
                        raise MCPError(f"MCP server '{self.name}' legacy POST failed")
                    if response.read(_MAX_EVENT_BYTES + 1):
                        raise MCPError(
                            f"MCP server '{self.name}' legacy POST returned an unexpected body"
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
                f"MCP server '{self.name}' legacy POST failed with status {exc.code}"
            ) from exc
        except (OSError, URLError) as exc:
            raise MCPError(
                f"MCP server '{self.name}' legacy POST transport failed: {type(exc).__name__}"
            ) from exc

    def _read_stream(self) -> None:
        request = Request(
            self.url,
            headers={**self.headers, "Accept": "text/event-stream"},
            method="GET",
        )
        response = None
        try:
            response = self._opener.open(request, timeout=24 * 60 * 60)
            if response.headers.get_content_type().lower() != "text/event-stream":
                raise MCPError(
                    f"MCP server '{self.name}' legacy GET returned unsupported content type"
                )
            with self._state_lock:
                if self._closed:
                    return
                self._event_response = response
            event_name = "message"
            data_lines: list[bytes] = []
            event_size = 0
            while not self._closed:
                line = response.readline(_MAX_LINE_BYTES + 1)
                if len(line) > _MAX_LINE_BYTES:
                    raise MCPError(f"MCP server '{self.name}' legacy SSE line exceeded limit")
                if not line:
                    break
                stripped = line.rstrip(b"\r\n")
                if not stripped:
                    self._dispatch_event(event_name, data_lines)
                    event_name = "message"
                    data_lines = []
                    event_size = 0
                elif stripped.startswith(b"event:"):
                    event_name = stripped[6:].strip().decode("utf-8", errors="replace")
                elif stripped.startswith(b"data:"):
                    data = stripped[5:].lstrip()
                    event_size += len(data) + 1
                    if event_size > _MAX_EVENT_BYTES:
                        raise MCPError(
                            f"MCP server '{self.name}' legacy SSE event exceeded 10 MiB"
                        )
                    data_lines.append(data)
            self._dispatch_event(event_name, data_lines)
            if not self._closed:
                raise MCPError(f"MCP server '{self.name}' legacy SSE stream closed")
        except HTTPError as exc:
            error = (
                MCPAuthenticationRequired(
                    f"MCP server '{self.name}' requires authentication",
                    rejected_authorization=self.headers.get("Authorization", ""),
                )
                if exc.code == 401
                else MCPError(
                    f"MCP server '{self.name}' legacy GET failed with status {exc.code}"
                )
            )
            self._set_transport_error(error)
        except Exception as exc:
            if not self._closed:
                error = exc if isinstance(exc, MCPError) else MCPError(
                    f"MCP server '{self.name}' legacy SSE failed: {type(exc).__name__}"
                )
                self._set_transport_error(error)
        finally:
            self._endpoint_ready.set()
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            with self._state_lock:
                self._event_response = None

    def _dispatch_event(self, event_name: str, data_lines: list[bytes]) -> None:
        if not data_lines:
            return
        payload = b"\n".join(data_lines)
        if event_name == "endpoint":
            try:
                value = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MCPError("Legacy MCP endpoint is not UTF-8") from exc
            self._set_endpoint(value)
            return
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPError(f"MCP server '{self.name}' legacy SSE returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise MCPError(f"MCP server '{self.name}' legacy SSE returned invalid JSON-RPC")
        message_id = message.get("id")
        if message_id is not None and ("result" in message or "error" in message):
            with self._state_lock:
                pending = self._pending.get(message_id)
            if pending is not None:
                try:
                    pending.put_nowait(message)
                except queue.Full:
                    pass
            return
        if message_id is not None and message.get("method"):
            self._send_server_request_error(message_id)
        elif message_id is None and isinstance(message.get("method"), str):
            self._queue_notification(
                message["method"],
                message.get("params") if isinstance(message.get("params"), dict) else {},
            )

    def _set_endpoint(self, value: str) -> None:
        endpoint = urljoin(self.url, value.strip())
        base = urlsplit(self.url)
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != base.scheme
            or parsed.hostname != base.hostname
            or parsed.port != base.port
            or parsed.username is not None
            or parsed.fragment
        ):
            raise MCPError(f"MCP server '{self.name}' legacy endpoint changed origin")
        with self._state_lock:
            if self._endpoint and self._endpoint != endpoint:
                raise MCPError(f"MCP server '{self.name}' changed its legacy endpoint")
            self._endpoint = endpoint
        self._endpoint_ready.set()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            response = self._event_response
        self._endpoint_ready.set()
        if response is not None:
            try:
                sock = response.fp.raw._sock
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                response.close()
            except Exception:
                pass
        self._fail_pending(MCPError(f"MCP server '{self.name}' closed"))
        try:
            self._notification_queue.put_nowait(_NOTIFICATION_CLOSED)
        except queue.Full:
            pass
        current = threading.current_thread()
        for thread in (self._event_reader, self._notification_reader):
            if thread is not None and thread is not current:
                thread.join(timeout=0.5)

    def _set_transport_error(self, error: MCPError) -> None:
        with self._state_lock:
            if self._closed:
                handler = None
            else:
                self._transport_error = error
                handler = self._transport_error_handler
        self._endpoint_ready.set()
        self._fail_pending(error)
        if handler is not None:
            try:
                handler(error)
            except Exception:
                pass

    def set_transport_error_handler(self, handler) -> None:
        if handler is not None and not callable(handler):
            raise ValueError("MCP transport error handler must be callable")
        with self._state_lock:
            self._transport_error_handler = handler
            error = self._transport_error
        if handler is not None and error is not None:
            handler(error)
