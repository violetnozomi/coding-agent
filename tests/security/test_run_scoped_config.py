"""Run-scoped configuration must follow the target workspace snapshot."""
from __future__ import annotations

import json
import io
from pathlib import Path
from types import SimpleNamespace


def _snapshot(tmp_path: Path, workspace: Path, values: dict[str, str]):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    workspace.mkdir(parents=True, exist_ok=True)
    return load_config_snapshot(
        workspace,
        environ=values,
        user_config_path=tmp_path / "missing-user.env",
        trust_store=WorkspaceTrustStore(tmp_path / "workspace-trust.json"),
    )


def test_provider_endpoint_does_not_bleed_between_workspaces(tmp_path):
    from nz_coder.providers.configuration import provider_connection

    first = _snapshot(tmp_path, tmp_path / "a", {
        "API_KEY": "first-secret",
        "API_BASE_URL": "https://first.invalid/v1",
    })
    second = _snapshot(tmp_path, tmp_path / "b", {
        "API_KEY": "second-secret",
        "API_BASE_URL": "https://second.invalid/v1",
    })

    first_connection = provider_connection("openai-compatible", config_snapshot=first)
    second_connection = provider_connection("openai-compatible", config_snapshot=second)

    assert first_connection.base_url == "https://first.invalid/v1"
    assert second_connection.base_url == "https://second.invalid/v1"
    assert second_connection.api_key == "second-secret"
    assert "second-secret" not in second_connection.credential_scope_id


def test_model_fallback_uses_target_workspace_snapshot(tmp_path):
    from nz_coder.providers.models import active_model_selection

    first = _snapshot(tmp_path, tmp_path / "a", {
        "MODEL_PROVIDER": "gemini",
        "MODEL_ID": "model-a",
        "MODEL_VARIANT": "high",
    })
    second = _snapshot(tmp_path, tmp_path / "b", {
        "MODEL_PROVIDER": "anthropic",
        "MODEL_ID": "model-b",
    })

    assert active_model_selection(first.workspace, config_snapshot=first).model_id == "model-a"
    selected = active_model_selection(second.workspace, config_snapshot=second)
    assert (selected.provider, selected.model_id, selected.variant) == (
        "anthropic", "model-b", None,
    )


def test_mcp_servers_json_and_timeouts_use_target_workspace_snapshot(tmp_path):
    from nz_coder.mcp.config import load_mcp_server_configs

    first = _snapshot(tmp_path, tmp_path / "a", {
        "NZ_MCP_SERVERS_JSON": json.dumps({
            "servers": {"first": {"command": ["first-server"]}}
        }),
        "NZ_MCP_STARTUP_TIMEOUT_SECONDS": "7",
        "NZ_MCP_TOOL_TIMEOUT_SECONDS": "9",
    })
    second = _snapshot(tmp_path, tmp_path / "b", {
        "NZ_MCP_SERVERS_JSON": json.dumps({
            "servers": {"second": {"command": ["second-server"]}}
        }),
        "NZ_MCP_STARTUP_TIMEOUT_SECONDS": "11",
        "NZ_MCP_TOOL_TIMEOUT_SECONDS": "13",
    })

    assert [item.name for item in load_mcp_server_configs(
        workspace=first.workspace, config_snapshot=first,
    )] == ["first"]
    servers = load_mcp_server_configs(
        workspace=second.workspace,
        config_snapshot=second,
    )

    assert [(item.name, item.source) for item in servers] == [("second", "environment")]
    assert servers[0].startup_timeout_seconds == 11
    assert servers[0].tool_timeout_seconds == 13
    assert all(item.name != "first" for item in servers)


def test_mcp_enabled_uses_target_workspace_snapshot(tmp_path):
    from nz_coder.mcp.runtime import MCPRuntime

    disabled = _snapshot(tmp_path, tmp_path / "disabled", {"NZ_MCP_ENABLED": "0"})
    enabled = _snapshot(tmp_path, tmp_path / "enabled", {
        "NZ_MCP_ENABLED": "1",
        "NZ_MCP_SERVERS_JSON": json.dumps({
            "servers": {"enabled": {"command": ["server"]}}
        }),
    })

    assert MCPRuntime.configured(
        workspace=disabled.workspace, config_snapshot=disabled,
    ).configs == []
    assert [item.name for item in MCPRuntime.configured(
        workspace=enabled.workspace, config_snapshot=enabled,
    ).configs] == ["enabled"]


