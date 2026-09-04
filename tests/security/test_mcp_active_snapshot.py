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
