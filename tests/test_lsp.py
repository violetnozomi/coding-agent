"""Protocol, safety, and tool-level tests for optional LSP support."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nz_coder.lsp import close_all_clients
from nz_coder.runtime.process.workdir import scoped_workdir


def _inert_client():
    from nz_coder.lsp.client import LSPClient

    client = LSPClient(
        server_id="inert",
        command=(sys.executable,),
        root=Path.cwd(),
        language_id="python",
    )
    client.capabilities = {
        "capabilities": {"textDocumentSync": 1, "diagnosticProvider": {}}
    }
    return client


def _registered_client(root, client, key=None, lease_id=""):
    import contextvars
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import ResolvedServer
    from nz_coder.foundation.workspace_trust import current_config_snapshot

    key = key or (str(root.resolve()), "fake", str(root.resolve()))
    entry = manager._Entry(
        key,
        ResolvedServer("fake", "python", (sys.executable,), root),
        root / "a.py",
        root,
        current_config_snapshot(root),
        contextvars.copy_context(),
    )
    entry.client = client
    entry.state = "ready"
    entry.done.set()
    entry.lease_id = lease_id
    manager._ENTRIES[key] = entry
    return entry


def test_lsp_temp_files_stay_private_without_changing_user_tmpdir(
    tmp_path, monkeypatch
):
    from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
    from nz_coder.foundation.user_paths import user_storage_layout
    from nz_coder.lsp.client import LSPClient
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore

    workspace = tmp_path / "workspace"
    user_tmp = workspace / ".tmp"
    user_tmp.mkdir(parents=True)
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(user_tmp))
    script = tmp_path / "temp_lsp_server.py"
    server = _FAKE_SERVER.replace(
        "import json\nimport sys",
        "import json\nimport sys\nimport tempfile\n"
        "created_tmp = tempfile.mkdtemp(prefix='pyright-regression-')",
        1,
    ).replace(
        '"capabilities": {', '"capabilities": {"createdTempDirectory": created_tmp,', 1
    )
    script.write_text(server)
    store = WorkspaceSnapshotStore(workspace, tmp_path / "snapshots")
    before = store.verification_snapshot()
    client = LSPClient(
        server_id="temp-test",
        command=(sys.executable, str(script)),
        root=workspace,
        language_id="python",
    )
    client.start()
    try:
        created = Path(client.capabilities["capabilities"]["createdTempDirectory"])
        assert created.is_relative_to(user_storage_layout(workspace).workspace_cache)
        assert store.verification_snapshot() == before
        user_environment = build_sanitized_subprocess_env(profile="strict-service")
        assert all(
            user_environment[key] == str(user_tmp) for key in ("TMPDIR", "TMP", "TEMP")
        )
    finally:
        client.close()


def test_lsp_rejects_workspace_owned_internal_storage_before_spawn(
    tmp_path, monkeypatch
):
    from nz_coder.foundation.user_paths import UnsafeUserStorage
    from nz_coder.lsp.client import LSPClient

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    spawned = []

    def forbidden_spawn(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("unsafe internal storage must fail before spawn")

    monkeypatch.setattr(subprocess, "Popen", forbidden_spawn)
    with pytest.raises(UnsafeUserStorage):
        LSPClient(
            server_id="unsafe-temp",
            command=(sys.executable, "unused.py"),
            root=tmp_path,
            language_id="python",
        )
    assert not spawned


_FAKE_SERVER = r"""
import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    size = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(size).decode("utf-8"))


def send(message):
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    )
    sys.stdout.buffer.flush()


