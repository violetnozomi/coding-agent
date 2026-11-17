"""工具层对诊断未知状态、完整输出和单次预算的呈现。"""

import json
from types import SimpleNamespace

from nz_coder.runtime.process.workdir import scoped_workdir


def test_lsp_output_limit_keeps_valid_envelope(tmp_path, monkeypatch):
    from nz_coder.tools import lsp as tool

    (tmp_path / "a.py").write_text("a=1")
    operations = []

    class Client:
        def open_document(self, path, *, reader, operation):
            reader()
            operations.append(operation)

        def request(self, *a, operation, **k):
            operations.append(operation)
            return [
                {"uri": (tmp_path / "a.py").as_uri(), "range": {}, "data": "x" * 1000}
            ]

    monkeypatch.setattr(tool, "get_client_for_file", lambda *a, **k: Client())
    actual = tool.current_run_settings()
    monkeypatch.setattr(
        tool,
        "current_run_settings",
        lambda: SimpleNamespace(
            lsp_enabled=True,
            lsp_max_output_chars=1,
            lsp_initialize_timeout=actual.lsp_initialize_timeout,
            lsp_request_timeout=actual.lsp_request_timeout,
        ),
    )
    with scoped_workdir(tmp_path):
        result = json.loads(tool.lsp("findReferences", "a.py"))
    assert result["truncated"] is True
    assert result["reason_code"] == "output_limit"
    assert operations[0] is operations[1]


def test_write_diagnostics_missing_server_is_visible(tmp_path, monkeypatch):
    from nz_coder.lsp import write_diagnostics as writer

    (tmp_path / "a.py").write_text("a=1")
    monkeypatch.setattr(writer, "get_client_for_file", lambda *a, **k: None)
    monkeypatch.setattr(
        writer,
        "current_run_settings",
        lambda: SimpleNamespace(
            lsp_enabled=True,
            lsp_write_diagnostics_enabled=True,
            lsp_diagnostic_wait=0.1,
            lsp_write_diagnostic_max_files=8,
            lsp_max_output_chars=4000,
        ),
    )
    batch = writer.collect_write_diagnostics(["a.py"], tmp_path)
    assert batch["reports"][0]["status"] == "unavailable"
    assert "unavailable/not_observed" in writer.format_write_diagnostics(batch)


def test_diagnostic_output_keeps_status(tmp_path, monkeypatch):
    from nz_coder.tools import lsp as tool
    from nz_coder.lsp.types import DiagnosticReport

    (tmp_path / "a.py").write_text("a=1")

    class Client:
        def diagnostics(self, path, *, reader, operation):
            reader()
            return DiagnosticReport(
                "a.py", "timeout", "not_observed", reason_code="deadline_exceeded"
            )

    monkeypatch.setattr(tool, "get_client_for_file", lambda *a, **k: Client())
    with scoped_workdir(tmp_path):
        result = json.loads(tool.lsp("diagnostics", "a.py"))
    assert result["status"] == "timeout"
    assert result["freshness"] == "not_observed"
    assert result["items"] == []


def test_symbol_ref_cannot_rebind_id_to_identical_other_file(tmp_path, monkeypatch):
    from nz_coder.tools import lsp as tool
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.intelligence import service

    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("def value(): pass\n")
    text, identity = WorkspaceFileAccess(tmp_path).read_text_with_identity("a.py")
    selection = {
        "start": {"line": 0, "character": 4},
        "end": {"line": 0, "character": 9},
        "encoding": "utf-32",
    }
    actual = {
        "symbol_id": "id-a",
        "file_path": "a.py",
        "content_hash": identity.content_hash,
        "selection": selection,
    }
    requests = []

    class Client:
        position_encoding = "utf-16"

        def open_document(self, path, *, reader, operation):
            reader()
            return 0

        def request(self, *a, **k):
            requests.append(a)
            return {"contents": "type"}

        def document_matches(self, *a):
            return True

    monkeypatch.setattr(tool, "get_client_for_file", lambda *a, **k: Client())
    monkeypatch.setattr(
        service,
        "workspace_repo_intelligence",
        lambda *a: SimpleNamespace(index=SimpleNamespace(symbol_ref=lambda id: actual)),
    )
    with scoped_workdir(tmp_path):
        output = tool.lsp("hover", symbol_ref={**actual, "file_path": "b.py"})
    assert output.startswith("Error:")
    assert not requests


