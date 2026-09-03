"""Workspace-scoped lifecycle management for persistent LSP clients."""
from __future__ import annotations

import atexit
import threading
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.foundation.workspace_trust import ConfigSnapshot

from .client import LSPClient
from .servers import ResolvedServer, resolve_server

_LOCK = threading.RLock()
_ClientKey = tuple
_CLIENTS: dict[_ClientKey, LSPClient] = {}
_BROKEN: set[_ClientKey] = set()
_ERRORS: dict[_ClientKey, str] = {}
_TRUST_REQUIRED: set[_ClientKey] = set()


def _client_key(
    path: Path,
    workspace: Path,
    config_snapshot: ConfigSnapshot | None = None,
) -> tuple[ResolvedServer | None, _ClientKey | None]:
    legacy_globals = config_snapshot is None
    if config_snapshot is None:
        from nz_coder.foundation.workspace_trust import (
            active_config_snapshot,
            current_config_snapshot,
        )

        config_snapshot = (
            active_config_snapshot(workspace)
            or current_config_snapshot(workspace)
        )
        legacy_globals = active_config_snapshot(workspace) is None
    resolved = resolve_server(path, workspace, config_snapshot=config_snapshot)
    if resolved is None:
        return None, None
    key = (
        str(workspace.resolve()),
        resolved.server_id,
        str(resolved.root.resolve()),
        resolved.fingerprint,
        resolved.command,
        resolved.config_source,
        str(
            config.LSP_INITIALIZE_TIMEOUT_SECONDS
            if legacy_globals
            else config_snapshot.get_float(
                "NZ_LSP_INITIALIZE_TIMEOUT_SECONDS", 20.0,
                minimum=0.001, maximum=600.0,
            )
        ),
        str(
            config.LSP_REQUEST_TIMEOUT_SECONDS
            if legacy_globals
            else config_snapshot.get_float(
                "NZ_LSP_REQUEST_TIMEOUT_SECONDS", 10.0,
                minimum=0.001, maximum=600.0,
            )
        ),
    )
    return resolved, key


def get_client_for_file(
    path: Path,
    workspace: Path,
    *,
    config_snapshot: ConfigSnapshot | None = None,
) -> LSPClient | None:
    """Return a cached client, starting it on first use."""
    legacy_globals = config_snapshot is None
    if config_snapshot is None:
        from nz_coder.foundation.workspace_trust import (
            active_config_snapshot,
            current_config_snapshot,
        )

        config_snapshot = (
            active_config_snapshot(workspace)
            or current_config_snapshot(workspace)
        )
        legacy_globals = active_config_snapshot(workspace) is None
    if legacy_globals:
        enabled = config.LSP_ENABLED
    else:
        enabled = config_snapshot.get_bool("NZ_LSP_ENABLED", True)
    if not enabled:
        close_workspace_clients(workspace)
        return None
    resolved, key = _client_key(path, workspace, config_snapshot)
    if resolved is None or key is None:
        return None
    with _LOCK:
        identity = key[:3]
        stale_keys = [candidate for candidate in _CLIENTS if candidate[:3] == identity and candidate != key]
        stale_clients = [_CLIENTS.pop(candidate) for candidate in stale_keys]
        for candidate in stale_keys:
            _BROKEN.discard(candidate)
            _ERRORS.pop(candidate, None)
            _TRUST_REQUIRED.discard(candidate)
        for stale in stale_clients:
            stale.close()
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
                initialize_timeout=float(key[-2]),
                request_timeout=float(key[-1]),
            )
        except Exception as exc:
            _BROKEN.add(key)
            _ERRORS[key] = str(exc)
            return None
        _CLIENTS[key] = client
        _ERRORS.pop(key, None)
        return client


def client_startup_error(
    path: Path,
    workspace: Path,
    *,
    config_snapshot: ConfigSnapshot | None = None,
) -> str:
    """Return the cached initialization failure for a source file."""
    _, key = _client_key(path, workspace, config_snapshot)
    if key is None:
        return ""
    with _LOCK:
        return _ERRORS.get(key, "")


def client_status_summary(workspace: Path) -> list[dict[str, str]]:
    """Return secret-free active/broken LSP rows for terminal consumers."""
    root = str(workspace.resolve())
    rows = []
    with _LOCK:
        for key, client in _CLIENTS.items():
            owner, server_id, server_root = key[:3]
            if owner != root:
                continue
            rows.append({
                "id": server_id,
                "root": server_root,
                "status": "connected" if client.process.poll() is None else "failed",
            })
        for key in _BROKEN:
            owner, server_id, server_root = key[:3]
            if owner != root or any(
                row["id"] == server_id and row["root"] == server_root
                for row in rows
            ):
                continue
            rows.append({"id": server_id, "root": server_root, "status": "failed"})
        for key in _TRUST_REQUIRED:
            owner, server_id, server_root = key[:3]
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