document_uri = ""
while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    params = message.get("params") or {}
    request_id = message.get("id")

    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": 900,
            "method": "workspace/configuration",
            "params": {"items": [{"section": "python"}]},
        })
        configuration = read_message()
        if configuration.get("result") != [None]:
            raise RuntimeError("client did not answer workspace/configuration")
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "capabilities": {
                    "textDocumentSync": 1,
                    "definitionProvider": True,
                    "referencesProvider": True,
                    "hoverProvider": True,
                    "documentSymbolProvider": True,
                    "workspaceSymbolProvider": True,
                    "diagnosticProvider": {},
                }
            },
        })
        continue

    if method == "textDocument/didOpen":
        document_uri = params["textDocument"]["uri"]
        send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": document_uri,
                "diagnostics": [{
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                    "severity": 2,
                    "source": "fake",
                    "message": "fake warning",
                }],
            },
        })
        continue

    if request_id is None:
        if method == "exit":
            break
        continue

    position = params.get("position", {"line": 0, "character": 0})
    location = {
        "uri": document_uri,
        "range": {"start": position, "end": position},
    }
    if method == "textDocument/definition":
        result = location
    elif method == "textDocument/references":
        result = [location, location]
    elif method == "textDocument/hover":
        result = {"contents": {"kind": "markdown", "value": "`int`"}}
    elif method == "textDocument/documentSymbol":
        result = [{
            "name": "answer",
            "kind": 13,
            "range": location["range"],
            "selectionRange": location["range"],
        }]
    elif method == "workspace/symbol":
        result = [{"name": params.get("query") or "answer", "kind": 13, "location": location}]
    elif method == "textDocument/implementation":
        result = [location]
    elif method == "textDocument/prepareCallHierarchy":
        result = [{
            "name": "answer",
            "kind": 12,
            "uri": document_uri,
            "range": location["range"],
            "selectionRange": location["range"],
        }]
    elif method in ("callHierarchy/incomingCalls", "callHierarchy/outgoingCalls"):
        result = [{"from": params["item"], "fromRanges": [location["range"]]}]
    elif method == "textDocument/diagnostic":
        result = {
            "kind": "full",
            "items": [{
                "range": location["range"],
                "severity": 2,
                "source": "fake",
                "message": "fake warning",
            }],
        }
    elif method == "shutdown":
        result = None
    else:
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"unsupported: {method}"},
        })
        continue
    send({"jsonrpc": "2.0", "id": request_id, "result": result})
