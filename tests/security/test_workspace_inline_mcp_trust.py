"""Workspace configuration trust never substitutes for MCP capability trust."""
from __future__ import annotations

import json


def _inline_snapshot(tmp_path, monkeypatch, payload):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    trust_path = tmp_path / "workspace-trust.json"
    mcp_trust_path = tmp_path / "mcp-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    monkeypatch.setenv("NZ_MCP_TRUST_STORE", str(mcp_trust_path))
    workspace.joinpath(".env").write_text(
        "NZ_MCP_ENABLED=1\nNZ_MCP_SERVERS_JSON="
        + json.dumps({"servers": payload}, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    store = WorkspaceTrustStore(trust_path)
    initial = load_config_snapshot(workspace, environ={
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(trust_path),
        "NZ_MCP_TRUST_STORE": str(mcp_trust_path),
    })
    store.trust(workspace, "workspace-config", initial.workspace_fingerprint)
    return workspace, mcp_trust_path, load_config_snapshot(workspace, environ={
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(trust_path),
        "NZ_MCP_TRUST_STORE": str(mcp_trust_path),
    })


def test_trusted_workspace_inline_mcp_still_requires_server_trust(tmp_path, monkeypatch):
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace, _trust_path, snapshot = _inline_snapshot(
        tmp_path, monkeypatch, {"inline": {"command": ["server"]}},
    )
    server = load_mcp_server_configs(workspace=workspace, config_snapshot=snapshot)[0]

    assert server.source == "trusted-workspace"
    assert server.fingerprint
    assert server.trusted is False


def test_inline_workspace_mcp_can_be_trusted_explicitly(tmp_path, monkeypatch):
    from nz_coder.mcp.config import load_mcp_server_configs
    from nz_coder.mcp.trust import MCPTrustStore

    workspace, trust_path, snapshot = _inline_snapshot(
        tmp_path, monkeypatch, {"inline": {"command": ["server"]}},
    )
    server = load_mcp_server_configs(workspace=workspace, config_snapshot=snapshot)[0]
    MCPTrustStore(trust_path).trust(workspace, server.name, server.fingerprint)

    trusted = load_mcp_server_configs(workspace=workspace, config_snapshot=snapshot)[0]
    assert trusted.trusted is True


def test_inline_workspace_mcp_change_invalidates_server_trust(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs
    from nz_coder.mcp.trust import MCPTrustStore

    workspace, trust_path, first = _inline_snapshot(
        tmp_path, monkeypatch, {"inline": {"command": ["server-v1"]}},
    )
    server = load_mcp_server_configs(workspace=workspace, config_snapshot=first)[0]
    MCPTrustStore(trust_path).trust(workspace, server.name, server.fingerprint)
    workspace.joinpath(".env").write_text(
        "NZ_MCP_ENABLED=1\nNZ_MCP_SERVERS_JSON="
        + json.dumps({"servers": {"inline": {"command": ["server-v2"]}}}, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    workspace_store = WorkspaceTrustStore(tmp_path / "workspace-trust.json")
    changed = load_config_snapshot(workspace, environ={
        "NZ_MCP_TRUST_STORE": str(trust_path),
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(tmp_path / "workspace-trust.json"),
    })
    workspace_store.trust(workspace, "workspace-config", changed.workspace_fingerprint)
    changed = load_config_snapshot(workspace, environ={
        "NZ_MCP_TRUST_STORE": str(trust_path),
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(tmp_path / "workspace-trust.json"),
    })
    rotated = load_mcp_server_configs(workspace=workspace, config_snapshot=changed)[0]

    assert rotated.fingerprint != server.fingerprint
    assert rotated.trusted is False


def test_untrusted_inline_workspace_mcp_never_starts_process(tmp_path, monkeypatch):
    from nz_coder.mcp.client import MCPClient
    from nz_coder.mcp.runtime import MCPRuntime

    workspace, _trust_path, snapshot = _inline_snapshot(
        tmp_path, monkeypatch, {"inline": {"command": ["server"]}},
    )
    started = []

    def forbidden_start(*_args, **_kwargs):
        started.append(True)
        raise AssertionError("untrusted inline MCP must not start")

    monkeypatch.setattr(MCPClient, "start", forbidden_start)
    runtime = MCPRuntime.configured(workspace=workspace, config_snapshot=snapshot)
    try:
        runtime.start()
        assert runtime.status_summary()[0]["status"] == "untrusted"
        assert started == []
    finally:
        runtime.close()


def test_inline_workspace_mcp_executable_change_invalidates_trust(tmp_path, monkeypatch):
    from nz_coder.mcp.config import load_mcp_server_configs
    from nz_coder.mcp.trust import MCPTrustStore

    executable = tmp_path / "workspace" / "server"
    executable.parent.mkdir()
    executable.write_text("one", encoding="utf-8")
    workspace, trust_path, snapshot = _inline_snapshot(
        tmp_path, monkeypatch, {"inline": {"command": ["./server"]}},
    )
    first = load_mcp_server_configs(workspace=workspace, config_snapshot=snapshot)[0]
    MCPTrustStore(trust_path).trust(workspace, first.name, first.fingerprint)
    executable.write_text("two", encoding="utf-8")
    changed = load_mcp_server_configs(workspace=workspace, config_snapshot=snapshot)[0]

    assert changed.fingerprint != first.fingerprint
    assert changed.trusted is False


def test_inline_workspace_remote_mcp_url_change_invalidates_trust(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs
    from nz_coder.mcp.trust import MCPTrustStore

    workspace, trust_path, first_snapshot = _inline_snapshot(
        tmp_path, monkeypatch, {"remote": {
            "type": "remote", "url": "https://one.example.test/mcp",
        }},
    )
    first = load_mcp_server_configs(
        workspace=workspace, config_snapshot=first_snapshot,
    )[0]
    MCPTrustStore(trust_path).trust(workspace, first.name, first.fingerprint)
    workspace.joinpath(".env").write_text(
        "NZ_MCP_ENABLED=1\nNZ_MCP_SERVERS_JSON="
        + json.dumps({"servers": {"remote": {
            "type": "remote", "url": "https://two.example.test/mcp",
        }}}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    environment = {
        "NZ_MCP_TRUST_STORE": str(trust_path),
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(tmp_path / "workspace-trust.json"),
    }
    untrusted = load_config_snapshot(workspace, environ=environment)
    WorkspaceTrustStore(tmp_path / "workspace-trust.json").trust(
        workspace, "workspace-config", untrusted.workspace_fingerprint,
    )
    changed_snapshot = load_config_snapshot(workspace, environ=environment)
    changed = load_mcp_server_configs(
        workspace=workspace, config_snapshot=changed_snapshot,
    )[0]

    assert changed.fingerprint != first.fingerprint
    assert changed.trusted is False


def test_inline_workspace_header_env_change_invalidates_trust(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs
    from nz_coder.mcp.trust import MCPTrustStore

    workspace, trust_path, snapshot = _inline_snapshot(
        tmp_path, monkeypatch, {"remote": {
            "type": "remote",
            "url": "https://mcp.example.test/service",
            "header_env": {"Authorization": "TOKEN_ONE"},
        }},
    )
    first = load_mcp_server_configs(workspace=workspace, config_snapshot=snapshot)[0]
    MCPTrustStore(trust_path).trust(workspace, first.name, first.fingerprint)
    workspace.joinpath(".env").write_text(
        "NZ_MCP_ENABLED=1\nNZ_MCP_SERVERS_JSON=" + json.dumps({
            "servers": {"remote": {
                "type": "remote",
                "url": "https://mcp.example.test/service",
                "header_env": {"Authorization": "TOKEN_TWO"},
            }}
        }, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    environment = {
        "NZ_MCP_TRUST_STORE": str(trust_path),
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(tmp_path / "workspace-trust.json"),
    }
    pending = load_config_snapshot(workspace, environ=environment)
    WorkspaceTrustStore(tmp_path / "workspace-trust.json").trust(
        workspace, "workspace-config", pending.workspace_fingerprint,
    )
    changed_snapshot = load_config_snapshot(workspace, environ=environment)
    changed = load_mcp_server_configs(
        workspace=workspace, config_snapshot=changed_snapshot,
    )[0]

    assert changed.fingerprint != first.fingerprint
    assert changed.trusted is False


def test_mcp_cli_can_trust_trusted_workspace_source(tmp_path, monkeypatch, capsys):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.cli import mcp_main
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace, _trust_path, _snapshot = _inline_snapshot(
        tmp_path, monkeypatch, {"inline": {"command": ["server"]}},
    )
    monkeypatch.chdir(workspace)

    assert mcp_main(["trust", "inline"]) == 0
    assert "trusted" in capsys.readouterr().out
    current = load_config_snapshot(workspace)
    assert load_mcp_server_configs(
        workspace=workspace, config_snapshot=current,
    )[0].trusted is True


def test_os_environment_mcp_remains_host_trusted(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = load_config_snapshot(workspace, environ={
        "NZ_MCP_SERVERS_JSON": json.dumps({
            "servers": {"host": {"command": ["host-server"]}},
        }),
    })
    server = load_mcp_server_configs(
        workspace=workspace, config_snapshot=snapshot,
    )[0]

    assert (server.source, server.trusted, server.fingerprint) == (
        "environment", True, "",
    )


def test_user_mcp_remains_user_trusted(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_mcp = tmp_path / "user" / "mcp.json"
    user_mcp.parent.mkdir()
    user_mcp.write_text(json.dumps({
        "servers": {"user": {"command": ["user-server"]}},
    }), encoding="utf-8")
    snapshot = load_config_snapshot(workspace, environ={
        "NZ_MCP_USER_CONFIG": str(user_mcp),
    })
    server = load_mcp_server_configs(
        workspace=workspace, config_snapshot=snapshot,
    )[0]

    assert (server.source, server.trusted, server.fingerprint) == (
        "user", True, "",
    )
