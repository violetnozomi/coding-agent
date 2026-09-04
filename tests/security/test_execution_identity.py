"""Configuration-source and executable-payload identity security contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _write_server(path: Path, marker: str) -> None:
    path.write_text(f"print({marker!r})\n", encoding="utf-8")


def test_workspace_cannot_override_mcp_user_config_path(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    injected = workspace / "attacker.json"
    (workspace / ".env").write_text(
        "NZ_MCP_USER_CONFIG=attacker.json\n", encoding="utf-8",
    )
    trust = WorkspaceTrustStore(tmp_path / "user" / "trust.json")
    pending = load_config_snapshot(workspace, environ={}, trust_store=trust)
    trust.trust(workspace, "workspace-config", pending.workspace_fingerprint)

    snapshot = load_config_snapshot(workspace, environ={}, trust_store=trust)

    assert snapshot.value("NZ_MCP_USER_CONFIG").source.value == "default"
    assert snapshot.get("NZ_MCP_USER_CONFIG") != str(injected)
    assert any(issue.key == "NZ_MCP_USER_CONFIG" for issue in snapshot.issues)


def test_workspace_cannot_override_mcp_trust_store_path(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "NZ_MCP_TRUST_STORE=.nz-coder/fake-trust.json\n", encoding="utf-8",
    )
    trust = WorkspaceTrustStore(tmp_path / "user" / "trust.json")
    pending = load_config_snapshot(workspace, environ={}, trust_store=trust)
    trust.trust(workspace, "workspace-config", pending.workspace_fingerprint)

    snapshot = load_config_snapshot(workspace, environ={}, trust_store=trust)

    assert snapshot.value("NZ_MCP_TRUST_STORE").source.value == "default"
    assert any(issue.key == "NZ_MCP_TRUST_STORE" for issue in snapshot.issues)


def test_mcp_trust_store_read_rejects_workspace_path(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.config import mcp_config_paths

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = load_config_snapshot(workspace, environ={
        "NZ_MCP_TRUST_STORE": str(workspace / "trust.json"),
    })

    with pytest.raises(ValueError, match="outside the workspace"):
        mcp_config_paths(workspace, config_snapshot=snapshot)


@pytest.mark.parametrize(
    ("command", "payload_name"),
    [
        (("python", "server.py"), "server.py"),
        (("node", "server.js"), "server.js"),
        (("bash", "server.sh"), "server.sh"),
        (("pwsh", "-File", "server.ps1"), "server.ps1"),
        (("java", "-jar", "server.jar"), "server.jar"),
        (("dotnet", "server.dll"), "server.dll"),
    ],
)
def test_execution_script_change_rotates_identity(tmp_path, monkeypatch, command, payload_name):
    from nz_coder.foundation.execution_identity import resolve_execution_identity

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = workspace / payload_name
    _write_server(payload, "one")
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    first = resolve_execution_identity(
        command, cwd=workspace, workspace=workspace,
        config_source="user-config", environment_profile="strict-service",
    )
    _write_server(payload, "two")
    second = resolve_execution_identity(
        command, cwd=workspace, workspace=workspace,
        config_source="user-config", environment_profile="strict-service",
    )

    assert first.workspace_controlled is True
    assert first.entrypoint_path == payload.resolve()
    assert second.fingerprint != first.fingerprint


def test_python_module_mcp_change_invalidates_trust(tmp_path, monkeypatch):
    from nz_coder.foundation.execution_identity import resolve_execution_identity

    workspace = tmp_path / "workspace"
    package = workspace / "workspace_module"
    package.mkdir(parents=True)
    entrypoint = package / "__main__.py"
    _write_server(entrypoint, "one")
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda _name: "/usr/bin/python",
    )
    first = resolve_execution_identity(
        ("python", "-m", "workspace_module"), cwd=workspace,
        workspace=workspace, config_source="user-config",
        environment_profile="strict-service",
    )
    _write_server(entrypoint, "two")
    second = resolve_execution_identity(
        ("python", "-m", "workspace_module"), cwd=workspace,
        workspace=workspace, config_source="user-config",
        environment_profile="strict-service",
    )

    assert first.entrypoint_kind == "python-module"
    assert first.workspace_controlled is True
    assert second.fingerprint != first.fingerprint


def test_user_config_system_python_does_not_auto_trust_workspace_module(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs
    from nz_coder.mcp.trust import MCPTrustStore

    workspace = tmp_path / "workspace"
    package = workspace / "workspace_module"
    package.mkdir(parents=True)
    _write_server(package / "__main__.py", "one")
    user_config = tmp_path / "user" / "mcp.json"
    user_config.parent.mkdir()
    user_config.write_text(json.dumps({
        "module": {"command": ["python", "-m", "workspace_module"]},
    }), encoding="utf-8")
    trust_path = tmp_path / "user" / "mcp-trust.json"
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda _name: "/usr/bin/python",
    )
    snapshot = load_config_snapshot(workspace, environ={
        "NZ_MCP_USER_CONFIG": str(user_config),
        "NZ_MCP_TRUST_STORE": str(trust_path),
    })

    pending = load_mcp_server_configs(
        workspace=workspace, config_snapshot=snapshot,
    )[0]
    assert pending.source == "user"
    assert pending.execution_identity is not None
    assert pending.execution_identity.workspace_controlled is True
    assert pending.trusted is False

    MCPTrustStore(trust_path).trust(workspace, pending.name, pending.fingerprint)
    trusted = load_mcp_server_configs(
        workspace=workspace, config_snapshot=snapshot,
    )[0]
    assert trusted.trusted is True


def test_lsp_workspace_module_requires_execution_trust(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.lsp.servers import resolve_server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "main.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    package = workspace / "workspace_lsp"
    package.mkdir()
    _write_server(package / "__main__.py", "one")
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda _name: "/usr/bin/python",
    )
    snapshot = load_config_snapshot(workspace, environ={
        "NZ_LSP_PYTHON_COMMAND": "python -m workspace_lsp",
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(tmp_path / "user" / "trust.json"),
    })

    server = resolve_server(source, workspace, config_snapshot=snapshot)

    assert server is not None
    assert server.source == "workspace"
    assert server.trusted is False
    assert server.execution_identity is not None
    assert server.execution_identity.entrypoint_kind == "python-module"


@pytest.mark.parametrize("command", [("python", "-c", "pass"), ("node", "-e", "0"), ("sh", "-c", "true")])
def test_ambiguous_inline_execution_fails_closed(tmp_path, monkeypatch, command):
    from nz_coder.foundation.execution_identity import UnsafeExecutionIdentity, resolve_execution_identity

    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    with pytest.raises(UnsafeExecutionIdentity):
        resolve_execution_identity(
            command, cwd=tmp_path, workspace=tmp_path,
            config_source="user-config", environment_profile="strict-service",
        )


def test_remote_mcp_query_change_invalidates_trust(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_config = workspace / ".nz-coder" / "mcp.json"
    project_config.parent.mkdir()

    def server(query: str):
        project_config.write_text(json.dumps({"remote": {
                "type": "remote", "url": f"https://example.invalid/mcp?{query}",
            }}), encoding="utf-8")
        snapshot = load_config_snapshot(workspace, environ={})
        return load_mcp_server_configs(workspace=workspace, config_snapshot=snapshot)[0]

    assert server("tenant=a").fingerprint != server("tenant=b").fingerprint


def test_execution_payload_swap_before_launch_fails_closed(tmp_path, monkeypatch):
    from nz_coder.foundation.execution_identity import (
        UnsafeExecutionIdentity,
        resolve_execution_identity,
        verify_execution_identity,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "server.py"
    _write_server(script, "one")
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda _name: os.fspath(Path(os.__file__).parent / "python"),
    )
    identity = resolve_execution_identity(
        ("python", "server.py"), cwd=workspace, workspace=workspace,
        config_source="project", environment_profile="strict-service",
    )
    _write_server(script, "swapped")

    with pytest.raises(UnsafeExecutionIdentity):
        verify_execution_identity(identity, workspace=workspace)


@pytest.mark.parametrize(
    "command",
    [
        ("python", "-W", "ignore", "server.py"),
        ("python", "-X", "utf8", "server.py"),
        ("bash", "-O", "extglob", "server.sh"),
        ("pwsh", "-ExecutionPolicy", "Bypass", "-File", "server.ps1"),
    ],
)
def test_interpreter_option_arguments_are_not_code_payloads(
    tmp_path, monkeypatch, command,
):
    """An option value must not displace the actual script identity."""
    from nz_coder.foundation.execution_identity import resolve_execution_identity

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / command[-1]
    _write_server(script, "one")
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    identity = resolve_execution_identity(
        command, cwd=workspace, workspace=workspace,
        config_source="project", environment_profile="strict-service",
    )

    assert identity.entrypoint_path == script.resolve()


@pytest.mark.parametrize(
    ("hook_flag", "hook_name"),
    [("--require", "preload.js"), ("--loader", "loader.js")],
)
def test_node_hook_and_main_are_both_content_bound(
    tmp_path, monkeypatch, hook_flag, hook_name,
):
    """Changing an explicit Node preload/loader must rotate the identity."""
    from nz_coder.foundation.execution_identity import resolve_execution_identity

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook = workspace / hook_name
    main = workspace / "main.js"
    _write_server(hook, "hook-one")
    _write_server(main, "main")
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda _name: "/usr/bin/node",
    )
    command = ("node", hook_flag, hook_name, "main.js")
    first = resolve_execution_identity(
        command, cwd=workspace, workspace=workspace,
        config_source="project", environment_profile="strict-service",
    )
    _write_server(hook, "hook-two")
    second = resolve_execution_identity(
        command, cwd=workspace, workspace=workspace,
        config_source="project", environment_profile="strict-service",
    )

    assert second.fingerprint != first.fingerprint


def test_execution_identity_never_follows_payload_symlink(tmp_path, monkeypatch):
    """A code payload symlink cannot redirect trust to an arbitrary target."""
    from nz_coder.foundation.execution_identity import (
        UnsafeExecutionIdentity,
        resolve_execution_identity,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    _write_server(outside, "secret")
    (workspace / "server.py").symlink_to(outside)
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda _name: "/usr/bin/python",
    )

    with pytest.raises(UnsafeExecutionIdentity, match="symlink"):
        resolve_execution_identity(
            ("python", "server.py"), cwd=workspace, workspace=workspace,
            config_source="project", environment_profile="strict-service",
        )


def test_execution_identity_repr_hides_command_arguments(tmp_path, monkeypatch):
    """Credential-like command arguments never enter dataclass repr output."""
    from nz_coder.foundation.execution_identity import resolve_execution_identity

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_server(workspace / "server.py", "main")
    monkeypatch.setattr(
        "nz_coder.foundation.execution_identity.shutil.which",
        lambda _name: "/usr/bin/python",
    )
    secret = "SENTINEL-COMMAND-SECRET"
    identity = resolve_execution_identity(
        ("python", "server.py", "--token", secret),
        cwd=workspace, workspace=workspace,
        config_source="project", environment_profile="strict-service",
    )

    assert secret not in repr(identity)
