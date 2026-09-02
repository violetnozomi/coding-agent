"""Workspace-scoped lifecycle management for persistent LSP clients."""
from __future__ import annotations

import atexit
import threading
from pathlib import Path

from nz_coder.foundation import config

from .client import LSPClient
from .servers import ResolvedServer, resolve_server

_LOCK = threading.RLock()
_CLIENTS: dict[tuple[str, str, str], LSPClient] = {}
_BROKEN: set[tuple[str, str, str]] = set()
_ERRORS: dict[tuple[str, str, str], str] = {}
_TRUST_REQUIRED: set[tuple[str, str, str]] = set()


def _client_key(
    path: Path,
    workspace: Path,
) -> tuple[ResolvedServer | None, tuple[str, str, str] | None]:
    resolved = resolve_server(path, workspace)
    if resolved is None:
        return None, None
    key = (
        str(workspace.resolve()),
        resolved.server_id,
        str(resolved.root.resolve()),
    )
    return resolved, key


def get_client_for_file(path: Path, workspace: Path) -> LSPClient | None:
    """Return a cached client, starting it on first use."""
    if not config.LSP_ENABLED:
        return None
    resolved, key = _client_key(path, workspace)
    if resolved is None or key is None:
        return None
    with _LOCK:
        if not resolved.trusted:
            stale = _CLIENTS.pop(key, None)
            if stale is not None:
                stale.close()
            _TRUST_REQUIRED.add(key)
            _ERRORS[key] = (
                f"Workspace LSP executable '{resolved.server_id}' requires trust; "
                "run `nz-coder lsp trust <source-file>` after review"
            )
            return None
        _TRUST_REQUIRED.discard(key)
        existing = _CLIENTS.get(key)
        if existing is not None and existing.process.poll() is None:
            return existing
        if key in _BROKEN:
            return None
        try:
            client = LSPClient(
                server_id=resolved.server_id,
                command=resolved.command,
                root=resolved.root,
                language_id=resolved.language_id,
                analysis_paths=resolved.analysis_paths,
            )
        except Exception as exc:
            _BROKEN.add(key)
            _ERRORS[key] = str(exc)
            return None
        _CLIENTS[key] = client
        _ERRORS.pop(key, None)
        return client


def client_startup_error(path: Path, workspace: Path) -> str:
    """Return the cached initialization failure for a source file."""
    _, key = _client_key(path, workspace)
    if key is None:
        return ""
    with _LOCK:
        return _ERRORS.get(key, "")


def client_status_summary(workspace: Path) -> list[dict[str, str]]:
    """Return secret-free active/broken LSP rows for terminal consumers."""
    root = str(workspace.resolve())
    rows = []
    with _LOCK:
        for (owner, server_id, server_root), client in _CLIENTS.items():
            if owner != root:
                continue
            rows.append({
                "id": server_id,
                "root": server_root,
                "status": "connected" if client.process.poll() is None else "failed",
            })
        for owner, server_id, server_root in _BROKEN:
            if owner != root or any(
                row["id"] == server_id and row["root"] == server_root
                for row in rows
            ):
                continue
            rows.append({"id": server_id, "root": server_root, "status": "failed"})
        for owner, server_id, server_root in _TRUST_REQUIRED:
            if owner != root or any(
                row["id"] == server_id and row["root"] == server_root
                for row in rows
            ):
                continue
            rows.append({
                "id": server_id,
                "root": server_root,
                "status": "trust-required",
            })
    return sorted(rows, key=lambda item: (item["id"], item["root"]))


def close_all_clients() -> None:
    """Close every cached language server."""
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
        _BROKEN.clear()
        _ERRORS.clear()
        _TRUST_REQUIRED.clear()
    for client in clients:
        client.close()


def close_workspace_clients(workspace: Path) -> None:
    """Close only language servers owned by one workspace runtime."""
    owner = str(Path(workspace).resolve())
    with _LOCK:
        selected_keys = [key for key in _CLIENTS if key[0] == owner]
        clients = [_CLIENTS.pop(key) for key in selected_keys]
        _BROKEN.difference_update(tuple(
            key for key in _BROKEN if key[0] == owner
        ))
        _TRUST_REQUIRED.difference_update(tuple(
            key for key in _TRUST_REQUIRED if key[0] == owner
        ))
        for key in tuple(_ERRORS):
            if key[0] == owner:
                _ERRORS.pop(key, None)
    for client in clients:
        client.close()


atexit.register(close_all_clients)
