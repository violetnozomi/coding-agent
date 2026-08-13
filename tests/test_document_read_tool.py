"""Source-contract tests for InfCode-aligned PDF/DOCX ``read_file`` reads."""
from __future__ import annotations

from nz_coder.documents import (
    DocumentPageRange,
    parse_document_pages,
    read_document_file,
)
from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.tools import TOOL_SPECS, ToolOutput
from nz_coder.tools.files import read_file

from tests.test_document_preflight import _write_docx


def test_parse_pdf_pages_accepts_infcode_forms_and_enforces_cap():
    assert parse_document_pages("5") == DocumentPageRange(5, 5, "5")
    assert parse_document_pages(" 1-20 ") == DocumentPageRange(1, 20, "1-20")

    for invalid in ("", "0", "3-2", "1-21", "1-2-3", "all"):
        try:
            parse_document_pages(invalid)
        except ValueError:
            continue
        raise AssertionError(f"pages={invalid!r} should be rejected")


def test_read_file_docx_uses_converted_line_pagination(tmp_path):
    document = tmp_path / "report.docx"
    _write_docx(document, ("one", "two", "three", "four"))

    with scoped_workdir(tmp_path):
        result = read_file("report.docx", offset=3, limit=3, pages="ignored")

    assert isinstance(result, ToolOutput)
    assert '<document_read filename="report.docx" path="report.docx">' in result
    assert "two\n\nthree" in result
    assert "one" not in result
    assert "Showing lines 3-5 of 7. Use offset=6 to continue." in result
    assert result.metadata["document_read"]["status"] == "completed"


def test_pdf_without_pages_requires_pagination_above_twenty(
    tmp_path,
    monkeypatch,
):
    pdf = tmp_path / "long.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.setattr("nz_coder.documents._pdf_page_count", lambda *_args: 21)
    monkeypatch.setattr(
        "nz_coder.documents._extract_pdf",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not convert")),
    )

    result = read_document_file(
        "long.pdf",
        workspace=tmp_path,
        session_id="session-read",
    )

    assert result.status == "error"
    assert "PDF has 21 pages" in result.error
    assert 'pages="1-20"' in result.error


def test_pdf_page_ranges_have_independent_sidecars(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    calls = []
    monkeypatch.setattr("nz_coder.documents._pdf_page_count", lambda *_args: 40)

    def extract(_path, _cache, _cancel, start, end):
        calls.append((start, end))
        return f"pages {start}-{end}"

    monkeypatch.setattr("nz_coder.documents._extract_pdf", extract)

    first = read_document_file(
        "paper.pdf",
        workspace=tmp_path,
        session_id="session-read",
        pages="1-20",
    )
    second = read_document_file(
        "paper.pdf",
        workspace=tmp_path,
        session_id="session-read",
        pages="21-40",
    )
    cached_first = read_document_file(
        "paper.pdf",
        workspace=tmp_path,
        session_id="session-read",
        pages="1-20",
    )

    assert first.text == cached_first.text == "pages 1-20"
    assert second.text == "pages 21-40"
    assert calls == [(1, 20), (21, 40)]
    cache = (
        tmp_path / ".nz-coder" / "sessions" / "session-read"
        / "documents" / ".cache"
    )
    assert len(list(cache.glob("*.md"))) == 2


def test_pdf_page_range_cannot_exceed_document(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.setattr("nz_coder.documents._pdf_page_count", lambda *_args: 3)

    result = read_document_file(
        "paper.pdf",
        workspace=tmp_path,
        session_id="session-read",
        pages="3-4",
    )

    assert result.status == "error"
    assert "exceeds document page count (3)" in result.error


def test_read_file_schema_exposes_pdf_pages_parameter():
    spec = next(
        item["function"] for item in TOOL_SPECS
        if item["function"]["name"] == "read_file"
    )

    pages = spec["parameters"]["properties"]["pages"]
    assert pages["type"] == "string"
    assert "Maximum 20 pages" in pages["description"]