"""


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_lsp_client_rejects_invalid_timeout_before_spawn(
    tmp_path, monkeypatch, timeout
):
    from nz_coder.lsp.client import LSPClient

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid timeout must fail before process spawn")
        ),
    )
    with pytest.raises(ValueError, match="timeout"):
        LSPClient(
            server_id="invalid",
            command=(sys.executable, "server.py"),
            root=tmp_path,
            language_id="python",
            initialize_timeout=timeout,
        )


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_lsp_resolver_rejects_invalid_timeout(tmp_path, timeout):
    from nz_coder.intelligence.lsp_resolver import LspCallTargetResolver

    with pytest.raises(ValueError, match="timeout"):
        LspCallTargetResolver(tmp_path, request_timeout=timeout)


def test_windows_lsp_override_preserves_backslashes_and_quoted_paths():
    from nz_coder.lsp.servers import _split_override

    assert _split_override(
        r'"C:\Program Files\Python\python.exe" -u C:\repo\fake_server.py',
        os_name="nt",
    ) == (
        r"C:\Program Files\Python\python.exe",
        "-u",
        r"C:\repo\fake_server.py",
    )


def _lsp_config_snapshot(tmp_path, workspace, *, environment=None, user_text=""):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    user_config = tmp_path / "user.env"
    if user_text:
        user_config.write_text(user_text, encoding="utf-8")
    return load_config_snapshot(
        workspace,
        environ=environment or {},
        user_config_path=user_config,
        trust_store=WorkspaceTrustStore(tmp_path / "trust.json"),
    )


def test_all_lsp_specs_have_matching_config_schema_entries():
    from nz_coder.foundation.workspace_trust import CONFIG_SCHEMA
    from nz_coder.lsp.servers import language_server_specs

    expected = {
        f"NZ_LSP_{spec.language.upper()}_COMMAND" for spec in language_server_specs()
    }
    assert expected <= set(CONFIG_SCHEMA)
    assert all(CONFIG_SCHEMA[key].value_type == "string" for key in expected)
    assert all(CONFIG_SCHEMA[key].workspace_trust_required for key in expected)


def test_typescript_lsp_command_loads_from_user_config(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    snapshot = _lsp_config_snapshot(
        tmp_path,
        workspace,
        user_text="NZ_LSP_TYPESCRIPT_COMMAND=typescript-language-server --stdio\n",
    )
    assert snapshot.get("NZ_LSP_TYPESCRIPT_COMMAND") == (
        "typescript-language-server --stdio"
    )


def test_go_lsp_command_loads_from_explicit_environment(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    snapshot = _lsp_config_snapshot(
        tmp_path, workspace, environment={"NZ_LSP_GO_COMMAND": "gopls -remote=auto"}
    )
    assert snapshot.get("NZ_LSP_GO_COMMAND") == "gopls -remote=auto"


def test_workspace_typescript_lsp_command_requires_exact_trust(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "NZ_LSP_TYPESCRIPT_COMMAND=repo-ts-server --stdio\n", encoding="utf-8"
    )
    untrusted = _lsp_config_snapshot(tmp_path, workspace)
    assert untrusted.get("NZ_LSP_TYPESCRIPT_COMMAND", "") == ""

    store = WorkspaceTrustStore(tmp_path / "trust.json")
    store.trust(workspace, "workspace-config", untrusted.workspace_fingerprint)
    trusted = _lsp_config_snapshot(tmp_path, workspace)
    assert trusted.get("NZ_LSP_TYPESCRIPT_COMMAND") == "repo-ts-server --stdio"


def test_windows_quoted_lsp_command_survives_schema_loading(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    command = r'"C:\Program Files\TypeScript\server.cmd" --stdio'
    snapshot = _lsp_config_snapshot(
        tmp_path, workspace, environment={"NZ_LSP_TYPESCRIPT_COMMAND": command}
    )
    assert snapshot.get("NZ_LSP_TYPESCRIPT_COMMAND") == command


def test_windows_file_uri_roundtrip_removes_uri_drive_prefix():
    from nz_coder.lsp.client import uri_to_path

    assert uri_to_path(
        "file:///C:/Users/Runner%20Admin/repo/app.py",
        os_name="nt",
    ) == Path(r"C:\Users\Runner Admin\repo\app.py")


def _write_fake_server(tmp_path):
    path = tmp_path / "fake_lsp.py"
    path.write_text(textwrap.dedent(_FAKE_SERVER), encoding="utf-8")
    return path


def _trust_test_lsp(tmp_path, monkeypatch, source_name="app.py"):
    from nz_coder.lsp.servers import trust_server

    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(tmp_path.parent / f"{tmp_path.name}-workspace-trust.json"),
    )
    trust_server(tmp_path / source_name, tmp_path)


def test_close_workspace_clients_preserves_other_workspaces(tmp_path):
    from nz_coder.lsp import manager

    first_root = (tmp_path / "first").resolve()
    second_root = (tmp_path / "second").resolve()
    first_root.mkdir()
    second_root.mkdir()

    class Client:
        def __init__(self):
            self.closed = False

        def close(self, **kwargs):
            self.closed = True

    first = Client()
    second = Client()
    first_key = (str(first_root), "python", str(first_root))
    second_key = (str(second_root), "python", str(second_root))
    manager.close_all_clients()
    try:
        _registered_client(first_root, first, first_key)
        _registered_client(second_root, second, second_key)

        manager.close_workspace_clients(first_root)

        assert first.closed is True
        assert second.closed is False
        assert first_key not in manager._ENTRIES
        assert second_key in manager._ENTRIES
    finally:
        manager.close_all_clients()


def test_lsp_tool_roundtrip_normalizes_paths_and_positions(tmp_path, monkeypatch):
    from nz_coder.tools.lsp import lsp

    server = _write_fake_server(tmp_path)
    monkeypatch.setenv(
        "NZ_LSP_PYTHON_COMMAND",
        f"{sys.executable} -u {server}",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    _trust_test_lsp(tmp_path, monkeypatch)

    close_all_clients()
    try:
        with scoped_workdir(tmp_path):
            definition = json.loads(
                lsp(
                    "goToDefinition",
                    "app.py",
                    line=1,
                    character=3,
                )
            )
            references = json.loads(
                lsp(
                    "findReferences",
                    "app.py",
                    line=1,
                    character=3,
                )
            )
            diagnostics = json.loads(lsp("diagnostics", "app.py"))

        assert definition["items"][0]["path"] == "app.py"
        assert definition["items"][0]["range"]["start"] == {"line": 0, "character": 2}
        assert len(references["items"]) == 2
        assert diagnostics["items"][0]["message"] == "fake warning"
    finally:
        close_all_clients()


def test_lsp_tool_supports_symbols_hover_and_call_hierarchy(tmp_path, monkeypatch):
    from nz_coder.tools.lsp import lsp

    server = _write_fake_server(tmp_path)
    monkeypatch.setenv(
        "NZ_LSP_PYTHON_COMMAND",
        f"{sys.executable} -u {server}",
    )
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    _trust_test_lsp(tmp_path, monkeypatch)

    close_all_clients()
    try:
        with scoped_workdir(tmp_path):
            hover = json.loads(lsp("hover", "app.py"))
            symbols = json.loads(lsp("documentSymbol", "app.py"))
            workspace = json.loads(
                lsp(
                    "workspaceSymbol",
                    "app.py",
                    query="answer",
                )
            )
            calls = json.loads(lsp("incomingCalls", "app.py"))

        assert hover["items"][0]["contents"]["value"] == "`int`"
        assert symbols["items"][0]["name"] == "answer"
        assert workspace["items"][0]["location"]["path"] == "app.py"
        assert calls["items"][0]["from"]["path"] == "app.py"
    finally:
        close_all_clients()


def test_lsp_tool_blocks_path_escape_before_server_discovery(tmp_path):
    from nz_coder.tools.lsp import lsp

    with scoped_workdir(tmp_path):
        result = lsp("hover", "../outside.py")

    assert result.startswith("Error: Path escapes workspace:")


def test_lsp_tool_gracefully_reports_unsupported_extension(tmp_path):
    from nz_coder.tools.lsp import lsp

    (tmp_path / "notes.unknown").write_text("hello\n", encoding="utf-8")
    with scoped_workdir(tmp_path):
        result = lsp("hover", "notes.unknown")

    assert result.startswith("Error: No LSP server is configured")


def test_lsp_tool_reports_server_initialization_failure(tmp_path, monkeypatch):
    import time

    from nz_coder.lsp.client import LSPClient
    from nz_coder.tools.lsp import lsp

    read_stderr = LSPClient._read_stderr

    def delayed_read_stderr(client):
        time.sleep(0.05)
        read_stderr(client)

    monkeypatch.setattr(LSPClient, "_read_stderr", delayed_read_stderr)
    broken = tmp_path / "broken_lsp.py"
    broken.write_text(
        "import sys\nsys.stderr.write('broken server\\n')\nsys.exit(2)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "NZ_LSP_PYTHON_COMMAND",
        f"{sys.executable} -u {broken}",
    )
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    _trust_test_lsp(tmp_path, monkeypatch)

    close_all_clients()
    try:
        with scoped_workdir(tmp_path):
            result = lsp("hover", "app.py")
        assert result.startswith("Error: LSP server failed to initialize:")
        assert "broken server" not in result
    finally:
        close_all_clients()


def test_lsp_tool_honors_disabled_configuration(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.tools.lsp import lsp

    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    monkeypatch.setattr(config, "LSP_ENABLED", False)

    with scoped_workdir(tmp_path):
        result = lsp("hover", "app.py")

    assert result == "Error: LSP support is disabled by NZ_LSP_ENABLED."


def test_lsp_optional_pack_is_unloaded_until_requested():
    code = (
        "import json\n"
        "import nz_coder.loop\n"
        "from nz_coder.tools import dispatch, get_specs\n"
        "before = [x['function']['name'] for x in get_specs()]\n"
        "result = dispatch('load_optional_tools', {'packs': ['lsp']})\n"
        "after = [x['function']['name'] for x in get_specs()]\n"
        "print(json.dumps({'before': before, 'after': after, 'result': result}))\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    payload = json.loads(process.stdout.strip().splitlines()[-1])

    assert process.returncode == 0, process.stderr
    assert "lsp" not in payload["before"]
    assert "lsp" in payload["after"]
    assert "Loaded optional tool packs" in payload["result"]


def test_lsp_is_classified_as_safe_read_only_tool():
    from nz_coder.permissions import PermissionManager

    decision = PermissionManager("default").check(
        "lsp",
        {
            "operation": "hover",
            "file_path": "app.py",
        },
    )

    assert decision == {"behavior": "allow", "reason": "Safe tool"}


def test_python_package_project_root_promotes_parent_import_path(tmp_path, monkeypatch):
    from nz_coder.lsp.servers import resolve_server

    package = tmp_path / "cron_engine"
    tests = package / "tests"
    tests.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "pyproject.toml").write_text(
        "[project]\nname='cron-engine'\n", encoding="utf-8"
    )
    target = tests / "test_parser.py"
    target.write_text("from cron_engine.parser import parse\n", encoding="utf-8")
    monkeypatch.setenv("NZ_LSP_PYTHON_COMMAND", sys.executable)

    resolved = resolve_server(target, tmp_path)

    assert resolved is not None
    assert resolved.root == tmp_path.resolve()
    assert resolved.analysis_paths == ()


def test_write_diagnostics_use_real_protocol_and_normalize_locations(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.lsp.write_diagnostics import collect_write_diagnostics

    server = _write_fake_server(tmp_path)
    monkeypatch.setenv(
        "NZ_LSP_PYTHON_COMMAND",
        f"{sys.executable} -u {server}",
    )
    monkeypatch.setattr(config, "LSP_WRITE_DIAGNOSTICS_ENABLED", True)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("answer = missing\n", encoding="utf-8")
    _trust_test_lsp(tmp_path, monkeypatch)

    close_all_clients()
    try:
        batch = collect_write_diagnostics(
            ["app.py", "app.py", "../outside.py"],
            tmp_path,
        )
        from nz_coder.lsp.write_diagnostics import format_write_diagnostics

        block = format_write_diagnostics(batch)
        assert block.startswith("<lsp-diagnostics>")
        assert "app.py:1:1 [warning] fake warning (fake)" in block
        assert block.count("fake warning") == 1
        assert "outside.py" not in block
    finally:
        close_all_clients()


def test_write_diagnostics_reports_missing_server(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.lsp.write_diagnostics import collect_write_diagnostics

    monkeypatch.setenv(
        "NZ_LSP_PYTHON_COMMAND",
        str(tmp_path / "missing-language-server"),
    )
    monkeypatch.setattr(config, "LSP_WRITE_DIAGNOSTICS_ENABLED", True)
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")

    close_all_clients()
    try:
        assert (
            collect_write_diagnostics(["app.py"], tmp_path)["reports"][0]["status"]
            == "unavailable"
        )
    finally:
        close_all_clients()


def test_lsp_open_document_uses_anchored_source_bytes(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    source = tmp_path / "source.py"
    source.write_text("anchored = 1\n", encoding="utf-8")
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("source.py")
    source.write_text("swapped = 2\n", encoding="utf-8")
    sent = []
    client = _inert_client()
    client.root = tmp_path.resolve()
    client.language_id = "python"
    client.notify = lambda method, params, **kwargs: sent.append((method, params))

    client.open_document(source, text, identity)

    assert sent[0][1]["textDocument"]["text"] == text


def test_lsp_diagnostics_does_not_reread_source_path(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.lsp.client import LSPResponseError

    source = tmp_path / "source.py"
    source.write_text("anchored = 1\n", encoding="utf-8")
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("source.py")
    client = _inert_client()
    client.root = tmp_path.resolve()
    client.language_id = "python"
    client.diagnostic_wait = 0.02
    client.notify = lambda *_args, **_kwargs: None
    client.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        LSPResponseError({"code": -1}),
    )
    source.unlink()

    assert client.diagnostics(source, text, identity).to_dict()["items"] == []


def test_lsp_outside_and_private_uris_are_redacted(tmp_path):
    from nz_coder.tools.lsp import _normalize_result

    private = tmp_path / ".nz-coder" / "secret.py"
    outside = tmp_path.parent / "host-secret.py"

    assert _normalize_result({"uri": private.as_uri()}, tmp_path)["path"] == (
        "private-workspace"
    )
    assert (
        _normalize_result({"targetUri": outside.as_uri()}, tmp_path)["targetPath"]
        == "outside-workspace"
    )


def test_lsp_outside_uri_does_not_expose_absolute_path(tmp_path):
    from nz_coder.tools.lsp import _normalize_result

    outside = tmp_path.parent / "host-secret.py"
    result = _normalize_result({"uri": outside.as_uri()}, tmp_path)
    assert result["path"] == "outside-workspace"
    assert str(outside) not in repr(result)


def test_lsp_private_uri_is_redacted(tmp_path):
    from nz_coder.tools.lsp import _normalize_result

    private = tmp_path / ".nz-coder" / "secret.py"
    result = _normalize_result({"uri": private.as_uri()}, tmp_path)
    assert result["path"] == "private-workspace"
    assert str(private) not in repr(result)


def test_lsp_source_change_is_observed_only_on_next_request(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    source = tmp_path / "source.py"
    source.write_text("first = 1\n", encoding="utf-8")
    access = WorkspaceFileAccess(tmp_path)
    first_text, first_identity = access.read_text_with_identity("source.py")
    source.write_text("second = 2\n", encoding="utf-8")
    sent = []
    client = _inert_client()
    client.root = tmp_path.resolve()
    client.language_id = "python"
    client.notify = lambda method, params, **kwargs: sent.append((method, params))

    client.open_document(source, first_text, first_identity)
    second_text, second_identity = access.read_text_with_identity("source.py")
    client.open_document(source, second_text, second_identity)

    assert sent[0][1]["textDocument"]["text"] == first_text
    assert sent[-1][1]["contentChanges"][0]["text"] == second_text


def test_lsp_parent_swap_after_tool_validation_cannot_escape(tmp_path, monkeypatch):
    from nz_coder.tools import lsp as lsp_tool

    workspace = tmp_path / "workspace"
    source_parent = workspace / "src"
    parked = workspace / "src-original"
    outside = tmp_path / "outside"
    source_parent.mkdir(parents=True)
    outside.mkdir()
    (source_parent / "main.py").write_text("inside = 1\n", encoding="utf-8")
    (outside / "main.py").write_text("SENTINEL-LSP-OUTSIDE\n", encoding="utf-8")
    original = lsp_tool._safe_path

    def validate_then_swap(path):
        validated = original(path)
        source_parent.rename(parked)
        source_parent.symlink_to(outside, target_is_directory=True)
        return validated

    monkeypatch.setattr(lsp_tool, "_safe_path", validate_then_swap)
    with scoped_workdir(workspace):
        result = lsp_tool.lsp("hover", "src/main.py", 1, 1)

    assert str(result).startswith("Error: ")
    assert "SENTINEL-LSP-OUTSIDE" not in str(result)


def test_lsp_reads_complete_fragmented_content_length_frame():
    import os
    import threading
    from types import SimpleNamespace

    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    client = _inert_client()
    client.process = SimpleNamespace(stdout=reader)
    body = json.dumps({"jsonrpc": "2.0", "result": "fragmented"}).encode()
    first_fragment_written = threading.Event()
    release = threading.Event()

    def write_fragments():
        try:
            os.write(
                write_fd, f"Content-Length: {len(body)}\r\n\r\n".encode() + body[:7]
            )
            first_fragment_written.set()
            release.wait(1)
            os.write(write_fd, body[7:])
        finally:
            os.close(write_fd)

    worker = threading.Thread(target=write_fragments)
    worker.start()
    assert first_fragment_written.wait(1)
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        assert client._read_message() == {"jsonrpc": "2.0", "result": "fragmented"}
    finally:
        release.set()
        worker.join(1)
        timer.join(1)
        reader.close()


def test_lsp_request_timeout_covers_blocked_stdin_write(tmp_path):
    import os
    import threading
    from nz_coder.lsp.client import LSPTimeoutError

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=(os.name != "nt"),
    )
    client = _inert_client()
    client.process = process
    client.server_id = "blocked"
    client.request_timeout = 0.05
    client._closed = False
    errors = []

    def send():
        try:
            client.request("blocked", {"data": "x" * (1024 * 1024)}, timeout=0.05)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=send)
    worker.start()
    try:
        worker.join(1)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], LSPTimeoutError)
        assert not client._pending
    finally:
        process.kill()
        process.wait(timeout=2)
        worker.join(2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_lsp_cleanup_failure_keeps_manager_owner_retryable(tmp_path):
    from nz_coder.lsp import manager
    from nz_coder.lsp.client import LSPError
    from nz_coder.foundation.capability_lease import capability_leases

    key = (str(tmp_path.resolve()), "test", str(tmp_path))

    class Client:
        failed = True

        def close(self, force=False, **kwargs):
            if self.failed:
                raise LSPError("cleanup pending")

    client = Client()
    lease = capability_leases().create(
        kind="lsp-client",
        resource_id="test",
        workspace=tmp_path,
        control_fingerprint="test",
        run_id="test",
        interaction_id="test",
        owner_session="test",
        revoke=lambda: None,
    )
    with manager._LOCK:
        _registered_client(tmp_path, client, key, lease.lease_id)
    try:
        with pytest.raises(LSPError, match="cleanup pending"):
            manager.close_workspace_clients(tmp_path)
        assert manager._ENTRIES[key].client is client
        assert capability_leases().get(lease.lease_id) is not None
        client.failed = False
        manager.close_workspace_clients(tmp_path)
        assert key not in manager._ENTRIES
        assert capability_leases().get(lease.lease_id) is None
    finally:
        with manager._LOCK:
            manager._ENTRIES.pop(key, None)
        capability_leases().release(lease.lease_id)


def test_lsp_close_after_protocol_failure_still_reaps_process():
    import os
    import threading

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=(os.name != "nt"),
    )
    client = _inert_client()
    client.process = process
    client.server_id = "closed"
    client._closed = True
    client._close_complete = False
    client._reader = threading.Thread()
    client._stderr_reader = threading.Thread()
    try:
        client.close(force=True)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def test_lsp_diagnostics_rejects_delayed_old_document_version(tmp_path):
    from nz_coder.lsp.client import LSPResponseError
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    source = tmp_path / "source.py"
    source.write_text("first")
    access = WorkspaceFileAccess(tmp_path)
    text, identity = access.read_text_with_identity("source.py")
    client = _inert_client()
    client.root = tmp_path.resolve()
    client.language_id = "python"
    client.diagnostic_wait = 0.02
    client.notify = lambda *_args, **_kwargs: None
    client.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        LSPResponseError({"code": -1})
    )
    old_version = client.open_document(source, text, identity)
    source.write_text("second")
    text, identity = access.read_text_with_identity("source.py")
    client.open_document(source, text, identity)
    client._handle_message(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": source.as_uri(),
                "version": old_version,
                "diagnostics": [{"message": "stale error"}],
            },
        }
    )
    assert client.diagnostics(source, text, identity).to_dict()["items"] == []


def test_lsp_current_push_before_notify_returns_and_reopen_version(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    source = tmp_path / "source.py"
    source.write_text("hello")
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("source.py")
    client = _inert_client()
    client.root = tmp_path.resolve()
    client.language_id = "python"

    def notify(method, params, **kwargs):
        if method == "textDocument/didOpen":
            document = params["textDocument"]
            client._handle_message(
                {
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": document["uri"],
                        "version": document["version"],
                        "diagnostics": [{"message": "current"}],
                    },
                }
            )

    client.notify = notify
    first = client.open_document(source, text, identity)
    assert json.loads(client._diagnostics[source.as_uri()]["current"][2]) == [
        {"message": "current"}
    ]
    client.close_document(source)
    second = client.open_document(source, text, identity)
    assert second > first
    client._handle_message(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": source.as_uri(),
                "version": first,
                "diagnostics": [{"message": "stale"}],
            },
        }
    )
    client._handle_message(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": source.as_uri(),
                "diagnostics": [{"message": "unversioned"}],
            },
        }
    )
    assert json.loads(client._diagnostics[source.as_uri()]["current"][2]) == [
        {"message": "current"}
    ]


def test_lsp_send_timeout_includes_write_lock_wait():
    import os
    from nz_coder.lsp.client import LSPTimeoutError

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=(os.name != "nt"),
    )
    client = _inert_client()
    client.process, client.server_id, client.request_timeout = process, "lock", 0.05
    client._closed = False
    client._write_lock.acquire()
    try:
        with pytest.raises(LSPTimeoutError):
            client.request("blocked", timeout=0.05)
        assert not client._pending
    finally:
        client._write_lock.release()
        process.kill()
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group")
def test_lsp_force_close_kills_descendant_after_leader_exit(tmp_path):
    import os
    import signal
    import threading
    import time

    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess,sys; "
        'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
        'open(sys.argv[1],"w").write(str(p.pid))'
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(pidfile)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    client = _inert_client()
    client.process, client.server_id = process, "orphan"
    client._closed, client._close_complete = False, False
    client._reader = threading.Thread(target=lambda: process.stdout.read())
    client._stderr_reader = threading.Thread(target=lambda: process.stderr.read())
    client._reader.start()
    client._stderr_reader.start()
    child = None
    try:
        process.wait(timeout=2)
        child = int(pidfile.read_text())
        client.close(force=True)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{child}/stat").read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("descendant is still running")
        assert not client._reader.is_alive() and not client._stderr_reader.is_alive()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        client._reader.join(2)
        client._stderr_reader.join(2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def test_lsp_pull_response_is_fenced_by_current_document_identity(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    source = tmp_path / "source.py"
    source.write_text("old")
    access = WorkspaceFileAccess(tmp_path)
    old_text, old_identity = access.read_text_with_identity("source.py")
    client = _inert_client()
    client.root, client.language_id = tmp_path.resolve(), "python"
    client._next_document_version, client.diagnostic_wait = 0, 0.02
    client.notify = lambda *_args, **_kwargs: None

    def delayed_pull(*_args, **_kwargs):
        source.write_text("new")
        text, identity = access.read_text_with_identity("source.py")
        client.open_document(source, text, identity)
        return {"kind": "full", "items": [{"message": "old pull"}]}

    client.request = delayed_pull
    assert client.diagnostics(source, old_text, old_identity).status == "superseded"
    assert source.as_uri() not in client._diagnostics


def test_lsp_reader_cannot_complete_its_own_cleanup():
    import os
    import threading
    from nz_coder.lsp.client import LSPError

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=(os.name != "nt"),
    )
    client = _inert_client()
    client.process, client.server_id = process, "self-close"
    client._closed, client._close_complete = False, False
    client._stderr_reader = threading.Thread()
    errors = []

    def reader_close():
        try:
            client.close(force=True)
        except LSPError as exc:
            errors.append(exc)

    client._reader = threading.Thread(target=reader_close)
    client._reader.start()
    try:
        client._reader.join(2)
        assert not client._reader.is_alive()
        assert errors and not client._close_complete
        client.close(force=True)
        assert client._close_complete
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def test_lsp_cleanup_failure_keeps_live_process_and_lease_for_retry(
    monkeypatch, tmp_path
):
    import os
    import threading
    import nz_coder.lsp.client as client_module
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.lsp import manager

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=(os.name != "nt"),
    )
    client = _inert_client()
    client.process, client.server_id = process, "retry"
    client._closed, client._close_complete = False, False
    client._reader = threading.Thread(target=lambda: process.stdout.read())
    client._stderr_reader = threading.Thread(target=lambda: process.stderr.read())
    client._reader.start()
    client._stderr_reader.start()
    key = (str(tmp_path.resolve()), "retry", str(tmp_path))
    lease = capability_leases().create(
        kind="lsp-client",
        resource_id="retry",
        workspace=tmp_path,
        control_fingerprint="test",
        run_id="test",
        interaction_id="test",
        owner_session="test",
        revoke=lambda: None,
    )
    entry = _registered_client(tmp_path, client, key, lease.lease_id)
    terminate = client_module.terminate_process_tree
    try:
        monkeypatch.setattr(
            client_module, "terminate_process_tree", lambda *args, **kwargs: False
        )
        with pytest.raises(client_module.LSPError, match="cleanup pending"):
            manager._close_entry(entry, force=True)
        assert process.poll() is None
        assert manager._ENTRIES[key].client is client
        assert capability_leases().get(lease.lease_id) is not None
        monkeypatch.setattr(client_module, "terminate_process_tree", terminate)
        manager._close_entry(entry, force=True)
        assert process.poll() is not None
        assert not client._reader.is_alive() and not client._stderr_reader.is_alive()
        assert key not in manager._ENTRIES
        assert capability_leases().get(lease.lease_id) is None
    finally:
        monkeypatch.setattr(client_module, "terminate_process_tree", terminate)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        client._reader.join(2)
        client._stderr_reader.join(2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
        manager._ENTRIES.pop(key, None)
        capability_leases().release(lease.lease_id)