def test_lsp_enabled_and_timeouts_use_target_workspace_snapshot(tmp_path, monkeypatch):
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import ResolvedServer

    workspace = tmp_path / "target"
    source = workspace / "app.py"
    source.parent.mkdir()
    source.write_text("answer = 42\n", encoding="utf-8")
    disabled = _snapshot(tmp_path, workspace, {"NZ_LSP_ENABLED": "0"})
    enabled = _snapshot(tmp_path, workspace, {
        "NZ_LSP_ENABLED": "1",
        "NZ_LSP_INITIALIZE_TIMEOUT_SECONDS": "17",
        "NZ_LSP_REQUEST_TIMEOUT_SECONDS": "19",
    })
    created = []

    class Client:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.process = SimpleNamespace(poll=lambda: None)
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(manager, "resolve_server", lambda *_args, **_kwargs: ResolvedServer(
        server_id="fake",
        language_id="python",
        command=("fake-lsp",),
        root=workspace,
        source="system",
        config_source="environment-config",
        fingerprint="server",
    ))
    monkeypatch.setattr(manager, "LSPClient", Client)
    manager.close_all_clients()
    try:
        assert manager.get_client_for_file(
            source, workspace, config_snapshot=disabled,
        ) is None
        first_client = manager.get_client_for_file(
            source, workspace, config_snapshot=enabled,
        )
        assert first_client is not None
        assert created[0]["initialize_timeout"] == 17
        assert created[0]["request_timeout"] == 19
        rotated = _snapshot(tmp_path, workspace, {
            "NZ_LSP_ENABLED": "1",
            "NZ_LSP_INITIALIZE_TIMEOUT_SECONDS": "23",
            "NZ_LSP_REQUEST_TIMEOUT_SECONDS": "29",
        })
        assert manager.get_client_for_file(
            source, workspace, config_snapshot=rotated,
        ) is not first_client
        assert first_client.closed is True
    finally:
        manager.close_all_clients()


def test_invalid_numeric_issue_does_not_include_secret_like_value(
    tmp_path, monkeypatch,
):
    from nz_coder.interface.setup.doctor import collect_doctor_checks

    secret = "sk-secret-invalid-float"
    integer_secret = "sk-secret-invalid-integer"
    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "NZ_MCP_STARTUP_TIMEOUT_SECONDS": secret,
        "MAX_AGENT_TURNS": integer_secret,
    })

    assert snapshot.get_float("NZ_MCP_STARTUP_TIMEOUT_SECONDS", 30.0) == 30.0
    # load_config_snapshot validates this registered key eagerly with its
    # schema/runtime default before callers inspect the snapshot.
    assert snapshot.get_int("MAX_AGENT_TURNS", 100) == 500
    assert all(
        secret not in issue.message and integer_secret not in issue.message
        for issue in snapshot.issues
    )
    assert secret not in snapshot.public_json()
    assert integer_secret not in snapshot.public_json()
    monkeypatch.setenv("NZ_MCP_STARTUP_TIMEOUT_SECONDS", secret)
    monkeypatch.setenv("MAX_AGENT_TURNS", integer_secret)
    doctor_output = repr(collect_doctor_checks(tmp_path / "target"))
    assert secret not in doctor_output
    assert integer_secret not in doctor_output


def test_workspace_mcp_config_keeps_workspace_provenance(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".env").write_text(
        "NZ_MCP_SERVERS_JSON=" + json.dumps({
            "servers": {"project-env": {"command": ["server"]}}
        }, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    store = WorkspaceTrustStore(tmp_path / "trust.json")
    initial = load_config_snapshot(
        workspace, environ={}, user_config_path=tmp_path / "missing", trust_store=store,
    )
    store.trust(workspace, "workspace-config", initial.workspace_fingerprint)
    snapshot = load_config_snapshot(
        workspace, environ={}, user_config_path=tmp_path / "missing", trust_store=store,
    )

    server = load_mcp_server_configs(
        workspace=workspace, config_snapshot=snapshot,
    )[0]
    assert server.source == "trusted-workspace"


def test_headless_target_workspace_does_not_inherit_startup_workspace_config(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        current_config_snapshot,
        load_config_snapshot,
    )
    from nz_coder.interface.headless import run_main
    from nz_coder.providers.configuration import provider_connection
    from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage

    startup = tmp_path / "startup"
    target = tmp_path / "target"
    startup.mkdir()
    target.mkdir()
    target.joinpath(".env").write_text(
        "API_BASE_URL=https://target.invalid/v1\n", encoding="utf-8",
    )
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    monkeypatch.delenv("API_BASE_URL", raising=False)
    initial = load_config_snapshot(target)
    WorkspaceTrustStore(trust_path).trust(
        target, "workspace-config", initial.workspace_fingerprint,
    )
    monkeypatch.setattr(config, "API_BASE_URL", "https://startup.invalid/v1")
    seen = []

    class Client:
        async def run(self, request, **_kwargs):
            snapshot = current_config_snapshot(request.workspace)
            seen.append(provider_connection(
                "openai-compatible", config_snapshot=snapshot,
            ).base_url)
            return RunResult(
                status=RunStatus.COMPLETED,
                final_text="done",
                messages=request.messages,
                usage=TokenUsage(),
                session_id=request.session_id,
                active_agent=request.agent.name,
            )

    code = run_main(
        ["--cwd", str(target), "inspect"],
        stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(),
        client_factory=Client,
    )
    assert code == 0
    assert seen == ["https://target.invalid/v1"]
