"""同一身份绑定源入口、冻结环境与真实平台包装；不启动 Windows shell。"""
from __future__ import annotations

import os
import json
import sys
from pathlib import Path

import pytest

from nz_coder.foundation import execution_identity as identities
from nz_coder.foundation.execution_identity import UnsafeExecutionIdentity, resolve_execution_identity, verify_execution_identity
from nz_coder.runtime.process.platform_runtime import executable_argv


def _program(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


def _windows_adapter(monkeypatch):
    monkeypatch.setattr(identities, 'executable_argv',
                        lambda command, **kwargs: executable_argv(command, os_name='nt', **kwargs), raising=False)
    # 只替换平台选择，查找与包装仍走产品函数和真实临时文件。
    monkeypatch.setattr(identities, '_which_in_environment',
                        lambda value, cwd, environment: identities._windows_which(value, cwd, environment), raising=False)


@pytest.mark.parametrize('suffix', ['.cmd', '.bat', '.ps1'])
@pytest.mark.parametrize('local_wrapper', [False, True])
def test_source_and_wrapper_share_one_frozen_identity(tmp_path, monkeypatch, suffix, local_wrapper):
    _windows_adapter(monkeypatch)
    root = tmp_path / 'workspace'
    root.mkdir()
    source = _program((tmp_path if local_wrapper else root) / ('source with spaces' + suffix), 'echo original')
    binary = (root if local_wrapper else tmp_path) / 'bin'
    wrapper = _program(binary / ('pwsh.exe' if suffix == '.ps1' else 'cmd.exe'), 'wrapper-v1')
    env = {'PATH': str(binary), 'PATHEXT': '.EXE;.CMD;.BAT'}
    source_command = (str(source), 'argument with spaces')
    identity = resolve_execution_identity(source_command, cwd=root, workspace=root,
        config_source='user', environment_profile='strict-service', environment=env)
    assert identity.command == source_command
    assert identity.launch_command == executable_argv(
        source_command, os_name='nt', which=lambda value: identities._windows_which(value, root, env))
    assert identity.launch_command[0] == str(wrapper)
    assert identity.workspace_controlled
    monkeypatch.setenv('PATH', str(tmp_path / 'different'))
    monkeypatch.setenv('PATHEXT', '.POISON')
    monkeypatch.chdir(tmp_path)
    assert verify_execution_identity(identity).launch_command == identity.launch_command
    source.write_text('echo changed')
    with pytest.raises(UnsafeExecutionIdentity, match='changed'):
        verify_execution_identity(identity)
    source.write_text('echo original')
    wrapper.write_text('wrapper-v2')
    with pytest.raises(UnsafeExecutionIdentity, match='changed'):
        verify_execution_identity(identity)


def test_windows_lookup_uses_frozen_pathext_and_service_cwd(tmp_path, monkeypatch):
    root = tmp_path / 'service'
    root.mkdir()
    source = _program(root / 'bin/server.CMD', 'echo fixture')
    other = tmp_path / 'ambient'
    _program(other / 'bin/server.POISON', 'ambient')
    monkeypatch.chdir(other)
    monkeypatch.setenv('PATH', str(other / 'bin'))
    monkeypatch.setenv('PATHEXT', '.POISON')
    assert identities._windows_which('server', root, {'PATH': 'bin', 'PATHEXT': '.CMD'}) == str(source)


def test_identity_keeps_shebang_source_and_final_launch_separate(tmp_path):
    source = _program(tmp_path / 'server', f'#!{sys.executable}\nprint("fixture")\n')
    identity = resolve_execution_identity((str(source),), cwd=tmp_path, workspace=tmp_path,
        config_source='user', environment_profile='strict-service', environment=dict(os.environ))
    assert identity.command == (str(source),)
    assert identity.launch_command == (sys.executable, str(source))
    source.write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(UnsafeExecutionIdentity, match='changed'):
        verify_execution_identity(identity)


@pytest.mark.parametrize('consumer', ['mcp', 'lsp'])
@pytest.mark.parametrize('changed_entry', ['source', 'wrapper'])
def test_clients_spawn_verified_launch_without_rewrapping(tmp_path, monkeypatch, consumer, changed_entry):
    _windows_adapter(monkeypatch)
    root = tmp_path / 'workspace'
    root.mkdir()
    source = _program(root / 'server.cmd', 'echo fixture')
    _program(tmp_path / 'bin/cmd.exe', 'wrapper-v1')
    env = {'PATH': str(tmp_path / 'bin'), 'PATHEXT': '.EXE;.CMD'}
    identity = resolve_execution_identity((str(source),), cwd=root, workspace=root,
        config_source='user', environment_profile='strict-service', environment=env)
    captured = []
    def capture(command, **kwargs):
        captured.append((command, kwargs))
        raise OSError('controlled spawn boundary')
    if consumer == 'mcp':
        from nz_coder.mcp.client import MCPClient, MCPError
        monkeypatch.setattr('nz_coder.mcp.client.subprocess.Popen', capture)
        client = MCPClient(name='launch', command=identity.command, cwd=root, execution_identity=identity)
        error_type = MCPError
    else:
        from nz_coder.lsp.client import LSPClient, LSPError
        monkeypatch.setattr('nz_coder.lsp.client.subprocess.Popen', capture)
        client = LSPClient(server_id='launch', command=identity.launch_command, root=root,
            language_id='python', execution_identity=identity, environment=identity.environment)
        error_type = LSPError
    with pytest.raises(error_type):
        client.start()
    assert len(captured) == 1
    command, kwargs = captured[0]
    assert command == identity.launch_command
    assert dict(kwargs['env']) == dict(identity.environment)
    assert Path(kwargs['cwd']) == identity.cwd
    wrapper = Path(identity.launch_command[0])
    (source if changed_entry == 'source' else wrapper).write_text('changed-after-approval')
    captured.clear()
    with pytest.raises(error_type):
        client.start()
    assert captured == []


def test_untrusted_windows_source_never_reaches_runtime_spawn(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs
    from nz_coder.mcp.runtime import MCPRuntime

    _windows_adapter(monkeypatch)
    root = tmp_path / "workspace"
    root.mkdir()
    source = _program(root / "server.cmd", "echo fixture")
    wrapper = _program(tmp_path / "bin/cmd.exe", "wrapper-v1")
    snapshot = load_config_snapshot(root, environ={
        "NZ_MCP_SERVERS_JSON": json.dumps({"local": {
            "command": [str(source)], "env": {"PATH": str(wrapper.parent), "PATHEXT": ".EXE;.CMD"},
        }}),
        "NZ_MCP_USER_CONFIG": str(tmp_path / "absent.json"),
        "NZ_MCP_TRUST_STORE": str(tmp_path / "trust.json"),
    }, user_config_path=tmp_path / "absent.env")
    servers = load_mcp_server_configs(workspace=root, config_snapshot=snapshot)
    assert servers[0].execution_identity.launch_command[0] == str(wrapper)
    monkeypatch.setattr("nz_coder.mcp.client.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("untrusted Windows source reached Popen"))
    runtime = MCPRuntime(servers)
    try:
        runtime.start()
        assert runtime.status_summary()[0]["status"] == "untrusted"
    finally:
        runtime.close()


def test_relative_posix_path_is_frozen_against_service_cwd(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    source = _program(root / "bin/server", f"#!{sys.executable}\nprint('service')\n")
    ambient = tmp_path / "ambient"
    _program(ambient / "bin/server", f"#!{sys.executable}\nprint('ambient')\n")
    monkeypatch.chdir(ambient)
    identity = resolve_execution_identity(("server",), cwd=root, workspace=root,
        config_source="user", environment_profile="strict-service", environment={"PATH": "bin"})
    assert identity.launch_command == (sys.executable, str(source))
    assert dict(identity.environment)["PATH"] == str(root / "bin")
    monkeypatch.chdir(tmp_path)
    assert verify_execution_identity(identity).launch_command == identity.launch_command
