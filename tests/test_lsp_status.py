"""Tests for the read-only LSP sidebar status projection."""
from __future__ import annotations

from types import SimpleNamespace

from nz_coder.lsp import manager


def test_client_status_summary_is_workspace_scoped_and_secret_free(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    first = (str(tmp_path.resolve()), "pyright", str(tmp_path.resolve()))
    second = (str(other.resolve()), "clangd", str(other.resolve()))
    old_clients, old_broken = dict(manager._CLIENTS), set(manager._BROKEN)
    try:
        manager._CLIENTS.clear()
        manager._BROKEN.clear()
        manager._CLIENTS[first] = SimpleNamespace(
            process=SimpleNamespace(poll=lambda: None),
        )
        manager._BROKEN.add(second)

        rows = manager.client_status_summary(tmp_path)

        assert rows == [{
            "id": "pyright",
            "root": str(tmp_path.resolve()),
            "status": "connected",
        }]
    finally:
        manager._CLIENTS.clear()
        manager._CLIENTS.update(old_clients)
        manager._BROKEN.clear()
        manager._BROKEN.update(old_broken)
