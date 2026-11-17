"""Minimal synchronous JSON-RPC client for stdio language servers."""

from __future__ import annotations

import json
import fnmatch
import math
import nturl2path
import os
import queue
import re
import select
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from nz_coder.foundation.execution_identity import (
    ExecutionIdentity,
    UnsafeExecutionIdentity,
    verify_execution_identity,
    resolve_execution_identity,
)
from nz_coder.foundation.workspace_file_access import WorkspaceFileIdentity
from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.runtime.process.platform_runtime import (
    terminate_process_tree,
)


from .types import DiagnosticReport, LspOperation

_STDERR_DRAIN_TIMEOUT_SECONDS = 0.2
_MAX_HEADER_BYTES = 64 * 1024
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_MAX_DIAGNOSTICS = 2_000
_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_MAX_STDERR_LINE_BYTES = 8 * 1024


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


class LSPCancelledError(LSPError):
    """本次调用取消，服务可以继续由其他调用共享。"""


class LSPResponseError(LSPError):
    """Raised for a JSON-RPC error response."""

    def __init__(self, error: dict):
        self.error = error
        super().__init__("Language server returned an error response")


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
        execution_identity: ExecutionIdentity | None = None,
        environment: tuple[tuple[str, str], ...] | None = None,
        initialization_json: str = "{}",
        settings_json: str = "{}",
        analysis_environment: str = "unknown",
        analysis_profile=None,
        analysis_workspace: Path | None = None,
    ):
        self.server_id = server_id
        self.command = tuple(command)
        self.root = root.resolve()
        self.language_id = language_id
        self.analysis_paths = tuple(Path(path).resolve() for path in analysis_paths)
        self.execution_identity = execution_identity
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
                if diagnostic_wait is None
                else diagnostic_wait
            ),
        )
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._changed = threading.Condition(self._state_lock)
        self._spawn_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._next_id = 1
        self._closed = False
        self._close_lock = threading.Lock()
        self._close_complete = False
        self._document_lock = threading.Lock()
        self._next_document_version = 0
        self._documents: dict[Path, tuple[int, str, WorkspaceFileIdentity]] = {}
        self._diagnostics: dict[str, dict] = {}
        self._receive_seq = 0
        self._registrations: dict[str, dict] = {}
        self._registration_generation = 0
        self._active_sends: dict[threading.Thread, threading.Event] = {}
        self._stderr = deque(maxlen=20)
        self.process = None
        self._reader = None
        self._stderr_reader = None
        self.capabilities = {}
        if environment is None:
            cache = prepare_user_storage(self.root).workspace_cache
            captured = build_sanitized_subprocess_env(
                profile="strict-service",
                overrides={key: str(cache) for key in ("TMPDIR", "TMP", "TEMP")},
            )
            environment = tuple(sorted(captured.items()))
        self.environment = tuple(environment)
        self.initialization_json = initialization_json
        self.settings_json = settings_json
        self.analysis_environment = analysis_environment
        self.analysis_profile = analysis_profile
        self.analysis_workspace = analysis_workspace

    @property
    def server_capabilities(self) -> dict:
        return self.capabilities.get("capabilities", {})

    @property
    def position_encoding(self) -> str:
        return self.server_capabilities.get("positionEncoding", "utf-16")

    def _operation(self, operation=None, timeout=None) -> LspOperation:
        if operation is not None:
            return (
                LspOperation(
                    operation.cancellation,
                    min(operation.deadline, time.monotonic() + timeout),
                )
                if timeout is not None
                else operation
            )
        return LspOperation(
            None,
            time.monotonic() + (self.request_timeout if timeout is None else timeout),
        )

    @staticmethod
    def _check(operation: LspOperation) -> None:
        if operation.cancelled():
            raise LSPCancelledError("LSP operation cancelled")
        if operation.remaining() <= 0:
            raise LSPTimeoutError("LSP operation deadline exceeded")

    def _acquire(self, lock, operation: LspOperation) -> None:
        while True:
            self._check(operation)
            if lock.acquire(timeout=min(0.02, operation.remaining())):
                self._check_after_acquire(lock, operation)
                return

    def _check_after_acquire(self, lock, operation):
        try:
            self._check(operation)
        except LSPError:
            lock.release()
            raise

    def start(self, operation: LspOperation | None = None) -> None:
        """manager 先登记 owner/lease，随后才允许真实启动。"""
        operation = self._operation(operation, self.initialize_timeout)
        self._acquire(self._start_lock, operation)
        try:
            if self.process is not None:
                if self._closed:
                    raise LSPError("Language server is closed")
                return
            self._start(operation)
        finally:
            self._start_lock.release()

    def _start(self, operation: LspOperation) -> None:
        try:
            self._check(operation)
            if self.execution_identity is None:
                self.execution_identity = resolve_execution_identity(
                    self.command, cwd=self.root, workspace=self.root, config_source="direct",
                    environment_profile="strict-service", environment=dict(self.environment),
                )
            identity = verify_execution_identity(self.execution_identity)
            self._acquire(self._spawn_lock, operation)
            try:
                self._check(operation)
                if self._closed:
                    raise LSPError("Language server is closed")
                self.process = subprocess.Popen(
                    identity.launch_command,
                    cwd=str(identity.cwd),
                    env=dict(identity.environment),
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
                if self._closed:
                    terminate_process_tree(self.process, force=True)
                    raise LSPError("Language server closed during startup")
            finally:
                self._spawn_lock.release()
        except UnsafeExecutionIdentity as exc:
            raise LSPError(
                f"Language server {self.server_id} execution identity changed"
            ) from exc
        except OSError as exc:
            raise LSPError(f"Failed to start language server {self.server_id}") from exc
        if not self.process.stdin or not self.process.stdout or not self.process.stderr:
            self.close(force=True)
            raise LSPError(f"Language server {self.server_id} has no stdio pipes")

        self._acquire(self._spawn_lock, operation)
        try:
            if self._closed:
                raise LSPError("Language server closed during startup")
            self._reader = threading.Thread(
                target=self._read_loop,
                name=f"nzcoder-lsp-{self.server_id}",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                name=f"nzcoder-lsp-{self.server_id}-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
        finally:
            self._spawn_lock.release()
        try:
            self.capabilities = (
                self.request(
                    "initialize",
                    {
                        "processId": os.getpid(),
                        "rootUri": path_to_uri(self.root),
                        "workspaceFolders": [
                            {
                                "name": self.root.name or "workspace",
                                "uri": path_to_uri(self.root),
                            }
                        ],
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
                                    "versionSupport": True,
                                },
                                "diagnostic": {
                                    "dynamicRegistration": True,
                                },
                            },
                            "general": {"positionEncodings": ["utf-16"]},
                        },
                        "initializationOptions": json.loads(self.initialization_json),
                        "clientInfo": {"name": "nz-coder", "version": "0.1.0"},
                    },
                    timeout=self.initialize_timeout,
                    operation=operation,
                )
                or {}
            )
            self.notify("initialized", {}, operation=operation)
            settings = json.loads(self.settings_json)
            if settings:
                self.notify(
                    "workspace/didChangeConfiguration",
                    {"settings": settings},
                    operation=operation,
                )
        except Exception as exc:
            self.close(force=True)
            raise LSPError("Language server failed to initialize") from exc

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
        operation: LspOperation | None = None,
    ) -> Any:
        operation = self._operation(
            operation, self.request_timeout if timeout is None else timeout
        )
        self._check(operation)
        if self._closed:
            raise LSPError("Language server is closed")
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        sent = False
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                },
                operation=operation,
            )
            sent = True
            while True:
                self._check(operation)
                try:
                    response = response_queue.get(
                        timeout=min(0.02, operation.remaining())
                    )
                    break
                except queue.Empty:
                    continue
            if isinstance(response, Exception):
                raise response
            if response.get("error") is not None:
                raise LSPResponseError(response["error"])
            return response.get("result")
        except (LSPCancelledError, LSPTimeoutError):
            if sent and not self._closed:
                try:
                    self.notify(
                        "$/cancelRequest",
                        {"id": request_id},
                        operation=LspOperation(None, time.monotonic() + 0.05),
                    )
                except LSPError:
                    pass
            raise
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def notify(
        self,
        method: str,
        params: dict | None = None,
        *,
        operation: LspOperation | None = None,
    ) -> None:
        if self._closed:
            raise LSPError("Language server is closed")
        self._send(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            operation=self._operation(operation),
        )

    def _sync_options(self) -> tuple[bool, int]:
        sync = self.server_capabilities.get("textDocumentSync")
        if isinstance(sync, int) and not isinstance(sync, bool):
            return sync in (1, 2), sync if sync in (1, 2) else 0
        if isinstance(sync, dict):
            return bool(sync.get("openClose", False)), sync.get("change", 0)
        return False, 0

    def _target(self, path: Path) -> Path:
        target = path if path.is_absolute() else self.root / path
        try:
            relative = target.relative_to(self.root)
        except ValueError as exc:
            raise LSPError("LSP document is outside the workspace") from exc
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise LSPError("LSP document path is invalid")
        return self.root / relative

    def _open_locked(self, target, text, source_identity, operation):
        opening, change = self._sync_options()
        if not opening and not change:
            raise LSPError("sync_unavailable")
        with self._state_lock:
            existing = self._documents.get(target)
            if existing and existing[1:] == (text, source_identity):
                return existing[0]
            if existing is None and not opening:
                raise LSPError("sync_unavailable")
            if existing is not None and not change:
                raise LSPError("sync_unavailable")
            version = self._next_document_version
            self._next_document_version += 1
            self._documents[target] = (version, text, source_identity)
            self._diagnostics.pop(target.as_uri(), None)
        uri = target.as_uri()
        try:
            if existing is None:
                language = {
                    ".tsx": "typescriptreact",
                    ".jsx": "javascriptreact",
                    ".js": "javascript",
                    ".mjs": "javascript",
                    ".cjs": "javascript",
                    ".ts": "typescript",
                }.get(target.suffix, self.language_id)
                self.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": language,
                            "version": version,
                            "text": text,
                        }
                    },
                    operation=operation,
                )
            else:
                changes = {"text": text}
                if change == 2:
                    old = existing[1]
                    # LSP 的行尾不包含 CRLF；末尾换行产生新的空行。
                    lines = re.split(r"\r\n|\r|\n", old)
                    last = lines[-1].removesuffix("\r")
                    units = (
                        len(last.encode("utf-16-le")) // 2
                        if self.position_encoding == "utf-16"
                        else (
                            len(last.encode("utf-8"))
                            if self.position_encoding == "utf-8"
                            else len(last)
                        )
                    )
                    changes["range"] = {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": len(lines) - 1, "character": units},
                    }
                self.notify(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": version},
                        "contentChanges": [changes],
                    },
                    operation=operation,
                )
        except Exception:
            # 未发送完整同步帧不能留下可供下一次查询复用的“已同步”记录。
            with self._changed:
                if existing is None:
                    self._documents.pop(target, None)
                else:
                    self._documents[target] = existing
                self._diagnostics.pop(uri, None)
                self._changed.notify_all()
            raise
        return version

    def open_document(
        self,
        path: Path,
        text: str | None = None,
        source_identity: WorkspaceFileIdentity | None = None,
        *,
        reader=None,
        operation: LspOperation | None = None,
    ) -> int:
        operation = self._operation(operation)
        target = self._target(path)
        self._acquire(self._document_lock, operation)
        try:
            if reader is not None:
                text, source_identity = reader()
            if text is None or source_identity is None:
                raise ValueError("LSP document requires source content and identity")
            return self._open_locked(target, text, source_identity, operation)
        finally:
            self._document_lock.release()

    def document_matches(
        self, path: Path, version: int, identity: WorkspaceFileIdentity
    ) -> bool:
        with self._state_lock:
            current = self._documents.get(self._target(path))
            return (
                current is not None and current[0] == version and current[2] == identity
            )

    def _providers(self, target: Path) -> tuple:
        providers = []
        static = self.server_capabilities.get("diagnosticProvider")
        if isinstance(static, dict) and _selector_matches(
            static.get("documentSelector"),
            target,
            {
                ".tsx": "typescriptreact",
                ".jsx": "javascriptreact",
                ".js": "javascript",
                ".mjs": "javascript",
                ".cjs": "javascript",
                ".ts": "typescript",
            }.get(target.suffix, self.language_id),
        ):
            providers.append(("static", static.get("identifier"), 0))
        language = {
            ".tsx": "typescriptreact",
            ".jsx": "javascriptreact",
            ".js": "javascript",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".ts": "typescript",
        }.get(target.suffix, self.language_id)
        for rid, registration in self._registrations.items():
            if registration["method"] != "textDocument/diagnostic":
                continue
            options = registration.get("registerOptions") or {}
            if _selector_matches(options.get("documentSelector"), target, language):
                providers.append(
                    (rid, options.get("identifier"), registration["_generation"])
                )
        return tuple(sorted(providers, key=repr))

    def diagnostics(
        self,
        path: Path,
        text: str | None = None,
        source_identity: WorkspaceFileIdentity | None = None,
        *,
        reader=None,
        operation: LspOperation | None = None,
    ) -> DiagnosticReport:
        operation = self._operation(operation, self.diagnostic_wait)
        target = self._target(path)
        uri = target.as_uri()
        version = None
        identity = None
        evidence = []
        truncated = False
        method = "push"
        freshness = "not_observed"
        terminal = "timeout"
        reason = ""
        analysis_environment = self.analysis_environment
        if self.analysis_profile is not None:
            from .servers import analysis_environment_for_file

            analysis_environment = analysis_environment_for_file(
                self.analysis_profile, target, self.analysis_workspace
            )

        def report(status, why=""):
            frozen, omitted = _diagnostic_snapshot(evidence)
            if omitted and status == "ready":
                status = "partial"
            return DiagnosticReport(
                str(target),
                status,
                freshness,
                frozen,
                version,
                identity,
                method,
                analysis_environment,
                why,
                truncated or omitted,
            )

        try:
            self._acquire(self._document_lock, operation)
            try:
                if reader is not None:
                    text, source_identity = reader()
                if text is None or source_identity is None:
                    raise ValueError("LSP diagnostics requires source identity")
                with self._state_lock:
                    start_seq = self._receive_seq
                try:
                    version = self._open_locked(
                        target, text, source_identity, operation
                    )
                except LSPError as exc:
                    if str(exc) == "sync_unavailable":
                        return report("unsupported", "sync_unavailable")
                    raise
                identity = source_identity
                with self._state_lock:
                    providers = self._providers(target)
                    static = self.server_capabilities.get("diagnosticProvider")
                    if isinstance(static, dict) and not _supported_selector(
                        static.get("documentSelector")
                    ):
                        reason = "unsupported_selector"
            finally:
                self._document_lock.release()
            identifiers = tuple(dict.fromkeys(item[1] for item in providers))
            completed = 0
            if identifiers:
                method = "pull"
                for identifier in identifiers:
                    self._check(operation)
                    params = {"textDocument": {"uri": uri}}
                    if identifier is not None:
                        params["identifier"] = identifier
                    try:
                        result = self.request(
                            "textDocument/diagnostic", params, operation=operation
                        )
                        if (
                            isinstance(result, dict)
                            and result.get("kind") == "full"
                            and isinstance(result.get("items"), list)
                        ):
                            items, omitted = _diagnostic_snapshot(result["items"])
                            evidence.extend(json.loads(items))
                            truncated |= omitted
                            completed += 1
                            freshness = "current"
                        else:
                            terminal = "unsupported"
                            reason = "invalid_diagnostic_report"
                    except LSPResponseError:
                        terminal = "unsupported"
                        reason = "pull_rejected"
                with self._state_lock:
                    if (
                        self._documents.get(target) != (version, text, identity)
                        or self._providers(target) != providers
                    ):
                        freshness = "not_observed"
                        evidence = []
                        return report("superseded", "input_changed")
                if completed == len(identifiers):
                    return report("partial" if truncated else "ready")
                if completed:
                    return report("partial", reason)
            while True:
                with self._changed:
                    if (
                        self._documents.get(target) != (version, text, identity)
                        or self._providers(target) != providers
                    ):
                        freshness = "not_observed"
                        evidence = []
                        return report("superseded", "input_changed")
                    cached = self._diagnostics.get(uri, {}).get("latest")
                    if cached and (cached[0] == version or cached[1] > start_seq):
                        push_version, _, items_json, omitted = cached
                        if push_version == version:
                            freshness = "current"
                            evidence = json.loads(items_json)
                            truncated = omitted
                            return report(
                                "partial" if identifiers or truncated else "ready",
                                reason,
                            )
                        if push_version is None:
                            freshness = "unversioned"
                            evidence = json.loads(items_json)
                            truncated = omitted
                    if self._closed:
                        terminal = "unavailable"
                        reason = "server_closed"
                        break
                    self._check(operation)
                    self._changed.wait(timeout=min(0.02, operation.remaining()))
        except LSPCancelledError:
            terminal = "cancelled"
            reason = "cancelled"
        except LSPTimeoutError:
            terminal = "unsupported" if reason == "unsupported_selector" else "timeout"
            reason = reason or "deadline_exceeded"
        except LSPError:
            terminal = "unavailable"
            reason = "lsp_unavailable"
        if version is not None:
            with self._state_lock:
                if (
                    self._documents.get(target) != (version, text, identity)
                    or self._providers(target) != providers
                ):
                    freshness = "not_observed"
                    evidence = []
                    return report("superseded", "input_changed")
                cached = self._diagnostics.get(uri, {}).get("latest")
                if freshness == "not_observed" and cached is not None:
                    push_version, seq, items_json, omitted = cached
                    if push_version == version or (
                        push_version is None and seq > start_seq
                    ):
                        evidence = json.loads(items_json)
                        truncated = omitted
                        freshness = (
                            "current" if push_version == version else "unversioned"
                        )
                        method = method + "+push" if method == "pull" else "push"
        return report("partial" if freshness != "not_observed" else terminal, reason)

    def close_document(
        self, path: Path, *, operation: LspOperation | None = None
    ) -> None:
        operation = self._operation(operation)
        target = self._target(path)
        self._acquire(self._document_lock, operation)
        try:
            with self._changed:
                existing = self._documents.pop(target, None)
                self._diagnostics.pop(target.as_uri(), None)
                self._changed.notify_all()
            if existing is not None and self._sync_options()[0]:
                self.notify(
                    "textDocument/didClose",
                    {"textDocument": {"uri": target.as_uri()}},
                    operation=operation,
                )
        finally:
            self._document_lock.release()

    def close(self, force: bool = False, *, deadline: float | None = None) -> None:
        operation = LspOperation(
            None, deadline if deadline is not None else time.monotonic() + 6
        )
        self._acquire(self._close_lock, operation)
        try:
            if self._close_complete:
                return
            if (
                not force
                and not self._closed
                and self.process is not None
                and self.process.poll() is None
            ):
                try:
                    self.request(
                        "shutdown",
                        {},
                        operation=LspOperation(
                            None, min(operation.deadline, time.monotonic() + 2)
                        ),
                    )
                    self.notify("exit", {}, operation=operation)
                except LSPError:
                    force = True
            with self._state_lock:
                self._closed = True
            self._fail_pending(LSPError("Language server closed"))
            with self._changed:
                self._changed.notify_all()
            self._acquire(self._spawn_lock, operation)
            self._spawn_lock.release()
            if self.process is not None:
                if not terminate_process_tree(
                    self.process, force=force, deadline=operation.deadline
                ):
                    raise LSPError("LSP cleanup pending")
                try:
                    self.process.wait(timeout=min(2.0, operation.remaining() / 2))
                except subprocess.TimeoutExpired as exc:
                    if not terminate_process_tree(
                        self.process, force=True, deadline=operation.deadline
                    ):
                        raise LSPError("LSP cleanup pending") from exc
                    try:
                        self.process.wait(timeout=operation.remaining())
                    except subprocess.TimeoutExpired as final:
                        raise LSPError("LSP cleanup pending") from final
                if not terminate_process_tree(
                    self.process, force=True, deadline=operation.deadline
                ):
                    raise LSPError("LSP cleanup pending")
            for thread in (self._reader, self._stderr_reader):
                if thread is None or thread.ident is None:
                    continue
                if thread is threading.current_thread():
                    raise LSPError("LSP cleanup pending")
                thread.join(timeout=operation.remaining())
                if thread.is_alive():
                    raise LSPError("LSP cleanup pending")
            while True:
                with self._state_lock:
                    sends = list(self._active_sends.items())
                if not sends:
                    break
                for thread, done in sends:
                    if thread is threading.current_thread():
                        raise LSPError("LSP cleanup pending")
                    done.wait(timeout=operation.remaining())
                    if thread.ident is not None:
                        thread.join(timeout=operation.remaining())
                    if thread.is_alive() or not done.is_set():
                        raise LSPError("LSP cleanup pending")
                    with self._state_lock:
                        self._active_sends.pop(thread, None)
                self._check(operation)
            if self.process is not None:
                for stream in (
                    self.process.stdin,
                    self.process.stdout,
                    self.process.stderr,
                ):
                    if stream is not None:
                        stream.close()
            self._check(operation)
            self._close_complete = True
        finally:
            self._close_lock.release()

    def _send(self, payload: dict, *, operation: LspOperation | None = None) -> None:
        operation = self._operation(operation)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(data) > _MAX_FRAME_BYTES:
            raise LSPError("LSP frame exceeds maximum size")
        framed = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data
        self._acquire(self._write_lock, operation)
        settled = threading.Event()
        completed = threading.Event()
        phase = {"sent": 0, "complete": False}
        phase_lock = threading.Lock()

        def watch():
            while not completed.wait(min(0.02, max(0.001, operation.remaining()))):
                if operation.cancelled() or operation.remaining() <= 0:
                    with phase_lock:
                        if phase["complete"] or phase["sent"] == 0:
                            return
                    self._closed = True
                    self._fail_pending(
                        LSPError("LSP connection interrupted during send")
                    )
                    if self.process is not None:
                        terminate_process_tree(self.process, force=True)
                    return

        watchdog = threading.Timer(0, watch)
        watchdog.daemon = True
        try:
            self._check(operation)
            with self._state_lock:
                if self._closed or self.process is None or not self.process.stdin:
                    raise LSPError("Language server is closed")
                self._active_sends[watchdog] = settled
            remaining = memoryview(framed)
            if os.name != "nt":
                # 非阻塞写让首字节前/完整帧后的取消不必牺牲健康的共享通道。
                descriptor = self.process.stdin.fileno()
                os.set_blocking(descriptor, False)
                while remaining:
                    self._check(operation)
                    try:
                        count = os.write(descriptor, remaining)
                    except BlockingIOError:
                        select.select(
                            [], [descriptor], [], min(0.02, operation.remaining())
                        )
                        continue
                    if not count:
                        raise BrokenPipeError
                    phase["sent"] += count
                    remaining = remaining[count:]
                phase["complete"] = True
            else:
                # Windows 匿名管道写可能阻塞，限时看门狗始终由client持有。
                with phase_lock:
                    phase["sent"] = -1
                watchdog.start()
                while remaining:
                    self._check(operation)
                    count = self.process.stdin.write(remaining)
                    if not count:
                        raise BrokenPipeError
                    remaining = remaining[count:]
                with phase_lock:
                    phase["complete"] = True
        except (LSPCancelledError, LSPTimeoutError):
            if phase["sent"] and not phase["complete"]:
                self._closed = True
                self._fail_pending(LSPError("LSP partial frame interrupted"))
                terminate_process_tree(
                    self.process, force=True, deadline=operation.deadline
                )
            raise
        except (BrokenPipeError, OSError) as exc:
            self._closed = True
            if self.process is not None:
                terminate_process_tree(
                    self.process, force=True, deadline=operation.deadline
                )
            self._check(operation)
            raise LSPError("Language server connection closed") from exc
        finally:
            completed.set()
            if watchdog.ident is not None:
                watchdog.join(timeout=max(0, operation.remaining()))
            self._write_lock.release()
            settled.set()
            with self._state_lock:
                if not watchdog.is_alive():
                    self._active_sends.pop(watchdog, None)

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                message = self._read_message()
                if message is None:
                    break
                self._handle_message(message)
        except Exception:
            error = LSPError(f"Language server {self.server_id} protocol failed")
            self._fail_pending(error)
            self._closed = True
            if self.process.poll() is None:
                terminate_process_tree(self.process, force=True)
        finally:
            if not self._closed:
                detail = self.stderr_tail
                suffix = f": {detail}" if detail else ""
                self._fail_pending(
                    LSPError(f"Language server {self.server_id} exited{suffix}")
                )
            with self._changed:
                self._closed = True
                self._changed.notify_all()

    def _read_message(self) -> dict | None:
        if not self.process.stdout:
            return None
        headers: dict[str, str] = {}
        header_bytes = 0
        while True:
            remaining = _MAX_HEADER_BYTES - header_bytes
            line = self.process.stdout.readline(remaining + 1)
            if not line:
                return None
            header_bytes += len(line)
            if header_bytes > _MAX_HEADER_BYTES or not line.endswith(b"\n"):
                raise LSPError("LSP header exceeds maximum size")
            if line in (b"\r\n", b"\n"):
                break
            decoded = line.decode("ascii", errors="replace").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.lower()] = value.strip()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError as exc:
            raise LSPError("Invalid Content-Length header") from exc
        if length <= 0:
            raise LSPError("Missing Content-Length header")
        if length > _MAX_FRAME_BYTES:
            raise LSPError("LSP frame exceeds maximum size")
        chunks = []
        remaining = length
        while remaining:
            chunk = self.process.stdout.read(remaining)
            if not chunk:
                raise LSPError("Truncated JSON-RPC message")
            chunks.append(chunk)
            remaining -= len(chunk)
        value = json.loads(b"".join(chunks).decode("utf-8"))
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
            target = uri_to_path(uri)
            version = params.get("version")
            diagnostics = params.get("diagnostics")
            with self._changed:
                current = self._documents.get(target)
                if (
                    current is not None
                    and isinstance(diagnostics, list)
                    and (
                        version is None
                        or (
                            isinstance(version, int)
                            and not isinstance(version, bool)
                            and version == current[0]
                        )
                    )
                ):
                    self._receive_seq += 1
                    items_json, truncated = _diagnostic_snapshot(diagnostics)
                    row = (version, self._receive_seq, items_json, truncated)
                    cached = self._diagnostics.setdefault(uri, {})
                    cached["latest"] = row
                    if version is not None:
                        cached["current"] = row
                    self._changed.notify_all()
            return
        if response_id is not None and method:
            response = {"jsonrpc": "2.0", "id": response_id}
            try:
                response["result"] = self._server_request_result(str(method), params)
            except LSPResponseError as exc:
                response["error"] = exc.error
            self._send(response)

    def _server_request_result(self, method: str, params: dict) -> Any:
        if method == "workspace/configuration":
            settings = json.loads(self.settings_json)
            result = []
            for item in params.get("items", []):
                value = settings
                section = item.get("section")
                if section:
                    for name in section.split("."):
                        value = value.get(name) if isinstance(value, dict) else None
                result.append(value)
            return result
        if method == "client/registerCapability":
            registrations = params.get("registrations") or []
            if any(
                item.get("method") not in {"textDocument/diagnostic"}
                for item in registrations
            ):
                raise LSPResponseError(
                    {"code": -32602, "message": "unsupported dynamic capability"}
                )
            for item in registrations:
                if item.get(
                    "method"
                ) == "textDocument/diagnostic" and not _supported_selector(
                    (item.get("registerOptions") or {}).get("documentSelector")
                ):
                    raise LSPResponseError(
                        {"code": -32602, "message": "unsupported diagnostic selector"}
                    )
            with self._changed:
                for item in registrations:
                    self._registrations[str(item["id"])] = {
                        **json.loads(json.dumps(item)),
                        "_generation": self._registration_generation + 1,
                    }
                self._registration_generation += 1
                self._changed.notify_all()
            return None
        if method == "client/unregisterCapability":
            with self._changed:
                for item in params.get(
                    "unregisterations", params.get("unregistrations", [])
                ):
                    self._registrations.pop(str(item["id"]), None)
                self._registration_generation += 1
                self._changed.notify_all()
            return None
        if method == "workspace/workspaceFolders":
            return [
                {
                    "name": self.root.name or "workspace",
                    "uri": path_to_uri(self.root),
                }
            ]
        return None

    def _read_stderr(self) -> None:
        if not self.process.stderr:
            return
        while True:
            raw = self.process.stderr.readline(_MAX_STDERR_LINE_BYTES + 1)
            if not raw:
                break
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


