"""Workspace-scoped language-server configuration and cache contracts."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace


def _source(workspace: Path, name: str = "app.py") -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / name
    path.write_text("answer = 42\n", encoding="utf-8")
    return path


def _snapshot(workspace: Path, command: str, tmp_path: Path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot

    return load_config_snapshot(
        workspace,
        environ={"NZ_LSP_PYTHON_COMMAND": command},
        user_config_path=tmp_path / "missing-user.env",
    )


def test_resolve_server_uses_target_workspace_config_snapshot(tmp_path):
    from nz_coder.lsp.servers import resolve_server

    workspace = tmp_path / "target"
    source = _source(workspace)
    snapshot = _snapshot(workspace, sys.executable, tmp_path)

    server = resolve_server(source, workspace, config_snapshot=snapshot)

    assert server is not None
    assert Path(server.command[0]).resolve() == Path(sys.executable).resolve()
    assert server.config_source == "environment-config"


def test_lsp_override_does_not_bleed_between_workspaces(tmp_path):
    from nz_coder.lsp.servers import resolve_server

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_source = _source(first)
    second_source = _source(second)
    first_command = tmp_path / "lsp-a"
    second_command = tmp_path / "lsp-b"
    first_command.write_text("a", encoding="utf-8")
    second_command.write_text("b", encoding="utf-8")
    first_snapshot = _snapshot(first, str(first_command), tmp_path)
    second_snapshot = _snapshot(second, str(second_command), tmp_path)

    first_server = resolve_server(
        first_source, first, config_snapshot=first_snapshot
    )
    second_server = resolve_server(
        second_source, second, config_snapshot=second_snapshot
    )

    assert first_server is not None and second_server is not None
    assert Path(first_server.command[0]) == first_command
    assert Path(second_server.command[0]) == second_command


def test_startup_workspace_override_is_not_used_for_target_workspace(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.lsp.servers import resolve_server

    startup = tmp_path / "startup"
    target = tmp_path / "target"
    startup_command = startup / "lsp-a"
    startup_command.parent.mkdir()
    startup_command.write_text("a", encoding="utf-8")
    source = _source(target)
    monkeypatch.setattr(
        config,
        "get",
        lambda key, default="": str(startup_command)
        if key == "NZ_LSP_PYTHON_COMMAND"
        else default,
    )
    monkeypatch.setattr("nz_coder.lsp.servers.shutil.which", lambda _name: None)

    assert resolve_server(source, target) is None


def test_startup_workspace_executable_is_not_system_trusted_in_other_workspace(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.lsp.servers import resolve_server

    startup = tmp_path / "startup"
    target = tmp_path / "target"
    executable = startup / "lsp-a"
    executable.parent.mkdir()
    executable.write_text("a", encoding="utf-8")
    source = _source(target)
    monkeypatch.setattr(config, "get", lambda *_args: str(executable))
    monkeypatch.setattr("nz_coder.lsp.servers.shutil.which", lambda _name: None)

    assert resolve_server(source, target) is None


def test_trusted_target_workspace_lsp_override_is_applied(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.lsp.servers import resolve_server

    workspace = tmp_path / "target"
    source = _source(workspace)
    workspace.joinpath(".env").write_text(
        f"NZ_LSP_PYTHON_COMMAND={sys.executable}\n", encoding="utf-8"
    )
    store = WorkspaceTrustStore(tmp_path / "trust.json")
    untrusted = load_config_snapshot(
        workspace, environ={}, user_config_path=tmp_path / "missing", trust_store=store
    )
    monkeypatch.setattr("nz_coder.lsp.servers.shutil.which", lambda _name: None)
    assert resolve_server(source, workspace, config_snapshot=untrusted) is None

    store.trust(workspace, "workspace-config", untrusted.workspace_fingerprint)
    trusted = load_config_snapshot(
        workspace, environ={}, user_config_path=tmp_path / "missing", trust_store=store
    )
    server = resolve_server(source, workspace, config_snapshot=trusted)

    assert server is not None
    assert server.config_source == "trusted-workspace-config"
    assert Path(server.command[0]).resolve() == Path(sys.executable).resolve()


def test_lsp_cache_rotates_when_resolved_fingerprint_changes(tmp_path, monkeypatch):
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import ResolvedServer

    workspace = tmp_path / "workspace"
    source = _source(workspace)
    current = {"fingerprint": "one"}

    def resolved(*_args, **_kwargs):
        return ResolvedServer(
            server_id="fake",
            language_id="python",
            command=(sys.executable,),
            root=workspace,
            source="system",
            config_source="environment-config",
            fingerprint=current["fingerprint"],
        )

    clients = []

    class Client:
        def __init__(self, **_kwargs):
            self.closed = False
            self.process = SimpleNamespace(poll=lambda: None)
            clients.append(self)

        def close(self):
            self.closed = True

    manager.close_all_clients()
    monkeypatch.setattr(manager, "resolve_server", resolved)
    monkeypatch.setattr(manager, "LSPClient", Client)
    first = manager.get_client_for_file(source, workspace)
    current["fingerprint"] = "two"
    second = manager.get_client_for_file(source, workspace)

    assert first is not second
    assert first.closed is True
    assert len(clients) == 2
    manager.close_all_clients()
