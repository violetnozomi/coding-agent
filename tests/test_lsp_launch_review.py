"""独立审查反例：PATH 的执行目录与多启动环境的精确授权。"""
import json
import os
import subprocess
import sys

import pytest

from nz_coder.foundation.execution_identity import verify_execution_identity
from nz_coder.foundation.workspace_trust import WorkspaceTrustStore
from nz_coder.lsp import servers
from nz_coder.runtime.process.execution_environment import ExecutionEnvironment, scoped_execution_environment
from tests.test_lsp_launch_environment import _program, _snapshot, _source


@pytest.mark.parametrize(("path_text", "json_override", "use_env_shebang"), [
    ("bin", False, False),
    ("", False, False),
    (os.pathsep + "bin" + os.pathsep, False, False),
    ("bin", True, False),
    ("bin", True, True),
])
def test_relative_path_uses_service_root_for_discovery_identity_and_real_spawn(
    tmp_path, monkeypatch, path_text, json_override, use_env_shebang,
):
    root = tmp_path / "child"
    source = _source(root)
    bindir = root / "bin" if "bin" in path_text else root
    if use_env_shebang:
        _program(bindir / "relative-lsp", "#!/usr/bin/env python\nprint('child-output')\n")
        (bindir / "python").symlink_to(sys.executable)
    else:
        _program(bindir / "relative-lsp", "#!/bin/sh\nprintf 'child-output'\n")
    host = tmp_path / "host"
    _program(host / "bin/relative-lsp", "#!/bin/sh\nprintf 'host-output'\n")
    monkeypatch.chdir(host)
    profile = {"python": {"env": {"PATH": path_text}}} if json_override else {}
    prepared = ExecutionEnvironment.capture(source={"PATH": "/unavailable" if json_override else path_text})
    with scoped_execution_environment(prepared):
        server = servers.resolve_server(source, root, config_snapshot=_snapshot(root, "relative-lsp", profile))
    assert server is not None, "child executable must be discoverable under its own cwd"
    expected = os.pathsep.join(str(root / part) if part else str(root) for part in path_text.split(os.pathsep))
    assert dict(server.environment)["PATH"] == expected
    assert dict(server.execution_identity.environment)["PATH"] == expected
    monkeypatch.setenv("PATH", str(host / "bin"))
    assert verify_execution_identity(server.execution_identity).fingerprint == server.fingerprint
    result = subprocess.run(server.command, cwd=server.root, env=dict(server.environment), capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert result.stdout.strip() == "child-output"


def test_two_environment_grants_coexist_and_untrust_selects_only_current_profile(tmp_path, monkeypatch):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / "tools/lsp")
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))
    snapshot = _snapshot(root, str(tool))
    environments = [ExecutionEnvironment.capture(source={"PATH": str(tool.parent) + os.pathsep + suffix}) for suffix in ("/one", "/two")]
    for environment in environments:
        with scoped_execution_environment(environment):
            assert servers.trust_server(source, root, config_snapshot=snapshot).trusted
    with scoped_execution_environment(environments[0]):
        first = servers.resolve_server(source, root, config_snapshot=snapshot)
        assert first.trusted, "approving environment B must preserve environment A"
        assert servers.untrust_server(source, root, config_snapshot=snapshot)
        assert not servers.resolve_server(source, root, config_snapshot=snapshot).trusted
    with scoped_execution_environment(environments[1]):
        second = servers.resolve_server(source, root, config_snapshot=snapshot)
        assert second.trusted, "revoking A must preserve B"
    assert servers.server_trust_kind(source, first) != servers.server_trust_kind(source, second)