def _diagnostic_snapshot(values: list[Any]) -> tuple[str, bool]:
    result = []
    total = 2
    truncated = False
    for item in values:
        if not isinstance(item, dict):
            truncated = True
            continue
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if (
            len(result) >= _MAX_DIAGNOSTICS
            or size > _MAX_DIAGNOSTIC_BYTES
            or total + size > _MAX_FRAME_BYTES
        ):
            truncated = True
            continue
        result.append(item)
        total += size + 1
    return json.dumps(result, ensure_ascii=False), truncated


def _selector_matches(selector, path: Path, language: str) -> bool:
    if selector is None:
        return True
    if not isinstance(selector, list):
        return False
    for item in selector:
        if not isinstance(item, dict) or set(item) - {"language", "scheme", "pattern"}:
            continue
        if (
            item.get("language", language) != language
            or item.get("scheme", "file") != "file"
        ):
            continue
        pattern = item.get("pattern")
        if pattern is None:
            return True
        if isinstance(pattern, str) and (
            _glob_matches(path.as_posix(), pattern)
            or ("/" not in pattern and _glob_matches(path.name, pattern))
        ):
            return True
        if isinstance(pattern, dict) and set(pattern) <= {"baseUri", "pattern"}:
            base = pattern.get("baseUri")
            if isinstance(base, dict):
                base = base.get("uri")
            root = uri_to_path(base) if isinstance(base, str) else None
            if (
                root is not None
                and path.is_relative_to(root)
                and isinstance(pattern.get("pattern"), str)
            ):
                if _glob_matches(path.relative_to(root).as_posix(), pattern["pattern"]):
                    return True
    return False


