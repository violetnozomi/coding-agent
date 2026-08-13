"""Source-level parity tests for InfCode's GrepTool/Ripgrep.search chain."""
from __future__ import annotations

import json
import os

from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.tools import ToolOutput
from nz_coder.runtime.ripgrep import decode_ripgrep_event
from nz_coder.tools.search import grep_search


def _match_event(path: str, text: str, line: int, offset: int = 0) -> dict:
    needle = "needle"
    start = text.index(needle)
    return {
        "type": "match",
        "data": {
            "path": {"text": path},
            "lines": {"text": text},
            "line_number": line,
            "absolute_offset": offset,
            "submatches": [{
                "match": {"text": needle},
                "start": start,
                "end": start + len(needle),
            }],
        },
    }


def _fake_rg(tmp_path, events: list[dict] | None = None, *, code: int = 0, raw: str = ""):
    script = tmp_path / f"fake-rg-{code}-{len(events or [])}"
    lines = ["#!/usr/bin/env python3", "import json, sys"]
    if raw:
        lines.append(f"print({raw!r}, flush=True)")
    for event in events or []:
        lines.append(f"print(json.dumps({event!r}), flush=True)")
    lines.append(f"sys.exit({code})")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_grep_default_matches_infcode_grouping_mtime_and_metadata(tmp_path):
    old = tmp_path / "old.py"
    newest = tmp_path / "src" / "new.py"
    newest.parent.mkdir()
    old.write_text("needle old\nplain\nneedle again\n", encoding="utf-8")
    newest.write_text("needle newest\n", encoding="utf-8")
    os.utime(old, (10, 10))
    os.utime(newest, (20, 20))

    with scoped_workdir(tmp_path):
        result = grep_search("needle")

    assert isinstance(result, ToolOutput)
    assert result.startswith("Found 3 matches")
    assert result.index(str(newest)) < result.index(str(old))
    assert "Line 1: needle newest" in result
    assert "Line 3: needle again" in result
    assert result.title == "needle"
    assert result.metadata == {"matches": 3, "truncated": False}


def test_grep_empty_and_exact_file_path_have_infcode_shape(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("first\nneedle\nthird\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        found = grep_search("needle", "source.txt")
        empty = grep_search("absent", "source.txt")

    assert str(source) in found
    assert "Line 2: needle" in found
    assert str(empty) == "No files found"
    assert empty.metadata == {"matches": 0, "truncated": False}


def test_grep_default_truncates_100_matching_rows_after_mtime_sort(tmp_path):
    source = tmp_path / "many.txt"
    source.write_text("".join(f"needle {index}\n" for index in range(101)), encoding="utf-8")

    with scoped_workdir(tmp_path):
        result = grep_search("needle")

    assert result.startswith("Found 101 matches (showing first 100)")
    assert "Results truncated: showing 100 of 101 matches (1 hidden)" in result
    assert result.metadata == {"matches": 101, "truncated": True}


def test_grep_bounds_visible_line_to_2000_characters(tmp_path):
    source = tmp_path / "long.txt"
    source.write_text("needle" + "x" * 3000 + "\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        result = grep_search("needle")

    visible = next(line for line in result.splitlines() if line.startswith("  Line 1:"))
    assert visible.endswith("...")
    assert len(visible.removeprefix("  Line 1: ")) == 2003


def test_grep_code_two_preserves_rows_and_emits_partial_notice(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("needle\n", encoding="utf-8")
    fake = _fake_rg(
        tmp_path,
        [_match_event("source.py", "needle\n", 1)],
        code=2,
    )
    monkeypatch.setattr("nz_coder.runtime.ripgrep.shutil.which", lambda _name: str(fake))

    with scoped_workdir(tmp_path):
        result = grep_search("needle")

    assert "Line 1: needle" in result
    assert "(Some paths were inaccessible and skipped)" in result
    assert result.metadata == {"matches": 1, "truncated": False}


def test_grep_drops_rows_whose_file_disappeared_before_stat(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("needle\n", encoding="utf-8")
    fake = _fake_rg(
        tmp_path,
        [
            _match_event("missing.py", "needle missing\n", 1),
            _match_event("source.py", "needle\n", 1),
        ],
    )
    monkeypatch.setattr("nz_coder.runtime.ripgrep.shutil.which", lambda _name: str(fake))

    with scoped_workdir(tmp_path):
        result = grep_search("needle")

    assert str(source) in result
    assert "missing.py" not in result
    assert result.metadata == {"matches": 1, "truncated": False}


def test_grep_invalid_json_is_an_explicit_tool_error(tmp_path, monkeypatch):
    fake = _fake_rg(tmp_path, raw="not-json")
    monkeypatch.setattr("nz_coder.runtime.ripgrep.shutil.which", lambda _name: str(fake))

    with scoped_workdir(tmp_path):
        result = grep_search("needle")

    assert result.startswith("Error: invalid ripgrep JSON output")


def test_grep_include_case_and_no_rg_fallback(tmp_path, monkeypatch):
    (tmp_path / "one.py").write_text("Needle\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("Needle\n", encoding="utf-8")
    monkeypatch.setattr("nz_coder.runtime.ripgrep.shutil.which", lambda _name: None)

    with scoped_workdir(tmp_path):
        sensitive = grep_search("needle", include="*.py")
        insensitive = grep_search("needle", include="*.py", case_insensitive=True)

    assert str(sensitive) == "No files found"
    assert str(tmp_path / "one.py") in insensitive
    assert str(tmp_path / "two.txt") not in insensitive


def test_grep_compatibility_files_count_and_context_share_json_rows(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("before\nneedle\nafter\nneedle two\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        files = grep_search("needle", output_mode="files_with_matches")
        counts = grep_search("needle", output_mode="count")
        context = grep_search("needle", context=1)

    assert f"Found 1 file(s) matching 'needle'\n{source}" == str(files)
    assert f"{source}:2" in counts
    assert "Line 1: before" in context
    assert "Line 3: after" in context
    assert "Line 4: needle two" in context


def test_decode_rg_match_validates_and_retains_submatch_offsets():
    row = decode_ripgrep_event(
        json.dumps(_match_event("./source.py", "x needle y\n", 7, 12))
    )

    assert row is not None
    assert row.path == "source.py"
    assert row.line == 7
    assert row.absolute_offset == 12
    assert row.submatches == ({"text": "needle", "start": 2, "end": 8},)
