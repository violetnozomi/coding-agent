"""LSP 的真实 stdio、诊断代次和取消边界回归。"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.lsp.client import LSPClient


SERVER = r"""
import json, sys, time
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: return None
        if line in (b'\n', b'\r\n'): break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
def send(value):
    data = json.dumps(value).encode()
    sys.stdout.buffer.write(('Content-Length: %d\r\n\r\n' % len(data)).encode()+data)
    sys.stdout.buffer.flush()
while True:
    msg = read()
    if msg is None: break
    method = msg.get('method'); args = msg.get('params') or {}; result = None
    if method == 'initialize':
        result = {'capabilities': {'textDocumentSync': 1}}
    elif method in ('textDocument/didOpen', 'textDocument/didChange'):
        doc = args['textDocument']
        send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics', 'params': {
            'uri':doc['uri'], 'diagnostics':[{'message':'real warning','severity':2}]}})
    elif method == 'textDocument/hover':
        time.sleep(.3); result = {'contents':'hello'}
    elif method == 'test/echo': result = args
    elif method == 'exit': break
    if 'id' in msg:
        send({'jsonrpc':'2.0','id':msg['id'],'result':result})
"""


def make_client(tmp_path, server=SERVER):
    script = tmp_path / "server.py"
    script.write_text(server)
    return LSPClient(
        server_id="fixture",
        command=(sys.executable, str(script)),
        root=tmp_path,
        language_id="python",
        diagnostic_wait=0.12,
    )


def test_construct_does_not_spawn(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("constructor spawned")
    )
    client = make_client(tmp_path)
    assert client.process is None
    client.close()


def test_versionless_push_is_observed_without_claiming_current(tmp_path):
    from nz_coder.lsp.types import LspOperation

    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    client = make_client(tmp_path)
    client.start(LspOperation(None, time.monotonic() + 2))
    try:
        report = client.diagnostics(
            target,
            reader=lambda: WorkspaceFileAccess(tmp_path).read_text_with_identity(
                "sample.py"
            ),
            operation=LspOperation(None, time.monotonic() + 0.15),
        )
        assert report.status == "partial"
        assert report.freshness == "unversioned"
        assert report.to_dict()["items"][0]["message"] == "real warning"
        snapshot = report.to_dict()
        snapshot["items"].clear()
        assert report.to_dict()["items"]
    finally:
        client.close()


def test_cancel_wait_does_not_retire_healthy_client(tmp_path):
    from nz_coder.lsp.types import LspOperation
    from nz_coder.lsp.client import LSPCancelledError

    client = make_client(tmp_path)
    client.start(LspOperation(None, time.monotonic() + 2))
    event = threading.Event()
    timer = threading.Timer(0.025, event.set)
    try:
        timer.start()
        started = time.monotonic()
        with pytest.raises(LSPCancelledError):
            client.request(
                "textDocument/hover", {}, operation=LspOperation(event, started + 1)
            )
        assert time.monotonic() - started < 0.18
        assert not client._closed
        assert client.request("test/echo", {"value": 42}) == {"value": 42}
    finally:
        timer.join()
        client.close()


def test_unchanged_document_keeps_version_and_versioned_snapshot(tmp_path):
    from nz_coder.lsp.types import LspOperation

    target = tmp_path / "sample.py"
    target.write_text("x=1\n")
    client = make_client(tmp_path)
    client.start(LspOperation(None, time.monotonic() + 2))
    try:
        text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity(
            "sample.py"
        )
        first = client.open_document(target, text, identity)
        assert client.open_document(target, text, identity) == first
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": target.as_uri(), "version": first, "diagnostics": []},
            }
        )
        report = client.diagnostics(
            target,
            reader=lambda: (text, identity),
            operation=LspOperation(None, time.monotonic() + 0.05),
        )
        assert report.status == "ready"
        assert report.freshness == "current"
    finally:
        client.close()


def test_no_content_sync_is_explicitly_unsupported(tmp_path):
    from nz_coder.lsp.types import LspOperation

    target = tmp_path / "sample.py"
    target.write_text("x=1")
    client = make_client(
        tmp_path, SERVER.replace("'textDocumentSync': 1", "'textDocumentSync': 0")
    )
    client.start(LspOperation(None, time.monotonic() + 2))
    try:
        report = client.diagnostics(
            target,
            reader=lambda: WorkspaceFileAccess(tmp_path).read_text_with_identity(
                "sample.py"
            ),
        )
        assert report.status == "unsupported"
        assert report.freshness == "not_observed"
        assert report.reason_code == "sync_unavailable"
    finally:
        client.close()


def test_manager_does_not_serialize_unrelated_initializations(tmp_path, monkeypatch):
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import ResolvedServer
    from nz_coder.lsp.types import LspOperation
    from nz_coder.foundation.workspace_trust import current_config_snapshot

    slow = tmp_path / "slow"
    fast = tmp_path / "fast"
    slow.mkdir()
    fast.mkdir()
    gate = threading.Event()
    entered = threading.Event()

    def resolve(path, workspace, **kwargs):
        return ResolvedServer(
            server_id="fake", language_id="python", command=("fake",), root=workspace
        )

    class Client:
        def __init__(self, **kwargs):
            self.root = kwargs["root"]
            self._closed = False
            self.process = None

        def start(self, operation):
            if self.root == slow:
                entered.set()
                gate.wait(1)
            self.process = type("Process", (), {"poll": lambda self: None})()

        def close(self, **kwargs):
            self._closed = True

    monkeypatch.setattr(manager, "resolve_server", resolve)
    monkeypatch.setattr(manager, "LSPClient", Client)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            manager.get_client_for_file(
                slow / "a.py",
                slow,
                config_snapshot=current_config_snapshot(slow),
                operation=LspOperation(None, time.monotonic() + 2),
            )
        )
    )
    thread.start()
    try:
        assert entered.wait(0.3)
        start = time.monotonic()
        fast_client = manager.get_client_for_file(
            fast / "a.py",
            fast,
            config_snapshot=current_config_snapshot(fast),
            operation=LspOperation(None, time.monotonic() + 0.15),
        )
        assert fast_client is not None
        assert time.monotonic() - start < 0.2
    finally:
        gate.set()
        thread.join()
        manager.close_all_clients()


def test_cancel_before_document_frame_does_not_cache_unsent_version(tmp_path):
    from nz_coder.lsp.client import LSPCancelledError

    target = tmp_path / "sample.py"
    target.write_text("x=1")
    client = make_client(tmp_path)
    client.capabilities = {"capabilities": {"textDocumentSync": 1}}
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("sample.py")

    def cancelled(*a, **k):
        raise LSPCancelledError("cancelled before send")

    client.notify = cancelled
    with pytest.raises(LSPCancelledError):
        client.open_document(target, text, identity)
    sent = []
    client.notify = lambda *a, **k: sent.append(a)
    client.open_document(target, text, identity)
    assert sent[0][0] == "textDocument/didOpen"


def test_provider_timeout_after_replacement_is_superseded(tmp_path):
    from nz_coder.lsp.client import LSPTimeoutError
    from nz_coder.lsp.types import LspOperation

    target = tmp_path / "sample.py"
    target.write_text("old")
    access = WorkspaceFileAccess(tmp_path)
    text, identity = access.read_text_with_identity("sample.py")
    client = make_client(tmp_path)
    client.capabilities = {
        "capabilities": {
            "textDocumentSync": 1,
            "diagnosticProvider": {"identifier": "one"},
        }
    }
    client._server_request_result(
        "client/registerCapability",
        {
            "registrations": [
                {
                    "id": "two",
                    "method": "textDocument/diagnostic",
                    "registerOptions": {"identifier": "two"},
                }
            ]
        },
    )
    client.notify = lambda *a, **k: None

    def request(method, params, **kwargs):
        if params["identifier"] == "one":
            return {"kind": "full", "items": [{"message": "old"}]}
        target.write_text("new")
        fresh, stamp = access.read_text_with_identity("sample.py")
        client.open_document(target, fresh, stamp)
        raise LSPTimeoutError("expired")

    client.request = request
    report = client.diagnostics(
        target, text, identity, operation=LspOperation(None, time.monotonic() + 1)
    )
    assert report.status == "superseded"
    assert report.freshness == "not_observed"
    assert not report.to_dict()["items"]


def test_pull_deadline_preserves_received_push(tmp_path):
    from nz_coder.lsp.client import LSPTimeoutError
    from nz_coder.lsp.types import LspOperation

    target = tmp_path / "sample.py"
    target.write_text("x=1")
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("sample.py")
    client = make_client(tmp_path)
    client.capabilities = {
        "capabilities": {"textDocumentSync": 1, "diagnosticProvider": {}}
    }

    def notify(method, params, **kwargs):
        if method == "textDocument/didOpen":
            client._handle_message(
                {
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": target.as_uri(),
                        "diagnostics": [{"message": "push error"}],
                    },
                }
            )

    client.notify = notify
    client.request = lambda *a, **k: (_ for _ in ()).throw(LSPTimeoutError("expired"))
    report = client.diagnostics(
        target, text, identity, operation=LspOperation(None, time.monotonic() + 0.1)
    )
    assert report.status == "partial"
    assert report.to_dict()["items"][0]["message"] == "push error"


def test_latest_unversioned_error_is_not_discarded_after_empty_current_push(tmp_path):
    from nz_coder.lsp.types import LspOperation

    target = tmp_path / "sample.py"
    target.write_text("x=1")
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("sample.py")
    client = make_client(tmp_path)
    client.capabilities = {"capabilities": {"textDocumentSync": 1}}

    def notify(method, params, **kwargs):
        if method == "textDocument/didOpen":
            client._handle_message(
                {
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": target.as_uri(),
                        "version": params["textDocument"]["version"],
                        "diagnostics": [],
                    },
                }
            )
            client._handle_message(
                {
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": target.as_uri(),
                        "diagnostics": [{"message": "new error"}],
                    },
                }
            )

    client.notify = notify
    report = client.diagnostics(
        target, text, identity, operation=LspOperation(None, time.monotonic() + 0.02)
    )
    assert report.status == "partial" and report.freshness == "unversioned"
    assert report.to_dict()["items"][0]["message"] == "new error"


def test_relative_selector_does_not_match_deeper_directory(tmp_path):
    from nz_coder.lsp.client import _selector_matches

    assert not _selector_matches(
        [{"pattern": {"baseUri": tmp_path.as_uri(), "pattern": "*.py"}}],
        tmp_path / "nested/a.py",
        "python",
    )
    assert _selector_matches(
        [{"pattern": {"baseUri": tmp_path.as_uri(), "pattern": "**/*.py"}}],
        tmp_path / "nested/a.py",
        "python",
    )


def test_cancel_after_complete_frame_keeps_server_alive(tmp_path, monkeypatch):
    import os
    from nz_coder.lsp.types import LspOperation

    client = make_client(tmp_path)
    client.start()
    event = threading.Event()
    original = os.write

    def write(fd, data):
        count = original(fd, data)
        if fd == client.process.stdin.fileno() and count == len(data):
            event.set()
        return count

    monkeypatch.setattr(os, "write", write)
    try:
        client._send(
            {"jsonrpc": "2.0", "method": "test/notification"},
            operation=LspOperation(event, time.monotonic() + 1),
        )
        assert event.is_set()
        assert not client._closed
    finally:
        monkeypatch.setattr(os, "write", original)
        client.close()


def test_multiple_diagnostic_identifiers_aggregate_and_revocation_fences(tmp_path):
    from nz_coder.lsp.types import LspOperation

    target = tmp_path / "a.py"
    target.write_text("x=1")
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("a.py")
    client = make_client(tmp_path)
    client.capabilities = {
        "capabilities": {
            "textDocumentSync": 1,
            "diagnosticProvider": {"identifier": "one"},
        }
    }
    registration = {
        "id": "second",
        "method": "textDocument/diagnostic",
        "registerOptions": {
            "identifier": "two",
            "documentSelector": [{"language": "python"}],
        },
    }
    client._server_request_result(
        "client/registerCapability", {"registrations": [registration]}
    )
    client.notify = lambda *a, **k: None
    seen = []

    def request(method, params, **kwargs):
        seen.append(params["identifier"])
        return {
            "kind": "full",
            "items": []
            if params["identifier"] == "one"
            else [{"message": "second error"}],
        }

    client.request = request
    report = client.diagnostics(
        target, text, identity, operation=LspOperation(None, time.monotonic() + 0.1)
    )
    assert report.status == "ready" and report.to_dict()["items"] == [
        {"message": "second error"}
    ]
    assert sorted(seen) == ["one", "two"]

    def revoke(method, params, **kwargs):
        client._server_request_result(
            "client/unregisterCapability",
            {
                "unregisterations": [
                    {"id": "second", "method": "textDocument/diagnostic"}
                ]
            },
        )
        return {"kind": "full", "items": []}

    client.request = revoke
    assert client.diagnostics(target, text, identity).status == "superseded"


def test_incremental_range_counts_cr_and_emoji_with_lsp_line_rules(tmp_path):
    target = tmp_path / "a.py"
    target.write_bytes("a\rb😀\r\n".encode())
    access = WorkspaceFileAccess(tmp_path)
    text, identity = access.read_text_with_identity("a.py")
    client = make_client(tmp_path)
    client.capabilities = {"capabilities": {"textDocumentSync": 2}}
    sent = []
    client.notify = lambda method, params, **kwargs: sent.append((method, params))
    client.open_document(target, text, identity)
    target.write_text("new")
    client.open_document(target, *access.read_text_with_identity("a.py"))
    assert sent[-1][1]["contentChanges"][0]["range"]["end"] == {
        "line": 2,
        "character": 0,
    }


def test_request_stage_limit_does_not_renew_overall_deadline(tmp_path):
    from nz_coder.lsp.client import LSPTimeoutError
    from nz_coder.lsp.types import LspOperation

    client = make_client(tmp_path)
    client.start()
    client.request_timeout = 0.025
    try:
        start = time.monotonic()
        with pytest.raises(LSPTimeoutError):
            client.request(
                "textDocument/hover", {}, operation=LspOperation(None, start + 1)
            )
        assert time.monotonic() - start < 0.15
        assert not client._closed
    finally:
        client.close(force=True)


def test_short_wait_does_not_cancel_owned_initialization(tmp_path, monkeypatch):
    from nz_coder.lsp import manager
    from nz_coder.lsp.types import LspOperation
    from nz_coder.lsp.client import LSPTimeoutError

    script = tmp_path / "slow_server.py"
    script.write_text(
        SERVER.replace(
            "if method == 'initialize':",
            "if method == 'initialize':\n        time.sleep(.12)",
        )
    )
    (tmp_path / "a.py").write_text("x=1")
    monkeypatch.setenv("NZ_LSP_PYTHON_COMMAND", f"{sys.executable} {script}")
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(tmp_path.parent / (tmp_path.name + "-trust.json")),
    )
    from nz_coder.lsp.servers import trust_server

    trust_server(tmp_path / "a.py", tmp_path)
    try:
        with pytest.raises(LSPTimeoutError):
            manager.get_client_for_file(
                tmp_path / "a.py",
                tmp_path,
                operation=LspOperation(None, time.monotonic() + 0.02),
            )
        row = manager.client_status_summary(tmp_path)[0]
        assert row["status"] == "starting"
        client = manager.get_client_for_file(
            tmp_path / "a.py",
            tmp_path,
            operation=LspOperation(None, time.monotonic() + 1),
        )
        assert client is not None
        assert manager.client_status_summary(tmp_path)[0]["entry_id"] == row["entry_id"]
    finally:
        manager.close_workspace_clients(tmp_path)


def test_unsupported_dynamic_selector_is_rejected_explicitly(tmp_path):
    from nz_coder.lsp.client import LSPResponseError

    client = make_client(tmp_path)
    with pytest.raises(LSPResponseError) as error:
        client._server_request_result(
            "client/registerCapability",
            {
                "registrations": [
                    {
                        "id": "unknown",
                        "method": "textDocument/diagnostic",
                        "registerOptions": {"documentSelector": [{"notebook": "file"}]},
                    }
                ]
            },
        )
    assert error.value.error["code"] == -32602


def test_initialize_keeps_its_own_stage_budget(tmp_path):
    from nz_coder.lsp.types import LspOperation

    slow = SERVER.replace(
        "if method == 'initialize':",
        "if method == 'initialize':\n        time.sleep(.06)",
    )
    client = make_client(tmp_path, slow)
    client.initialize_timeout = 0.25
    client.request_timeout = 0.01
    try:
        client.start(LspOperation(None, time.monotonic() + 0.25))
        assert client.process.poll() is None
    finally:
        client.close(force=True)


def test_retired_queries_cannot_restart_until_actual_owner_acquires(
    tmp_path, monkeypatch
):
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import trust_server
    from nz_coder.intelligence.service import (
        acquire_repo_intelligence,
        release_repo_intelligence,
    )

    script = tmp_path / "server.py"
    script.write_text(SERVER)
    source = tmp_path / "a.py"
    source.write_text("x=1")
    monkeypatch.setenv("NZ_LSP_PYTHON_COMMAND", f"{sys.executable} {script}")
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(tmp_path.parent / (tmp_path.name + "-trust.json")),
    )
    trust_server(source, tmp_path)
    client = manager.get_client_for_file(source, tmp_path)
    manager.close_workspace_clients(tmp_path)
    acquired = False
    try:
        assert manager.get_client_for_file(source, tmp_path) is None
        assert not manager.client_status_summary(tmp_path)
        acquire_repo_intelligence(tmp_path, max_files=10)
        acquired = True
        replacement = manager.get_client_for_file(source, tmp_path)
        assert replacement is not None and replacement.process.pid != client.process.pid
    finally:
        if acquired:
            release_repo_intelligence(tmp_path)
        else:
            manager.close_workspace_clients(tmp_path)


def test_windows_tree_termination_uses_remaining_close_deadline():
    from types import SimpleNamespace
    from nz_coder.runtime.process.platform_runtime import terminate_process_tree

    timeouts = []

    def runner(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0)

    process = SimpleNamespace(pid=42, kill=lambda: None)
    assert terminate_process_tree(
        process, os_name="nt", runner=runner, deadline=time.monotonic() + 0.03
    )
    assert 0 < timeouts[0] <= 0.03
    assert not terminate_process_tree(
        process, os_name="nt", runner=runner, deadline=time.monotonic() - 1
    )
    assert len(timeouts) == 1


def test_unimplemented_dynamic_watched_files_is_not_acknowledged(tmp_path):
    from nz_coder.lsp.client import LSPResponseError

    client = make_client(tmp_path)
    with pytest.raises(LSPResponseError):
        client._server_request_result(
            "client/registerCapability",
            {
                "registrations": [
                    {
                        "id": "watch",
                        "method": "workspace/didChangeWatchedFiles",
                        "registerOptions": {"watchers": []},
                    }
                ]
            },
        )


def test_exit_replacement_cannot_reopen_workspace_retired_during_cleanup(
    tmp_path, monkeypatch
):
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import trust_server

    script = tmp_path / "server.py"
    script.write_text(SERVER)
    source = tmp_path / "a.py"
    source.write_text("x=1")
    monkeypatch.setenv("NZ_LSP_PYTHON_COMMAND", f"{sys.executable} {script}")
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(tmp_path.parent / (tmp_path.name + "-trust.json")),
    )
    trust_server(source, tmp_path)
    client = manager.get_client_for_file(source, tmp_path)
    client.process.kill()
    client.process.wait(timeout=2)
    close = manager._close_entry

    def close_then_retire(entry, **kwargs):
        close(entry, **kwargs)
        manager.retire_workspace_clients(tmp_path)

    monkeypatch.setattr(manager, "_close_entry", close_then_retire)
    try:
        assert manager.get_client_for_file(source, tmp_path) is None
        assert not manager.client_status_summary(tmp_path)
    finally:
        manager.close_workspace_clients(tmp_path)
