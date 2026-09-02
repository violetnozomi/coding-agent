"""Workspace-local LSP executables require exact fingerprint trust."""
from __future__ import annotations

import os


def _typescript_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    source = workspace / "src" / "main.ts"
    executable = workspace / "node_modules" / ".bin" / "typescript-language-server"
    source.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    source.write_text("export const answer = 42\n", encoding="utf-8")
    executable.write_text("version-one\n", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    return workspace, source, executable


def test_workspace_node_modules_lsp_is_not_started_before_trust(tmp_path, monkeypatch):
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import resolve_server

    workspace, source, _ = _typescript_workspace(tmp_path)
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "user" / "trust.json")
    )
    server = resolve_server(source, workspace)
    assert server is not None
    assert server.source == "workspace"
    assert server.trusted is False
    monkeypatch.setattr(
        manager,
        "LSPClient",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted workspace LSP must not spawn")
        ),
    )

    manager.close_all_clients()
    assert manager.get_client_for_file(source, workspace) is None
    assert "requires trust" in manager.client_startup_error(source, workspace)
    assert manager.client_status_summary(workspace)[0]["status"] == "trust-required"
    manager.close_all_clients()


def test_lsp_trust_is_invalidated_when_workspace_executable_changes(
    tmp_path,
    monkeypatch,
):
    from nz_coder.lsp.servers import resolve_server, trust_server

    workspace, source, executable = _typescript_workspace(tmp_path)
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "user" / "trust.json")
    )
    trusted = trust_server(source, workspace)
    assert trusted.trusted is True

    executable.write_text("version-two\n", encoding="utf-8")
    changed = resolve_server(source, workspace)
    assert changed is not None
    assert changed.fingerprint != trusted.fingerprint
    assert changed.trusted is False


def test_system_lsp_remains_trusted(tmp_path, monkeypatch):
    from nz_coder.lsp.servers import resolve_server

    source = tmp_path / "main.go"
    source.write_text("package main\n", encoding="utf-8")
    monkeypatch.setattr(
        "nz_coder.lsp.servers.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    server = resolve_server(source, tmp_path)

    assert server is not None
    assert server.source == "system"
    assert server.trusted is True
