"""Run-scoped MCP configuration must never fall into startup globals implicitly."""
from __future__ import annotations

import json


def _inline_server(marker: str) -> str:
    return json.dumps({marker: {"command": ["python", "server.py"]}})


def test_mcp_implicit_call_uses_active_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.foundation.workspace_trust import (
        load_config_snapshot,
        scoped_config_snapshot,
    )
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "server.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(config, "MCP_SERVERS_JSON", _inline_server("startup"))
    snapshot = load_config_snapshot(
        workspace, environ={"NZ_MCP_SERVERS_JSON": _inline_server("active")},
    )

    with scoped_config_snapshot(snapshot):
        configs = load_mcp_server_configs(workspace=workspace)

    assert [item.name for item in configs] == ["active"]


def test_mcp_legacy_globals_require_explicit_compatibility_mode(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "server.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(config, "MCP_SERVERS_JSON", _inline_server("legacy"))

    normal = load_mcp_server_configs(workspace=workspace)
    legacy = load_mcp_server_configs(
        workspace=workspace, compatibility_mode=True,
    )

    assert normal == []
    assert [item.name for item in legacy] == ["legacy"]


class _EmptySkillLoader:
    def list_skills(self):
        return []

    def reload(self):
        return None


def _mcp_extension_names(registry):
    return [
        item.name for item in registry.snapshot()
        if item.kind == "mcp_server"
    ]


def test_mcp_extension_projection_uses_active_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.foundation.workspace_trust import (
        load_config_snapshot,
        scoped_config_snapshot,
    )
    from nz_coder.extensions.registry import ExtensionRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "server.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(config, "MCP_SERVERS_JSON", _inline_server("startup"))
    snapshot = load_config_snapshot(
        workspace, environ={"NZ_MCP_SERVERS_JSON": _inline_server("active")},
    )

    with scoped_config_snapshot(snapshot):
        registry = ExtensionRegistry(
            workspace=workspace,
            skill_loader=_EmptySkillLoader(),
            hook_loader=lambda _path: [],
        )
        names = _mcp_extension_names(registry)

    assert names == ["active"]


def test_mcp_reload_does_not_use_startup_workspace_globals(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.runtime import MCPRuntime

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "server.py").write_text("print('ok')\n", encoding="utf-8")
    snapshot = load_config_snapshot(workspace, environ={
        "NZ_MCP_ENABLED": "true",
        "NZ_MCP_SERVERS_JSON": _inline_server("active"),
    })
    runtime = MCPRuntime.configured(workspace=workspace, config_snapshot=snapshot)
    monkeypatch.setattr(config, "MCP_SERVERS_JSON", _inline_server("startup"))

    assert runtime.reload_config() is False
    assert [item.name for item in runtime.configs] == ["active"]


def test_mcp_a_workspace_config_does_not_appear_in_b_extension_status(tmp_path):
    from nz_coder.foundation.workspace_trust import (
        load_config_snapshot,
        scoped_config_snapshot,
    )
    from nz_coder.extensions.registry import ExtensionRegistry

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "server.py").write_text("print('a')\n", encoding="utf-8")
    (second / "server.py").write_text("print('b')\n", encoding="utf-8")
    first_snapshot = load_config_snapshot(
        first, environ={"NZ_MCP_SERVERS_JSON": _inline_server("server-a")},
    )
    second_snapshot = load_config_snapshot(
        second, environ={"NZ_MCP_SERVERS_JSON": _inline_server("server-b")},
    )

    with scoped_config_snapshot(first_snapshot):
        first_registry = ExtensionRegistry(
            workspace=first,
            skill_loader=_EmptySkillLoader(),
            hook_loader=lambda _path: [],
        )
    with scoped_config_snapshot(second_snapshot):
        second_registry = ExtensionRegistry(
            workspace=second,
            skill_loader=_EmptySkillLoader(),
            hook_loader=lambda _path: [],
        )

    assert _mcp_extension_names(first_registry) == ["server-a"]
    assert _mcp_extension_names(second_registry) == ["server-b"]
