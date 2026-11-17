"""LSP 启动必须绑定实际环境、可信来源及分析输入。"""
from dataclasses import asdict, replace
import json
import pickle
import sys

import pytest

from nz_coder.foundation.execution_identity import verify_execution_identity, UnsafeExecutionIdentity
from nz_coder.foundation.workspace_trust import load_config_snapshot, ConfigValidationError
from nz_coder.lsp.servers import resolve_server
from nz_coder.runtime.process.execution_environment import ExecutionEnvironment, scoped_execution_environment


def _program(path, text="native-fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def _source(root, name="app.py"):
    root.mkdir(parents=True, exist_ok=True)
    source = root / name
    source.write_text("VALUE = 1\n")
    return source


def _snapshot(root, command, profile=None, language="python"):
    return load_config_snapshot(root, environ={
        f"NZ_LSP_{language.upper()}_COMMAND": str(command),
        "NZ_LSP_SERVERS_JSON": json.dumps(profile or {}),
    }, user_config_path=root / "missing-user-config")


def _origin(root):
    info = root.stat()
    return (str(root.resolve()), info.st_dev, info.st_ino)


def test_prepared_path_is_used_before_host_discovery_and_frozen(tmp_path, monkeypatch):
    root = tmp_path / "child"
    source = _source(root)
    prepared_server = _program(root / "venv/bin/test-lsp")
    host = _program(tmp_path / "host/test-lsp")
    monkeypatch.setenv("PATH", str(host.parent))
    prepared = ExecutionEnvironment.capture(source={"PATH": str(prepared_server.parent)})
    with scoped_execution_environment(prepared):
        server = resolve_server(source, root, config_snapshot=_snapshot(root, "test-lsp"))
    assert server.command == (str(prepared_server),)
    assert dict(server.environment)["PATH"] == str(prepared_server.parent)
    monkeypatch.setenv("PATH", "/changed")
    assert verify_execution_identity(server.execution_identity).fingerprint == server.fingerprint


def test_json_path_override_precedes_discovery_and_placeholders_use_pre_override_env(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / "tools/test-lsp")
    prepared = ExecutionEnvironment.capture(source={"PATH": "/before"})
    profile = {"python": {"env": {"PATH": "${worktree}/tools", "ORIGINAL": "${env:PATH}"},
                          "settings": {"nested": ["${worktree}/src"]}}}
    with scoped_execution_environment(prepared):
        server = resolve_server(source, root, config_snapshot=_snapshot(root, "test-lsp", profile))
    assert server is not None
    assert server.command == (str(tool),)
    assert dict(server.environment)["ORIGINAL"] == "/before"
    assert json.loads(server.settings_json) == {"nested": [str(root / "src")]}


def test_missing_placeholder_is_explicit_config_error(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    profile = {"python": {"settings": {"path": "${env:UNAVAILABLE_LSP_TEST}"}}}
    with pytest.raises(ConfigValidationError, match="placeholder"):
        resolve_server(source, root, config_snapshot=_snapshot(root, sys.executable, profile))


@pytest.mark.parametrize("profile", [
    {"python": {"command": ["other"]}},
    {"unregistered": {}},
    {"python": {"initialization": []}},
    {"python": {"env": {"API_TOKEN": "private-value"}}},
    {"python": {"env": {"PYTHONPATH": "/untrusted"}}},
    {"python": {"env": {"NODE_OPTIONS": "--require=/untrusted"}}},
    {"python": {"settings": {"path": "${parent}"}}},
])
def test_snapshot_rejects_json_that_bypasses_config_or_environment_policy(tmp_path, profile):
    with pytest.raises(ConfigValidationError, match="NZ_LSP_SERVERS_JSON"):
        _snapshot(tmp_path, sys.executable, profile)


def test_json_is_normalized_opaque_and_carried_in_run_settings(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings
    profile = {"python": {"settings": {"sensitive": "private-lsp-value"}}}
    snapshot = _snapshot(tmp_path, sys.executable, profile)
    settings = RunSettings.from_snapshot(snapshot)
    assert json.loads(settings.lsp_servers_json) == profile
    assert snapshot.public()["NZ_LSP_SERVERS_JSON"]["value"] == "<configured>"
    assert "private-lsp-value" not in repr(asdict(replace(settings, snapshot=None)))
    assert "private-lsp-value" not in repr(snapshot.value("NZ_LSP_SERVERS_JSON"))


def test_simple_env_shebang_uses_frozen_interpreter_and_parent_source_requires_child_trust(tmp_path, monkeypatch):
    parent, child = tmp_path / "parent", tmp_path / "child"
    parent.mkdir()
    source = _source(child)
    interpreter = _program(parent / ".venv/bin/python", "interpreter-v1")
    script = _program(parent / ".venv/bin/pyright-langserver", "#!/usr/bin/env python\nprint('server')\n")
    prepared = ExecutionEnvironment.capture(source={"PATH": str(interpreter.parent)}, origin_workspaces=(_origin(parent), _origin(child)))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))
    snapshot = _snapshot(child, "pyright-langserver")
    with scoped_execution_environment(prepared):
        server = resolve_server(source, child, config_snapshot=snapshot)
    assert server.command == (str(interpreter), str(script))
    assert server.source == "workspace"
    assert server.trusted is False
    assert server.origin_workspaces == prepared.origin_workspaces
    assert verify_execution_identity(server.execution_identity).fingerprint == server.fingerprint
    interpreter.write_text("interpreter-v2")
    with pytest.raises(UnsafeExecutionIdentity, match="changed"):
        verify_execution_identity(server.execution_identity)


def test_origin_identity_survives_private_payload_and_rejects_replaced_parent(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    env = ExecutionEnvironment.capture(source={"PATH": "/one"}, origin_workspaces=(_origin(parent),))
    restored = pickle.loads(pickle.dumps(env))
    parent.rename(tmp_path / "old-parent")
    parent.mkdir()
    with pytest.raises(Exception, match="origin|source"):
        restored.validate()
    replacement = ExecutionEnvironment.capture(source={"PATH": "/one"}, origin_workspaces=(_origin(parent),))
    assert env.fingerprint != replacement.fingerprint


def test_prepare_environment_supplies_parent_and_execution_root_origins(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment, WorkspacePreparationSpec
    parent, child = tmp_path / "parent", tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    receipt = prepare_environment(WorkspacePreparationSpec({}), parent=parent, child=child,
                                  state={}, preparation=None, read_only=True)
    assert receipt.environment.origin_workspaces == (_origin(parent), _origin(child))


def test_prepared_python_module_is_explicitly_unsupported(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    (root / "server_module.py").write_text("pass\n")
    with scoped_execution_environment(ExecutionEnvironment.capture()):
        with pytest.raises(UnsafeExecutionIdentity, match="prepared.*module|module.*prepared"):
            resolve_server(source, root, config_snapshot=_snapshot(root, f"{sys.executable} -m server_module"))


def test_pyright_prepared_interpreter_mapping_and_explicit_precedence(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    venv = root / ".venv"
    python = _program(venv / "bin/python")
    tool = _program(venv / "bin/basedpyright-langserver")
    prepared = ExecutionEnvironment.capture(source={"PATH": str(tool.parent), "VIRTUAL_ENV": str(venv)})
    with scoped_execution_environment(prepared):
        inferred = resolve_server(source, root, config_snapshot=_snapshot(root, str(tool)))
        explicit = resolve_server(source, root, config_snapshot=_snapshot(root, str(tool), {
            "python": {"initialization": {"pythonPath": "/explicit/python"}}}))
    assert json.loads(inferred.settings_json)["python"]["pythonPath"] == str(python)
    assert inferred.analysis_environment == "configured"
    assert json.loads(explicit.initialization_json)["pythonPath"] == "/explicit/python"
    assert json.loads(explicit.settings_json)["python"]["pythonPath"] == "/explicit/python"
    assert "VIRTUAL_ENV" not in dict(inferred.environment)
    assert inferred.environment_fingerprint == explicit.environment_fingerprint


def test_ty_remains_default_and_does_not_claim_interpreter_alignment(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / ".venv/bin/ty")
    _program(tool.parent / "basedpyright-langserver")
    prepared = ExecutionEnvironment.capture(source={"PATH": str(tool.parent), "VIRTUAL_ENV": str(tool.parent.parent)})
    with scoped_execution_environment(prepared):
        server = resolve_server(source, root, config_snapshot=_snapshot(root, ""))
    assert server.server_id == "ty"
    assert server.analysis_environment == "unconfigured"


@pytest.mark.parametrize("entry_kind,expected", [("child", "configured"), ("parent", "unconfigured"), ("missing", "unconfigured")])
def test_clangd_only_accepts_selected_database_for_current_child_source(tmp_path, entry_kind, expected):
    root = tmp_path / "child"
    source = _source(root, "app.cpp")
    tool = _program(tmp_path / "bin/clangd")
    build = root / "build"
    build.mkdir()
    if entry_kind != "missing":
        directory = root if entry_kind == "child" else tmp_path / "parent"
        (build / "compile_commands.json").write_text(json.dumps([
            {"directory": str(directory), "file": "app.cpp", "arguments": ["c++", "-c", "app.cpp"]}]))
    server = resolve_server(source, root, config_snapshot=_snapshot(
        root, f"{tool} --compile-commands-dir={{root}}/build", language="cpp"))
    assert server.analysis_environment == expected


def test_owner_trust_uses_same_snapshot_and_never_inherits_parent_grant(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore
    from nz_coder.lsp.servers import trust_server, untrust_server
    parent, child = tmp_path / "parent", tmp_path / "child"
    parent.mkdir()
    source = _source(child)
    tool = _program(parent / "tools/test-lsp")
    store_path = tmp_path / "user/trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(store_path))
    env = ExecutionEnvironment.capture(source={"PATH": str(tool.parent)}, origin_workspaces=(_origin(parent), _origin(child)))
    snapshot = _snapshot(child, "test-lsp")
    with scoped_execution_environment(env):
        candidate = resolve_server(source, child, config_snapshot=snapshot)
        WorkspaceTrustStore(store_path).trust(parent, "lsp:python", candidate.fingerprint, executable=str(tool))
        assert not resolve_server(source, child, config_snapshot=snapshot).trusted
        trusted = trust_server(source, child, config_snapshot=snapshot)
        assert trusted.trusted
        assert trusted.fingerprint == candidate.fingerprint
        assert untrust_server(source, child, config_snapshot=snapshot)
        assert not resolve_server(source, child, config_snapshot=snapshot).trusted


def test_final_environment_is_filtered_and_identity_binds_values(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / "tools/test-lsp")
    prepared = ExecutionEnvironment.capture(source={"PATH": str(tool.parent), "PYTHONPATH": "/unsafe", "VIRTUAL_ENV": "/unsafe", "API_TOKEN": "ambient-secret"})
    profiles = [{"python": {"env": {"MODE": value}, "settings": {"secret": "settings-private-value"}}} for value in ("one", "two")]
    with scoped_execution_environment(prepared):
        first, second = [resolve_server(source, root, config_snapshot=_snapshot(root, str(tool), p)) for p in profiles]
    assert first.fingerprint != second.fingerprint
    assert first.environment_fingerprint != second.environment_fingerprint
    assert not {"PYTHONPATH", "VIRTUAL_ENV", "API_TOKEN"} & dict(first.environment).keys()
    assert "settings-private-value" not in repr(asdict(first))


def test_complex_env_shebang_is_explicitly_unsupported(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / "server", "#!/usr/bin/env -S python -u\npass\n")
    with pytest.raises(UnsafeExecutionIdentity, match="unsupported.*shebang"):
        resolve_server(source, root, config_snapshot=_snapshot(root, str(tool)))


def test_pyright_symlink_entry_keeps_service_type_and_explicit_settings_win(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    node = _program(tmp_path / "system/node")
    payload = _program(root / "node_modules/pyright/langserver.index.js", "#!/usr/bin/env node\n// fixture\n")
    tool = root / "node_modules/.bin/pyright-langserver"
    tool.parent.mkdir()
    tool.symlink_to(payload)
    env = ExecutionEnvironment.capture(source={"PATH": str(node.parent)})
    with scoped_execution_environment(env):
        server = resolve_server(source, root, config_snapshot=_snapshot(root, str(tool), {
            "python": {"initialization": {"pythonPath": "/init/python"},
                       "settings": {"python": {"pythonPath": "/settings/python"}}}}))
    assert server.analysis_environment == "configured"
    assert server.analysis_configuration_source == "explicit-settings"
    assert json.loads(server.settings_json)["python"]["pythonPath"] == "/settings/python"


def test_service_temporary_directory_is_bound_before_identity(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    tool = _program(root / "test-lsp")
    server = resolve_server(source, root, config_snapshot=_snapshot(root, str(tool)))
    environment = dict(server.environment)
    assert environment["TMPDIR"] == environment["TMP"] == environment["TEMP"]
    assert not str(environment["TMPDIR"]).startswith(str(root))
    assert dict(server.execution_identity.environment) == environment


def test_prepared_explicit_script_may_receive_module_named_argument(tmp_path):
    root = tmp_path / "child"
    source = _source(root)
    script = _program(root / "server.py", "pass\n")
    with scoped_execution_environment(ExecutionEnvironment.capture()):
        server = resolve_server(source, root, config_snapshot=_snapshot(root, f"{sys.executable} {script} -m option"))
    assert server.execution_identity.entrypoint_kind == "python-script"
