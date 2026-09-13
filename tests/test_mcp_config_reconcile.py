"""Tests for layered MCP configuration, trust, and live reconciliation."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def _configure_paths(monkeypatch, config, tmp_path):
    user = tmp_path / "user" / "mcp.json"
    project = tmp_path / "workspace" / ".nz-coder" / "mcp.json"
    trust = tmp_path / "user" / "mcp-trust.json"
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(user))
    monkeypatch.setattr(config, "MCP_PROJECT_CONFIG", ".nz-coder/mcp.json")
    monkeypatch.setattr(config, "MCP_TRUST_STORE", str(trust))
    monkeypatch.setattr(config, "MCP_SERVERS_JSON", "")
    return user, project, trust


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _trust_project_control(workspace: Path, monkeypatch) -> None:
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    trust_path = workspace.parent / f"{workspace.name}-control-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore(trust_path).trust(
        workspace,
        "workspace-control",
        snapshot.control_fingerprint,
    )


def test_layered_configs_replace_by_name_and_require_project_command_trust(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.mcp import MCPTrustStore, load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user, project, trust_path = _configure_paths(monkeypatch, config, tmp_path)
    _write_json(user, {
        "servers": {
            "shared": {"command": ["user-command"]},
            "user-only": {"command": ["user-only"]},
        }
    })
    _write_json(project, {
        "servers": {
            "shared": {"command": ["project-command"]},
            "project-only": {"command": ["project-only"]},
        }
    })
    _trust_project_control(workspace, monkeypatch)
    monkeypatch.setattr(
        config,
        "MCP_SERVERS_JSON",
        json.dumps({"servers": {"shared": {"command": ["environment-command"]}}}),
    )

    configs = load_mcp_server_configs(workspace=workspace, compatibility_mode=True)
    by_name = {server.name: server for server in configs}

    assert by_name["shared"].command == ("environment-command",)
    assert by_name["shared"].source == "environment"
    assert by_name["shared"].trusted is True
    assert by_name["user-only"].source == "user"
    assert by_name["user-only"].trusted is True
    assert by_name["project-only"].source == "project"
    assert by_name["project-only"].trusted is False

    project_server = by_name["project-only"]
    store = MCPTrustStore(trust_path)
    store.trust(workspace, project_server.name, project_server.fingerprint)
    trusted = {
        server.name: server
        for server in load_mcp_server_configs(workspace=workspace, compatibility_mode=True)
    }["project-only"]
    assert trusted.trusted is True
    assert stat_mode(trust_path) == 0o600

    _write_json(project, {
        "servers": {
            "project-only": {"command": ["changed-command"]},
        }
    })
    _trust_project_control(workspace, monkeypatch)
    changed = {
        server.name: server
        for server in load_mcp_server_configs(workspace=workspace, compatibility_mode=True)
    }["project-only"]
    assert changed.fingerprint != project_server.fingerprint
    assert changed.trusted is False


def test_project_remote_mcp_requires_trust_and_security_changes_invalidate_it(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.mcp import MCPTrustStore, load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, project, trust_path = _configure_paths(monkeypatch, config, tmp_path)
    _write_json(project, {
        "servers": {
            "remote": {
                "type": "remote",
                "url": "https://mcp.example.test/service",
                "header_env": {"Authorization": "MCP_REMOTE_TOKEN"},
                "tool_effects": {"lookup": "read"},
            }
        }
    })
    _trust_project_control(workspace, monkeypatch)

    server = load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0]
    assert server.source == "project"
    assert server.trusted is False
    MCPTrustStore(trust_path).trust(workspace, server.name, server.fingerprint)
    assert load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0].trusted is True

    _write_json(project, {
        "servers": {
            "remote": {
                "type": "remote",
                "url": "https://other.example.test/service",
                "header_env": {"Authorization": "MCP_REMOTE_TOKEN"},
                "tool_effects": {"lookup": "read"},
            }
        }
    })
    assert load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0].trusted is False


def test_project_mcp_executable_change_invalidates_trust(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.mcp import MCPTrustStore, load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = workspace / "mcp-server"
    executable.write_text("version-one\n", encoding="utf-8")
    _, project, trust_path = _configure_paths(monkeypatch, config, tmp_path)
    _write_json(project, {
        "servers": {"local": {"command": ["./mcp-server"]}}
    })
    _trust_project_control(workspace, monkeypatch)
    server = load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0]
    MCPTrustStore(trust_path).trust(workspace, server.name, server.fingerprint)
    assert load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0].trusted is True

    executable.write_text("version-two\n", encoding="utf-8")
    changed = load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0]
    assert changed.fingerprint != server.fingerprint
    assert changed.trusted is False


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_project_config_symlink_escape_is_rejected_before_read(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.mcp import load_mcp_server_configs

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)
    _configure_paths(monkeypatch, config, tmp_path)

    assert load_mcp_server_configs(workspace=workspace, compatibility_mode=True) == []


def test_runtime_reconcile_add_change_remove_and_untrusted_status(tmp_path):
    from nz_coder.mcp import MCPRuntime, MCPServerConfig

    created = []

    class Client:
        def __init__(self, *, name, **kwargs):
            self.name = name
            self.closed = False
            created.append(self)

        def start(self):
            return {}

        def list_tools(self):
            return [{"name": "ping", "inputSchema": {"type": "object"}}]

        def close(self):
            self.closed = True

    one = MCPServerConfig(name="one", command=("one",), cwd=tmp_path)
    runtime = MCPRuntime([one], client_factory=Client).start()
    first = runtime.clients["one"]
    try:
        two = MCPServerConfig(name="two", command=("two",), cwd=tmp_path)
        changed_one = MCPServerConfig(name="one", command=("changed",), cwd=tmp_path)
        result = runtime.reconcile([changed_one, two])

        assert result == {"added": ["two"], "removed": [], "changed": ["one"]}
        assert first.closed is True
        assert runtime.clients["one"] is not first
        assert set(runtime.clients) == {"one", "two"}

        untrusted = MCPServerConfig(
            name="blocked",
            command=("blocked",),
            cwd=tmp_path,
            source="project",
            trusted=False,
            fingerprint="abc",
        )
        result = runtime.reconcile([two, untrusted])
        statuses = {item["name"]: item["status"] for item in runtime.status_summary()}

        assert result == {"added": ["blocked"], "removed": ["one"], "changed": []}
        assert statuses["blocked"] == "untrusted"
        assert "blocked" not in runtime.clients
        assert "one" not in runtime.clients
    finally:
        runtime.close()


def test_real_project_stdio_server_trust_and_file_reconcile(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.mcp import MCPRuntime, MCPTrustStore, load_mcp_server_configs
    from nz_coder.tools import dispatch, scoped_dynamic_tool_provider

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, project, trust_path = _configure_paths(monkeypatch, config, tmp_path)
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    _write_json(project, {
        "servers": {
            "echo": {
                "command": [sys.executable, str(FIXTURE_SERVER)],
                "cwd": ".",
                "startup_timeout_seconds": 3,
                "tool_timeout_seconds": 2,
                "tool_effects": {"echo": "read"},
            }
        }
    })
    _trust_project_control(workspace, monkeypatch)

    runtime = MCPRuntime.configured(
        workspace=workspace, compatibility_mode=True,
    ).start()
    try:
        assert runtime.status_summary()[0]["status"] == "untrusted"
        assert runtime.tool_bindings() == []

        server = load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0]
        MCPTrustStore(trust_path).trust(workspace, "echo", server.fingerprint)
        bindings = runtime.tool_bindings()

        assert {item["name"] for item in bindings} >= {"mcp_echo_echo"}
        with scoped_dynamic_tool_provider(runtime.tool_bindings):
            output = dispatch("mcp_echo_echo", {"value": "live"})
        assert "echo:live" in output
        process = runtime.clients["echo"].process

        _write_json(project, {
            "servers": {
                "echo": {
                    "command": [sys.executable, str(FIXTURE_SERVER), "--changed"],
                    "cwd": ".",
                    "tool_effects": {"echo": "read"},
                }
            }
        })
        assert {item["name"] for item in runtime.tool_bindings()} >= {
            "mcp_echo_echo"
        }
        assert runtime.reload_config() is True
        assert runtime.tool_bindings() == []
        assert runtime.status_summary()[0]["status"] == "untrusted"
        assert process is not None and process.poll() is not None

        project.unlink()
        assert runtime.reload_config() is True
        assert runtime.tool_bindings() == []
        assert runtime.status_summary() == []
    finally:
        runtime.close()


def test_invalid_reloaded_config_keeps_last_healthy_generation(tmp_path):
    from nz_coder.mcp import MCPRuntime, MCPServerConfig

    revisions = iter(["v1", "v2"])
    current_revision = [next(revisions)]
    healthy = MCPServerConfig(name="healthy", command=("unused",), cwd=tmp_path)

    class Client:
        def __init__(self, **kwargs):
            self.closed = False

        def start(self):
            return {}

        def list_tools(self):
            return []

        def close(self):
            self.closed = True

    def broken_loader():
        raise ValueError("broken project config with secret-like details")

    runtime = MCPRuntime(
        [healthy],
        client_factory=Client,
        workspace=tmp_path,
        config_loader=broken_loader,
        config_revision=lambda: current_revision[0],
    ).start()
    original = runtime.clients["healthy"]
    current_revision[0] = next(revisions)
    try:
        assert runtime.tool_bindings() == []
        assert runtime.clients["healthy"] is original
        config_status = {
            item["name"]: item for item in runtime.status_summary()
        }["$config"]
        assert config_status["status"] == "failed"
        assert config_status["error"] == "ValueError"
    finally:
        runtime.close()


def test_mcp_cli_lists_trusts_and_untrusts_project_command(
    tmp_path,
    monkeypatch,
    capsys,
):
    from nz_coder.foundation import config
    from nz_coder.mcp import load_mcp_server_configs
    from nz_coder.mcp.cli import mcp_main

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "server.py").write_text("print('ok')\n", encoding="utf-8")
    user, project, trust = _configure_paths(monkeypatch, config, tmp_path)
    monkeypatch.setenv("NZ_MCP_USER_CONFIG", str(user))
    monkeypatch.setenv("NZ_MCP_TRUST_STORE", str(trust))
    monkeypatch.chdir(workspace)
    _write_json(project, {
        "servers": {"local": {"command": ["python3", "server.py"]}}
    })
    _trust_project_control(workspace, monkeypatch)

    assert mcp_main(["list"]) == 0
    assert "source=project" in capsys.readouterr().out
    assert mcp_main(["trust", "local"]) == 0
    assert "trusted" in capsys.readouterr().out
    assert load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0].trusted is True
    assert mcp_main(["untrust", "local"]) == 0
    assert "untrusted" in capsys.readouterr().out
    assert load_mcp_server_configs(workspace=workspace, compatibility_mode=True)[0].trusted is False