def _glob_matches(value: str, pattern: str) -> bool:
    # * 只能跨字符，** 才能跨目录；不能用 fnmatch 将 '/' 当作普通字符。
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match:
        choices = match.group(1).split(",")
        return len(choices) <= 64 and any(
            _glob_matches(
                value, pattern[: match.start()] + item + pattern[match.end() :]
            )
            for item in choices
        )
    values = value.split("/")
    patterns = pattern.split("/")

    def compare(i, j):
        if j == len(patterns):
            return i == len(values)
        if patterns[j] == "**":
            return any(compare(k, j + 1) for k in range(i, len(values) + 1))
        return (
            i < len(values)
            and fnmatch.fnmatchcase(values[i], patterns[j])
            and compare(i + 1, j + 1)
        )

    return compare(0, 0)


def _supported_selector(selector) -> bool:
    if selector is None:
        return True
    if not isinstance(selector, list):
        return False
    for item in selector:
        if not isinstance(item, dict) or set(item) - {"language", "scheme", "pattern"}:
            return False
        if any(
            key in item and not isinstance(item[key], str)
            for key in ("language", "scheme")
        ):
            return False
        pattern = item.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            if (
                not isinstance(pattern, dict)
                or set(pattern) != {"baseUri", "pattern"}
                or not isinstance(pattern["pattern"], str)
            ):
                return False
            base = pattern["baseUri"]
            if isinstance(base, dict):
                base = base.get("uri")
            if not isinstance(base, str) or uri_to_path(base) is None:
                return False
    return True
