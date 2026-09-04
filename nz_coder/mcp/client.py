"""Minimal newline-delimited JSON-RPC 2.0 client for MCP stdio servers."""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from nz_coder.foundation.execution_identity import (
    ExecutionIdentity,
    UnsafeExecutionIdentity,
    verify_execution_identity,
)
from nz_coder.foundation.json_safety import reject_nonstandard_json_constant
from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.runtime.process.platform_runtime import executable_argv, terminate_process_tree

_NOTIFICATION_CLOSED = object()


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("MCP timeout must be a positive finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("MCP timeout must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise ValueError(
            "MCP timeout must be a positive finite number no greater than 3600 seconds"
        )
    return timeout


class MCPError(RuntimeError):
    """Base MCP transport or protocol error."""


class MCPTimeoutError(MCPError):
    """Raised when an MCP request exceeds its configured deadline."""


class MCPRequestError(MCPError):
    """Raised for a JSON-RPC error response while preserving its code."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class MCPClient:
    """Own one stdio subprocess and correlate concurrent JSON-RPC requests."""

    protocol_version = "2024-11-05"

    def __init__(
        self,
        *,
        name: str,
        command: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str] | None = None,
        startup_timeout_seconds: float = 30.0,
        tool_timeout_seconds: float = 30.0,
        execution_identity: ExecutionIdentity | None = None,
    ):
        self.name = name
        self.command = tuple(command)
        self.cwd = Path(cwd)
        self.environment = dict(environment or {})
        self.startup_timeout_seconds = _validated_timeout(startup_timeout_seconds)
        self.tool_timeout_seconds = _validated_timeout(tool_timeout_seconds)
        self.execution_identity = execution_identity
        self.process: subprocess.Popen | None = None
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._transport_error: MCPError | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._notification_reader: threading.Thread | None = None
        self._notification_handlers: dict[
            str,
            Callable[[dict[str, Any]], None],
        ] = {}
        self._unhandled_notifications: dict[str, dict[str, Any]] = {}
        self._notification_pending: set[str] = set()
        self._notification_queue: queue.Queue = queue.Queue(maxsize=32)

    def start(self) -> dict[str, Any]:
        """Spawn, initialize, and send the MCP initialized notification."""
        env = build_sanitized_subprocess_env(
            overrides=self.environment,
            profile="strict-service",
        )
        with self._state_lock:
            if self.process is not None:
                return {
                    "serverInfo": dict(self.server_info),
                    "capabilities": dict(self.server_capabilities),
                }
            if self._closed:
                raise MCPError(f"MCP server '{self.name}' is closed")
            try:
                if self.execution_identity is not None:
                    verify_execution_identity(self.execution_identity)
                self.process = subprocess.Popen(
                    executable_argv(self.command),
                    cwd=self.cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=(os.name != "nt"),
                    creationflags=(
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        if os.name == "nt"
                        else 0
                    ),
                )
            except UnsafeExecutionIdentity as exc:
                self.process = None
                raise MCPError(
                    f"MCP server '{self.name}' execution identity changed"
                ) from exc
            except Exception as exc:
                self.process = None
                raise MCPError(
                    f"Failed to start MCP server '{self.name}': {exc}"
                ) from exc

        self._reader = threading.Thread(
            target=self._read_stdout,
            name=f"mcp-{self.name}-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name=f"mcp-{self.name}-stderr",
            daemon=True,
        )
        self._notification_reader = threading.Thread(
            target=self._dispatch_notifications,
            name=f"mcp-{self.name}-notifications",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
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
            self.server_info = dict(result.get("serverInfo") or {})
            self.server_capabilities = dict(result.get("capabilities") or {})
            self.notify("notifications/initialized", {})
            return result
        except Exception:
            self.close()
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        """List all server tools, following bounded cursor pagination."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):
            params = {"cursor": cursor} if cursor else {}
            result = self.request(
                "tools/list",
                params,
                timeout=self.startup_timeout_seconds,
            )
            if not isinstance(result, dict) or not isinstance(result.get("tools", []), list):
                raise MCPError(f"MCP server '{self.name}' returned an invalid tools/list result")
            tools.extend(item for item in result.get("tools", []) if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            cursor = next_cursor
        raise MCPError(f"MCP server '{self.name}' tools/list exceeded 100 pages")

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one MCP tool and return the protocol result object."""
        result = self.request(
            "tools/call",
            {"name": tool_name, "arguments": dict(arguments)},
            timeout=self.tool_timeout_seconds,
        )
        if not isinstance(result, dict):
            raise MCPError(f"MCP server '{self.name}' returned an invalid tools/call result")
        return result

    def list_prompts(self) -> list[dict[str, Any]]:
        """List prompt definitions when the server exposes that capability."""
        return self._list_paginated("prompts/list", "prompts")

    def get_prompt(
        self,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        result = self.request(
            "prompts/get",
            {"name": prompt_name, "arguments": dict(arguments or {})},
            timeout=self.tool_timeout_seconds,
        )
        if not isinstance(result, dict):
            raise MCPError(
                f"MCP server '{self.name}' returned an invalid prompts/get result"
            )
        return result

    def list_resources(self) -> list[dict[str, Any]]:
        """List resource definitions when the server exposes that capability."""
        return self._list_paginated("resources/list", "resources")

    def read_resource(self, uri: str) -> dict[str, Any]:
        result = self.request(
            "resources/read",
            {"uri": uri},
            timeout=self.tool_timeout_seconds,
        )
        if not isinstance(result, dict):
            raise MCPError(
                f"MCP server '{self.name}' returned an invalid resources/read result"
            )
        return result

    def set_notification_handler(
        self,
        method: str,
        handler: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Install or remove one asynchronous server-notification handler."""
        if not isinstance(method, str) or not method:
            raise ValueError("MCP notification method must be a non-empty string")
        replay: dict[str, Any] | None = None
        with self._state_lock:
            if handler is None:
                self._notification_handlers.pop(method, None)
            elif callable(handler):
                self._notification_handlers[method] = handler
                replay = self._unhandled_notifications.pop(method, None)
            else:
                raise ValueError("MCP notification handler must be callable")
        if replay is not None:
            self._queue_notification(method, replay)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> Any:
        """Send one JSON-RPC request and await its correlated response."""
        with self._state_lock:
            if self._closed:
                raise MCPError(f"MCP server '{self.name}' is closed")
            if self._transport_error is not None:
                raise self._transport_error
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            try:
                response = response_queue.get(timeout=max(0.001, float(timeout)))
            except queue.Empty as exc:
                raise MCPTimeoutError(
                    f"MCP server '{self.name}' request '{method}' timed out after {timeout:g}s"
                ) from exc
            if isinstance(response, Exception):
                raise response
            if not isinstance(response, dict):
                raise MCPError(f"MCP server '{self.name}' returned an invalid response")
            if "error" in response:
                error = response.get("error") or {}
                if isinstance(error, dict):
                    detail = error.get("message") or error
                    code = error.get("code")
                else:
                    detail = error
                    code = None
                raise MCPRequestError(
                    f"MCP server '{self.name}' request '{method}' failed: {detail}",
                    code=code if isinstance(code, int) else None,
                )
            return response.get("result")
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        """Close pipes and terminate the exact subprocess group created here."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        process = self.process
        if process is None:
            self._fail_pending(MCPError(f"MCP server '{self.name}' closed"))
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        if os.name != "nt":
            self._terminate_process(process, force=False)
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            # The server leader may already have exited while descendants remain
            # in the independent process group. Always make a final best-effort
            # group kill instead of keying cleanup only to leader.poll().
            self._terminate_process(process, force=True)
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        else:
            if process.poll() is None:
                self._terminate_process(process, force=False)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            # Sweep descendants even when the stdio leader exited first.
            self._terminate_process(process, force=True)
        self._fail_pending(MCPError(f"MCP server '{self.name}' closed"))
        try:
            self._notification_queue.put_nowait(_NOTIFICATION_CLOSED)
        except queue.Full:
            pass
        current = threading.current_thread()
        for thread in (self._reader, self._stderr_reader, self._notification_reader):
            if thread is not None and thread is not current:
                thread.join(timeout=0.5)

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if self._closed or process is None or process.stdin is None:
            raise MCPError(f"MCP server '{self.name}' is not running")
        try:
            payload = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise MCPError("MCP request must contain valid JSON values") from exc
        with self._write_lock:
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except Exception as exc:
                raise MCPError(f"Failed writing to MCP server '{self.name}': {exc}") from exc

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        error: MCPError | None = None
        try:
            for line in process.stdout:
                value = line.strip()
                if not value:
                    continue
                try:
                    message = json.loads(
                        value,
                        parse_constant=reject_nonstandard_json_constant,
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    error = MCPError(
                        f"MCP server '{self.name}' wrote invalid JSON to stdout: {exc}"
                    )
                    break
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if message_id is not None and ("result" in message or "error" in message):
                    with self._state_lock:
                        pending = self._pending.get(message_id)
                    if pending is not None:
                        try:
                            pending.put_nowait(message)
                        except queue.Full:
                            pass
                    continue
                if message_id is not None and message.get("method"):
                    self._send_server_request_error(message_id)
                    continue
                if message_id is None and isinstance(message.get("method"), str):
                    self._queue_notification(
                        message["method"],
                        message.get("params") if isinstance(message.get("params"), dict) else {},
                    )
        except Exception as exc:
            error = MCPError(f"MCP server '{self.name}' stdout failed: {exc}")
        if error is None and not self._closed:
            error = MCPError(f"MCP server '{self.name}' exited unexpectedly")
        if error is not None:
            with self._state_lock:
                self._transport_error = error
            self._fail_pending(error)

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                value = line.rstrip()
                if value:
                    self._stderr_tail.append(value[-2000:])
        except Exception:
            return

    def _send_server_request_error(self, request_id: Any) -> None:
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "Client-side MCP requests are not supported",
                    },
                }
            )
        except MCPError:
            pass

    def _list_paginated(self, method: str, field: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):
            result = self.request(
                method,
                {"cursor": cursor} if cursor else {},
                timeout=self.startup_timeout_seconds,
            )
            if not isinstance(result, dict) or not isinstance(result.get(field, []), list):
                raise MCPError(
                    f"MCP server '{self.name}' returned an invalid {method} result"
                )
            items.extend(item for item in result.get(field, []) if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return items
            cursor = next_cursor
        raise MCPError(f"MCP server '{self.name}' {method} exceeded 100 pages")

    def _queue_notification(self, method: str, params: dict[str, Any]) -> None:
        with self._state_lock:
            if method not in self._notification_handlers:
                # Servers may announce list changes during initial discovery,
                # before the runtime has installed its handlers. Keep one
                # coalesced edge per method so registration can replay it.
                if len(self._unhandled_notifications) < 32:
                    self._unhandled_notifications[method] = dict(params)
                return
            if method in self._notification_pending:
                return
            self._notification_pending.add(method)
        try:
            self._notification_queue.put_nowait((method, dict(params)))
        except queue.Full:
            with self._state_lock:
                self._notification_pending.discard(method)

    def _dispatch_notifications(self) -> None:
        while True:
            item = self._notification_queue.get()
            if item is _NOTIFICATION_CLOSED:
                return
            method, params = item
            with self._state_lock:
                self._notification_pending.discard(method)
                handler = self._notification_handlers.get(method)
                closed = self._closed
            if closed:
                return
            if handler is None:
                continue
            try:
                handler(params)
            except Exception:
                continue

    def _fail_pending(self, error: Exception) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
        for response_queue in pending:
            try:
                response_queue.put_nowait(error)
            except queue.Full:
                pass

    @staticmethod
    def _terminate_process(process: subprocess.Popen, *, force: bool) -> None:
        terminate_process_tree(process, force=force)
