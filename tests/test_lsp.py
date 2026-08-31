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
                    "diagnosticProvider": True,
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
def test_lsp_client_rejects_invalid_timeout_before_spawn(tmp_path, monkeypatch, timeout):
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


def test_close_workspace_clients_preserves_other_workspaces(tmp_path):
    from nz_coder.lsp import manager

    first_root = (tmp_path / "first").resolve()
    second_root = (tmp_path / "second").resolve()
    first_root.mkdir()
    second_root.mkdir()

    class Client:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    first = Client()
    second = Client()
    first_key = (str(first_root), "python", str(first_root))
    second_key = (str(second_root), "python", str(second_root))
    manager.close_all_clients()
    try:
        manager._CLIENTS[first_key] = first
        manager._CLIENTS[second_key] = second
        manager._BROKEN.update((first_key, second_key))
        manager._ERRORS.update({first_key: "first", second_key: "second"})

        manager.close_workspace_clients(first_root)

        assert first.closed is True
        assert second.closed is False
        assert first_key not in manager._CLIENTS
        assert second_key in manager._CLIENTS
        assert first_key not in manager._BROKEN
        assert second_key in manager._BROKEN
        assert first_key not in manager._ERRORS
        assert second_key in manager._ERRORS
    finally:
        manager.close_all_clients()


def test_lsp_tool_roundtrip_normalizes_paths_and_positions(tmp_path, monkeypatch):
    from nz_coder.tools.lsp import lsp

    server = _write_fake_server(tmp_path)
    monkeypatch.setenv(
        "NZ_LSP_PYTHON_COMMAND",
        f"{sys.executable} -u {server}",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")

    close_all_clients()
    try:
        with scoped_workdir(tmp_path):
            definition = json.loads(lsp(
                "goToDefinition",
                "app.py",
                line=1,
                character=3,
            ))
            references = json.loads(lsp(
                "findReferences",
                "app.py",
                line=1,
                character=3,
            ))
            diagnostics = json.loads(lsp("diagnostics", "app.py"))

        assert definition["path"] == "app.py"
        assert definition["range"]["start"] == {"line": 0, "character": 2}
        assert len(references) == 2
        assert diagnostics[0]["message"] == "fake warning"
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

    close_all_clients()
    try:
        with scoped_workdir(tmp_path):
            hover = json.loads(lsp("hover", "app.py"))
            symbols = json.loads(lsp("documentSymbol", "app.py"))
            workspace = json.loads(lsp(
                "workspaceSymbol",
                "app.py",
                query="answer",
            ))
            calls = json.loads(lsp("incomingCalls", "app.py"))

        assert hover["contents"]["value"] == "`int`"
        assert symbols[0]["name"] == "answer"
        assert workspace[0]["location"]["path"] == "app.py"
        assert calls[0]["from"]["path"] == "app.py"
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

    close_all_clients()
    try:
        with scoped_workdir(tmp_path):
            result = lsp("hover", "app.py")
        assert result.startswith("Error: LSP server failed to initialize:")
        assert "broken server" in result
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

    decision = PermissionManager("default").check("lsp", {
        "operation": "hover",
        "file_path": "app.py",
    })

    assert decision == {"behavior": "allow", "reason": "Safe tool"}


def test_python_package_project_root_promotes_parent_import_path(tmp_path, monkeypatch):
    from nz_coder.lsp.servers import resolve_server

    package = tmp_path / "cron_engine"
    tests = package / "tests"
    tests.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "pyproject.toml").write_text("[project]\nname='cron-engine'\n", encoding="utf-8")
    target = tests / "test_parser.py"
    target.write_text("from cron_engine.parser import parse\n", encoding="utf-8")
    monkeypatch.setenv("NZ_LSP_PYTHON_COMMAND", sys.executable)

    resolved = resolve_server(target, tmp_path)

    assert resolved is not None
    assert resolved.root == tmp_path.resolve()
    assert resolved.analysis_paths == ()


def test_write_diagnostics_use_real_protocol_and_normalize_locations(
    tmp_path, monkeypatch,
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

    close_all_clients()
    try:
        block = collect_write_diagnostics(
            ["app.py", "app.py", "../outside.py"],
            tmp_path,
        )
        assert block.startswith("<lsp-diagnostics>")
        assert "app.py:1:1 [warning] fake warning (fake)" in block
        assert block.count("fake warning") == 1
        assert "outside.py" not in block
    finally:
        close_all_clients()


def test_write_diagnostics_silently_skip_missing_server(tmp_path, monkeypatch):
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
        assert collect_write_diagnostics(["app.py"], tmp_path) == ""
    finally:
        close_all_clients()