def test_symbol_ref_rejects_explicit_position(tmp_path, monkeypatch):
    from nz_coder.tools import lsp as tool

    monkeypatch.setattr(tool, "get_client_for_file", lambda *a, **k: None)
    with scoped_workdir(tmp_path):
        output = tool.lsp(
            "hover",
            line=2,
            symbol_ref={
                "symbol_id": "a",
                "file_path": "a.py",
                "content_hash": "hash",
                "selection": {},
            },
        )
    assert "mutually exclusive" in output


def test_large_hover_returns_complete_location_hint(tmp_path, monkeypatch):
    from nz_coder.tools import lsp as tool

    (tmp_path / "a.py").write_text("a=1")

    class Client:
        def open_document(self, path, *, reader, operation):
            reader()

        def request(self, *a, **k):
            return {"contents": {"value": "x" * 10000}}

    monkeypatch.setattr(tool, "get_client_for_file", lambda *a, **k: Client())
    current = tool.current_run_settings()
    monkeypatch.setattr(
        tool,
        "current_run_settings",
        lambda: SimpleNamespace(
            lsp_enabled=True,
            lsp_max_output_chars=400,
            lsp_initialize_timeout=current.lsp_initialize_timeout,
            lsp_request_timeout=current.lsp_request_timeout,
        ),
    )
    with scoped_workdir(tmp_path):
        output = tool.lsp("hover", "a.py")
    value = json.loads(output)
    assert value["truncated"] is True
    assert value["items"][0]["path"] == "a.py"
    assert len(output) <= 400


def test_diagnostic_clipping_keeps_report_contract():
    from nz_coder.tools.lsp import _bounded_output
    from nz_coder.lsp.types import DiagnosticReport

    report = DiagnosticReport(
        "a.py",
        "ready",
        "current",
        items_json='[{"message":"one"},{"message":"two"}]',
        requested_version=1,
    ).to_dict()
    report["total_if_known"] = 2
    limited = json.loads(_bounded_output(report, 2000, limit=1))
    assert limited["status"] == "partial" and limited["truncated"] is True
    timeout = DiagnosticReport("a.py", "timeout", "not_observed").to_dict()
    timeout["total_if_known"] = None
    tiny = json.loads(_bounded_output(timeout, 1))
    assert tiny["status"] == "timeout" and tiny["freshness"] == "not_observed"
    assert tiny["path"] == "a.py" and tiny["truncated"] is True


def test_document_symbol_result_path_uses_its_query_anchor(tmp_path, monkeypatch):
    from nz_coder.tools import lsp as tool

    (tmp_path / "a.py").write_text("def value(): pass")

    class Client:
        def open_document(self, path, *, reader, operation):
            reader()

        def request(self, *args, **kwargs):
            return [{"name": "value", "kind": 12, "range": {}, "selectionRange": {}}]

    monkeypatch.setattr(tool, "get_client_for_file", lambda *a, **k: Client())
    with scoped_workdir(tmp_path):
        result = json.loads(tool.lsp("documentSymbol", "a.py", result_path="a.py"))
    assert result["total_if_known"] == 1
    assert result["items"][0]["name"] == "value"


def test_diagnostics_missing_client_still_returns_structured_report(
    tmp_path, monkeypatch
):
    from nz_coder.tools import lsp as tool

    (tmp_path / "a.py").write_text("a=1")
    monkeypatch.setattr(tool, "get_client_for_file", lambda *a, **k: None)
    with scoped_workdir(tmp_path):
        result = json.loads(tool.lsp("diagnostics", "a.py"))
    assert result["status"] == "unavailable" and result["freshness"] == "not_observed"
    assert result["items"] == [] and result["requested_version"] is None
