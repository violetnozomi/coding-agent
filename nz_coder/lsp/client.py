"""Minimal synchronous JSON-RPC client for stdio language servers."""
from __future__ import annotations

import json
import math
import nturl2path
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.runtime.process.platform_runtime import executable_argv, terminate_process_tree


_STDERR_DRAIN_TIMEOUT_SECONDS = 0.2


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("LSP timeout must be a positive finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("LSP timeout must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
        raise ValueError(
            "LSP timeout must be a positive finite number no greater than 600 seconds"
        )
    return timeout


class LSPError(RuntimeError):
    """Base error raised by the LSP runtime."""


class LSPTimeoutError(LSPError):
    """Raised when a language server does not answer in time."""


class LSPResponseError(LSPError):
    """Raised for a JSON-RPC error response."""

    def __init__(self, error: dict):
        self.error = error
        super().__init__(str(error.get("message") or error))


def path_to_uri(path: Path) -> str:
    """Convert an absolute path to a standards-compliant file URI."""
    return path.resolve().as_uri()


def uri_to_path(uri: str, *, os_name: str | None = None) -> Path | None:
    """Convert a file URI to a local path."""
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    selected_os = os.name if os_name is None else os_name
    value = (
        nturl2path.url2pathname(parsed.path)
        if selected_os == "nt"
        else url2pathname(parsed.path)
    )
    return Path(value)


class LSPClient:
    """One persistent LSP subprocess and its JSON-RPC connection."""

    def __init__(
        self,
        *,
        server_id: str,
        command: tuple[str, ...],
        root: Path,
        language_id: str,
        analysis_paths: tuple[Path, ...] = (),
        initialize_timeout: float | None = None,
        request_timeout: float | None = None,
        diagnostic_wait: float | None = None,
    ):
        self.server_id = server_id
        self.command = tuple(command)
        self.root = root.resolve()
        self.language_id = language_id
        self.analysis_paths = tuple(Path(path).resolve() for path in analysis_paths)
        self.initialize_timeout = _validated_timeout(
            initialize_timeout
            if initialize_timeout is not None
            else current_run_settings().lsp_initialize_timeout
        )
        self.request_timeout = _validated_timeout(
            request_timeout
            if request_timeout is not None
            else current_run_settings().lsp_request_timeout
        )
        self.diagnostic_wait = max(
            0.0,
            float(
                current_run_settings().lsp_diagnostic_wait
                if diagnostic_wait is None else diagnostic_wait
            ),
        )
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._next_id = 1
        self._closed = False
        self._documents: dict[Path, tuple[int, str]] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diagnostic_events: dict[str, threading.Event] = {}
        self._stderr = deque(maxlen=20)
        try:
            self.process = subprocess.Popen(
                executable_argv(self.command),
                cwd=str(self.root),
                env=build_sanitized_subprocess_env(profile="strict-service"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=(os.name != "nt"),
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
        except OSError as exc:
            raise LSPError(f"Failed to start {server_id}: {exc}") from exc
        if not self.process.stdin or not self.process.stdout or not self.process.stderr:
            self.close(force=True)
            raise LSPError(f"Language server {server_id} has no stdio pipes")

        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"nzcoder-lsp-{server_id}",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name=f"nzcoder-lsp-{server_id}-stderr",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
        try:
            self.capabilities = self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": path_to_uri(self.root),
                    "workspaceFolders": [{
                        "name": self.root.name or "workspace",
                        "uri": path_to_uri(self.root),
                    }],
                    "capabilities": {
                        "workspace": {
                            "configuration": True,
                            "workspaceFolders": True,
                        },
                        "textDocument": {
                            "synchronization": {
                                "didOpen": True,
                                "didChange": True,
                            },
                            "hover": {"contentFormat": ["markdown", "plaintext"]},
                            "definition": {"linkSupport": True},
                            "references": {},
                            "documentSymbol": {
                                "hierarchicalDocumentSymbolSupport": True,
                            },
                            "callHierarchy": {},
                            "publishDiagnostics": {
                                "relatedInformation": True,
                            },
                            "diagnostic": {
                                "dynamicRegistration": True,
                                "relatedDocumentSupport": True,
                            },
                        },
                        "general": {"positionEncodings": ["utf-16"]},
                    },
                    "clientInfo": {"name": "nz-coder", "version": "0.1.0"},
                },
                timeout=self.initialize_timeout,
            ) or {}
            self.notify("initialized", {})
            if self.language_id == "python" and self.analysis_paths:
                self.notify("workspace/didChangeConfiguration", {
                    "settings": {
                        "python": {
                            "analysis": {
                                "extraPaths": [str(path) for path in self.analysis_paths],
                            },
                        },
                    },
                })
        except Exception as exc:
            self.close(force=True)
            self._stderr_reader.join(timeout=_STDERR_DRAIN_TIMEOUT_SECONDS)
            detail = self.stderr_tail
            if detail and detail not in str(exc):
                raise LSPError(f"{exc}: {detail}") from exc
            raise

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send one JSON-RPC request and wait for its response."""
        if self._closed:
            raise LSPError(f"Language server {self.server_id} is closed")
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        })
        try:
            response = response_queue.get(
                timeout=self.request_timeout if timeout is None else timeout
            )
        except queue.Empty as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise LSPTimeoutError(
                f"{self.server_id} timed out handling {method}"
            ) from exc
        if isinstance(response, Exception):
            raise response
        if response.get("error") is not None:
            raise LSPResponseError(response["error"])
        return response.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send one JSON-RPC notification."""
        if self._closed:
            raise LSPError(f"Language server {self.server_id} is closed")
        self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    def open_document(self, path: Path) -> int:
        """Open or refresh a document using full-content synchronization."""
        target = path.resolve()
        text = target.read_text(encoding="utf-8", errors="replace")
        uri = path_to_uri(target)
        existing = self._documents.get(target)
        if existing is None:
            version = 0
            self.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": self.language_id,
                    "version": version,
                    "text": text,
                },
            })
        else:
            version = existing[0] + 1
            self.notify("workspace/didChangeWatchedFiles", {
                "changes": [{"uri": uri, "type": 2}],
            })
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
        self._documents[target] = (version, text)
        return version

    def diagnostics(self, path: Path) -> list[dict[str, Any]]:
        """Return pull or published diagnostics for one document."""
        target = path.resolve()
        uri = path_to_uri(target)
        event = self._diagnostic_events.setdefault(uri, threading.Event())
        event.clear()
        self.open_document(target)
        try:
            report = self.request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": uri}},
                timeout=self.diagnostic_wait,
            )
            if isinstance(report, dict) and isinstance(report.get("items"), list):
                self._diagnostics[uri] = report["items"]
                return list(report["items"])
        except (LSPResponseError, LSPTimeoutError):
            pass
        event.wait(timeout=self.diagnostic_wait)
        return list(self._diagnostics.get(uri, []))

    def close_document(self, path: Path) -> None:
        """Forget a deleted document and notify the server about its removal."""
        target = path.resolve()
        uri = path_to_uri(target)
        if target in self._documents:
            self.notify("textDocument/didClose", {
                "textDocument": {"uri": uri},
            })
            self._documents.pop(target, None)
        self.notify("workspace/didChangeWatchedFiles", {
            "changes": [{"uri": uri, "type": 3}],
        })
        self._diagnostics.pop(uri, None)
        self._diagnostic_events.pop(uri, None)

    def close(self, force: bool = False) -> None:
        """Shut down the language server and release its process."""
        if self._closed:
            return
        if not force and self.process.poll() is None:
            try:
                self.request("shutdown", {}, timeout=2)
                self.notify("exit", {})
            except LSPError:
                force = True
        self._closed = True
        if self.process.poll() is None:
            terminate_process_tree(self.process, force=force)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate_process_tree(self.process, force=True)
                self.process.wait(timeout=2)
        elif os.name == "nt":
            terminate_process_tree(self.process, force=True)
        self._fail_pending(LSPError(f"Language server {self.server_id} closed"))

    def _send(self, payload: dict) -> None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        framed = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data
        try:
            with self._write_lock:
                if not self.process.stdin:
                    raise BrokenPipeError
                self.process.stdin.write(framed)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise LSPError(
                f"Language server {self.server_id} connection closed"
            ) from exc

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                message = self._read_message()
                if message is None:
                    break
                self._handle_message(message)
        except Exception as exc:
            self._fail_pending(LSPError(
                f"Language server {self.server_id} protocol failed: {exc}"
            ))
        finally:
            if not self._closed:
                detail = self.stderr_tail
                suffix = f": {detail}" if detail else ""
                self._fail_pending(LSPError(
                    f"Language server {self.server_id} exited{suffix}"
                ))

    def _read_message(self) -> dict | None:
        if not self.process.stdout:
            return None
        headers: dict[str, str] = {}
        while True:
            line = self.process.stdout.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            decoded = line.decode("ascii", errors="replace").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            raise LSPError("Missing Content-Length header")
        body = self.process.stdout.read(length)
        if len(body) != length:
            raise LSPError("Truncated JSON-RPC message")
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise LSPError("JSON-RPC message must be an object")
        return value

    def _handle_message(self, message: dict) -> None:
        response_id = message.get("id")
        if response_id is not None and "method" not in message:
            with self._state_lock:
                response_queue = self._pending.pop(response_id, None)
            if response_queue is not None:
                response_queue.put(message)
            return

        method = message.get("method")
        params = message.get("params") or {}
        if method == "textDocument/publishDiagnostics":
            uri = str(params.get("uri") or "")
            diagnostics = params.get("diagnostics")
            if uri and isinstance(diagnostics, list):
                self._diagnostics[uri] = diagnostics
                self._diagnostic_events.setdefault(uri, threading.Event()).set()
            return
        if response_id is not None and method:
            result = self._server_request_result(str(method), params)
            self._send({
                "jsonrpc": "2.0",
                "id": response_id,
                "result": result,
            })

    def _server_request_result(self, method: str, params: dict) -> Any:
        if method == "workspace/configuration":
            return [None for _ in params.get("items", [])]
        if method == "workspace/workspaceFolders":
            return [{
                "name": self.root.name or "workspace",
                "uri": path_to_uri(self.root),
            }]
        return None

    def _read_stderr(self) -> None:
        if not self.process.stderr:
            return
        for raw in iter(self.process.stderr.readline, b""):
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr.append(text[:1000])

    def _fail_pending(self, exc: Exception) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for response_queue in pending:
            try:
                response_queue.put_nowait(exc)
            except queue.Full:
                pass