def test_equal_environment_different_service_roots_have_independent_grants(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    one, two = _source(workspace / "one"), _source(workspace / "two")
    for directory in (one.parent, two.parent):
        (directory / "pyproject.toml").write_text("")
    tool = _program(workspace / "tools/lsp")
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))
    profile = {"python": {"env": {key: "/shared-cache" for key in ("TMPDIR", "TMP", "TEMP")}}}
    snapshot = _snapshot(workspace, str(tool), profile)
    prepared = ExecutionEnvironment.capture(source={"PATH": str(tool.parent)})
    with scoped_execution_environment(prepared):
        first = servers.trust_server(one, workspace, config_snapshot=snapshot)
        second = servers.trust_server(two, workspace, config_snapshot=snapshot)
        assert first.environment_fingerprint == second.environment_fingerprint
        assert servers.resolve_server(one, workspace, config_snapshot=snapshot).trusted
        assert servers.resolve_server(two, workspace, config_snapshot=snapshot).trusted
    assert servers.server_trust_kind(one, first) != servers.server_trust_kind(two, second)


def test_payload_edit_invalidates_grant_without_creating_new_scope(tmp_path, monkeypatch):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / "tools/lsp", "payload-one")
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))
    snapshot = _snapshot(root, str(tool))
    assert hasattr(servers, "server_trust_kind"), "one stable LSP scope must serve all trust operations"
    first = servers.trust_server(source, root, config_snapshot=snapshot)
    tool.write_text("payload-two")
    second = servers.resolve_server(source, root, config_snapshot=snapshot)
    assert not second.trusted
    assert first.fingerprint != second.fingerprint
    assert servers.server_trust_kind(source, first) == servers.server_trust_kind(source, second)


def test_same_interpreter_can_authorize_two_explicit_script_profiles(tmp_path, monkeypatch):
    root = tmp_path / "child"
    source = _source(root)
    scripts = [_program(root / name, "pass\n") for name in ("one.py", "two.py")]
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))
    snapshots = [_snapshot(root, f"{sys.executable} {script}") for script in scripts]
    granted = [servers.trust_server(source, root, config_snapshot=snapshot) for snapshot in snapshots]
    assert granted[0].command[0] == granted[1].command[0]
    assert granted[0].environment_fingerprint == granted[1].environment_fingerprint
    assert all(servers.resolve_server(source, root, config_snapshot=snapshot).trusted for snapshot in snapshots)
    assert servers.server_trust_kind(source, granted[0]) != servers.server_trust_kind(source, granted[1])


def test_old_unscoped_grant_does_not_authorize_new_profile(tmp_path, monkeypatch):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / "tools/lsp")
    store = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(store))
    snapshot = _snapshot(root, str(tool))
    candidate = servers.resolve_server(source, root, config_snapshot=snapshot)
    WorkspaceTrustStore(store).trust(root, "lsp:python", candidate.fingerprint, executable=str(tool))
    assert not servers.resolve_server(source, root, config_snapshot=snapshot).trusted


@pytest.mark.parametrize("explicit_interpreter", [False, True])
def test_per_file_analysis_state_uses_captured_launch_profile_and_live_database(tmp_path, explicit_interpreter):
    root = tmp_path / "child"
    one, two = _source(root, "one.cpp"), _source(root, "two.cpp")
    tool = _program(tmp_path / "bin/clangd")
    build = root / "build"
    build.mkdir()
    database = build / "compile_commands.json"
    database.write_text(json.dumps([{"directory": str(root), "file": str(one), "arguments": ["c++", "-c", str(one)]}]))
    command = f"{sys.executable} {tool}" if explicit_interpreter else str(tool)
    profile = servers.resolve_server(one, root, config_snapshot=_snapshot(root, f"{command} --compile-commands-dir={{root}}/build", language="cpp"))
    assert hasattr(servers, "analysis_environment_for_file"), "reports require the current file's analysis state"
    assert profile.analysis_environment == "configured"
    assert servers.analysis_environment_for_file(profile, one, root) == "configured"
    assert servers.analysis_environment_for_file(profile, two, root) == "unconfigured"
    database.unlink()
    assert servers.analysis_environment_for_file(profile, one, root) == "unconfigured"
    assert profile.analysis_environment == "configured", "a query must not mutate the shared launch profile"
