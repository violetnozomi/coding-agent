"""Credential-safe environment contracts for child processes."""
from __future__ import annotations

import subprocess
import sys


def test_sanitized_environment_keeps_runtime_basics_and_removes_credentials():
    from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env

    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "LANG": "C.UTF-8",
        "PYTHONPATH": "/repo/src",
        "API_KEY": "sentinel-provider-secret",
        "OPENAI_API_KEY": "sentinel-openai-secret",
        "AWS_SECRET_ACCESS_KEY": "sentinel-cloud-secret",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GITHUB_TOKEN": "sentinel-ci-secret",
        "NPM_TOKEN": "sentinel-registry-secret",
        "MCP_OAUTH_TOKEN": "sentinel-mcp-secret",
        "UNRELATED_CUSTOM": "not-required",
    }

    result = build_sanitized_subprocess_env(source=source)

    assert result == {
        "HOME": "/home/test",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin",
        "PYTHONPATH": "/repo/src",
    }
    assert not any("sentinel" in value for value in result.values())


def test_explicit_low_risk_override_is_allowed_but_secret_name_is_rejected():
    from nz_coder.foundation.subprocess_env import (
        UnsafeSubprocessEnvironment,
        build_sanitized_subprocess_env,
    )

    result = build_sanitized_subprocess_env(
        source={"PATH": "/usr/bin"},
        overrides={"MCP_MODE": "test"},
    )
    assert result["MCP_MODE"] == "test"

    try:
        build_sanitized_subprocess_env(
            source={"PATH": "/usr/bin"},
            overrides={"OPENAI_API_KEY": "sentinel"},
        )
    except UnsafeSubprocessEnvironment as exc:
        assert "OPENAI_API_KEY" in str(exc)
        assert "sentinel" not in str(exc)
    else:
        raise AssertionError("credential-like child override must fail closed")


def test_mcp_client_subprocess_does_not_receive_provider_secret(tmp_path, monkeypatch):
    from nz_coder.mcp.client import MCPClient, MCPError

    captured_keys: set[str] = set()
    secret_seen = False

    def fake_popen(*_args, **kwargs):
        nonlocal secret_seen
        environment = kwargs.get("env") or {}
        captured_keys.update(environment)
        secret_seen = "sentinel-provider-secret" in environment.values()
        raise OSError("expected test stop")

    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-provider-secret")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    client = MCPClient(name="safe", command=(sys.executable, "server.py"), cwd=tmp_path)

    try:
        client.start()
    except MCPError:
        pass

    assert "OPENAI_API_KEY" not in captured_keys
    assert secret_seen is False


def test_lsp_client_subprocess_does_not_receive_provider_secret(tmp_path, monkeypatch):
    from nz_coder.lsp.client import LSPClient, LSPError

    captured_keys: set[str] = set()
    secret_seen = False

    def fake_popen(*_args, **kwargs):
        nonlocal secret_seen
        environment = kwargs.get("env") or {}
        captured_keys.update(environment)
        secret_seen = "sentinel-provider-secret" in environment.values()
        raise OSError("expected test stop")

    monkeypatch.setenv("API_KEY", "sentinel-provider-secret")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    try:
        LSPClient(
            server_id="safe",
            command=(sys.executable, "server.py"),
            root=tmp_path,
            language_id="python",
        )
    except LSPError:
        pass

    assert "API_KEY" not in captured_keys
    assert secret_seen is False
