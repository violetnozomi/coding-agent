"""InfCode source-contract tests for text and directory ``read_file``."""
from __future__ import annotations

import threading

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import TOOL_SPECS, ToolOutput
from nz_coder.tools.files import read_file
from nz_coder.tools.read_support import warm_lsp


def test_text_read_defaults_to_2000_lines_and_counts_to_eof(tmp_path, monkeypatch):
    source = tmp_path / "many.txt"
    source.write_text("\n".join(f"line-{index}" for index in range(1, 2002)))
    warmed = []
    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *args: warmed.append(args))

    with scoped_workdir(tmp_path):
        result = read_file("many.txt")

    assert isinstance(result, ToolOutput)
    assert "2000: line-2000" in result
    assert "2001: line-2001" not in result
    assert "Showing lines 1-2000 of 2001. Use offset=2001 to continue." in result
    assert result.metadata["truncated"] is True
    assert warmed == [(source, tmp_path)]


def test_long_line_and_utf8_byte_cap_match_infcode(tmp_path, monkeypatch):
    source = tmp_path / "wide.txt"
    source.write_text("x" * 3000 + "\n" + "界" * 3000 + "\nend", encoding="utf-8")
    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *_args: None)

    with scoped_workdir(tmp_path):
        result = read_file("wide.txt")

    assert "x" * 2000 + "... (line truncated to 2000 chars)" in result
    assert "界" * 2000 + "... (line truncated to 2000 chars)" in result
    assert "End of file - total 3 lines" in result

    capped = tmp_path / "capped.txt"
    capped.write_text("\n".join("界" * 2000 for _ in range(20)), encoding="utf-8")
    with scoped_workdir(tmp_path):
        cut = read_file("capped.txt")
    assert "Output capped at 50 KB" in cut
    assert "Use offset=9 to continue." in cut
    assert cut.metadata["truncated"] is True


def test_text_offset_out_of_range_is_not_silently_clamped(tmp_path):
    (tmp_path / "short.txt").write_text("one\ntwo\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        result = read_file("short.txt", offset=3)

    assert result == "Error: Offset 3 is out of range for this file (2 lines)"


def test_directory_read_is_sorted_paged_and_marks_directories(tmp_path):
    (tmp_path / "z.txt").write_text("z")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".hidden").write_text("h")
    (tmp_path / "folder").mkdir()

    with scoped_workdir(tmp_path):
        first = read_file(".", limit=2)
        second = read_file(".", offset=3, limit=2)

    assert isinstance(first, ToolOutput)
    assert "<type>directory</type>" in first
    assert "\n.hidden\na.txt\n" in first
    assert "Showing 2 of 4 entries" in first
    assert "\nfolder/\nz.txt\n" in second
    assert "(4 entries)" in second


def test_missing_path_has_bounded_sibling_suggestions(tmp_path):
    for name in ("report.md", "report.txt", "report.py", "report.json"):
        (tmp_path / name).write_text(name)

    with scoped_workdir(tmp_path):
        result = read_file("report")

    assert result.startswith("Error: File not found: report")
    assert "Did you mean one of these?" in result
    suggestions = result.split("Did you mean one of these?\n", 1)[1].splitlines()
    assert len(suggestions) == 3


def test_binary_rejection_and_utf16_bom_exception(tmp_path, monkeypatch):
    (tmp_path / "payload.bin").write_bytes(b"plain-looking bytes")
    (tmp_path / "nul.data").write_bytes(b"text\x00binary")
    (tmp_path / "wide.txt").write_text("hello\nworld", encoding="utf-16")
    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *_args: None)

    with scoped_workdir(tmp_path):
        extension = read_file("payload.bin")
        control = read_file("nul.data")
        wide = read_file("wide.txt")

    assert extension == "Error: Cannot read binary file: payload.bin"
    assert control == "Error: Cannot read binary file: nul.data"
    assert "1: hello" in wide
    assert "2: world" in wide
    assert wide.metadata["encoding"] == "utf-16"


def test_legacy_gb18030_fallback_is_model_visible(tmp_path, monkeypatch):
    (tmp_path / "legacy.txt").write_bytes("中文内容\n第二行".encode("gb18030"))
    monkeypatch.setattr("nz_coder.tools.files.warm_lsp", lambda *_args: None)

    with scoped_workdir(tmp_path):
        result = read_file("legacy.txt")

    assert "1: 中文内容" in result
    assert "2: 第二行" in result
    assert result.metadata["encoding"].lower() == "gb18030"


def test_read_file_schema_documents_text_default():
    spec = next(
        item["function"] for item in TOOL_SPECS
        if item["function"]["name"] == "read_file"
    )

    assert "Default: 2000" in spec["parameters"]["properties"]["limit"]["description"]


def test_lsp_warm_is_background_best_effort_and_deduplicated(tmp_path, monkeypatch):
    source = tmp_path / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    calls = []

    class Client:
        def open_document(self, path, text, source_identity):
            calls.append(("open", path))
            completed.set()

    def get_client(path, workspace):
        calls.append(("get", path, workspace))
        started.set()
        release.wait(1)
        return Client()

    monkeypatch.setattr("nz_coder.lsp.get_client_for_file", get_client)

    warm_lsp(source, tmp_path)
    assert started.wait(1)
    warm_lsp(source, tmp_path)
    release.set()
    assert completed.wait(1)

    assert [item[0] for item in calls] == ["get", "open"]
