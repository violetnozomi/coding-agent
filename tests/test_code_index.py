"""Tests for the workspace-persistent incremental code index."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace


def test_index_persists_symbols_and_references_across_instances(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    source = tmp_path / "service.py"
    source.write_text(
        "def helper():\n    return 1\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )

    first = PersistentCodeIndex(tmp_path)
    entries, first_stats = first.scan(tmp_path, max_files=20)
    second = PersistentCodeIndex(tmp_path)
    persisted, second_stats = second.scan(tmp_path, max_files=20)

    assert first.database_path.is_file()
    assert first_stats.indexed == 1
    assert [symbol.name for symbol in entries[0].symbols] == ["helper", "run"]
    assert second_stats.reused == 1
    assert persisted == entries
    assert [(item.path, item.name, item.line) for item in second.references("helper", tmp_path)] == [
        ("service.py", "helper", 5)
    ]


def test_incremental_update_replaces_old_rows_and_removes_deleted_file(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    source = tmp_path / "app.py"
    source.write_text("def old_name():\n    return 1\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    source.write_text("def new_name():\n    return 2\n", encoding="utf-8")
    updated = index.update_paths(["app.py"])
    entries, stats = index.scan(tmp_path, max_files=20)

    assert updated.indexed == 1
    assert stats.reused == 1
    assert [item.name for item in entries[0].symbols] == ["new_name"]

    source.unlink()
    removed = index.update_paths(["app.py"])
    with sqlite3.connect(index.database_path) as connection:
        count = connection.execute("SELECT count(*) FROM files").fetchone()[0]
    assert removed.removed == 1
    assert count == 0


def test_truncated_scan_does_not_delete_unvisited_cached_files(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)

    _, stats = index.scan(tmp_path, max_files=1, refresh=True)

    with sqlite3.connect(index.database_path) as connection:
        paths = [row[0] for row in connection.execute("SELECT path FROM files ORDER BY path")]
    assert stats.omitted == 1
    assert paths == ["a.py", "b.py"]


def test_index_rejects_workspace_state_symlink_escape(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)

    try:
        PersistentCodeIndex(workspace)
    except ValueError as exc:
        assert "escapes workspace" in str(exc)
    else:
        raise AssertionError("state-directory symlink escape was not rejected")
    assert not (outside / "index").exists()


def test_code_references_tool_uses_persistent_index(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.tools.repo_map import code_references

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    (tmp_path / "main.py").write_text(
        "from pkg import target\n\ndef run():\n    return target()\n",
        encoding="utf-8",
    )

    result = code_references("target")

    assert "References for 'target'" in result
    assert "main.py:4:" in result


def test_write_refresh_helper_updates_only_workspace_relative_paths(tmp_path):
    from nz_coder.intelligence.code_index import (
        PersistentCodeIndex,
        update_code_index_after_write,
    )

    (tmp_path / "fresh.py").write_text("def fresh():\n    pass\n", encoding="utf-8")
    stats = update_code_index_after_write(["fresh.py"], tmp_path)
    entries, scan_stats = PersistentCodeIndex(tmp_path).scan(tmp_path, max_files=10)

    assert stats.indexed == 1
    assert scan_stats.reused == 1
    assert entries[0].symbols[0].name == "fresh"


def test_index_persists_cross_file_call_edges_and_symbol_context(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "helpers.py").write_text(
        "def normalize(value):\n    return value.strip()\n", encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "from helpers import normalize\n\ndef handle(value):\n    return normalize(value)\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    callers = index.callers("normalize")
    context = index.symbol_context("normalize")

    assert [(edge.caller, edge.callee, edge.path, edge.line) for edge in callers] == [
        ("handle", "normalize", "service.py", 4),
    ]
    assert context["definition"]["path"] == "helpers.py"
    assert context["freshness"] == "indexed"
    assert context["callers"][0]["caller"] == "handle"


def test_incremental_update_replaces_stale_call_edges(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    source = tmp_path / "app.py"
    source.write_text("def old(): pass\ndef run(): old()\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    assert index.callers("old")

    source.write_text("def new(): pass\ndef run(): new()\n", encoding="utf-8")
    index.update_paths(["app.py"])

    assert index.callers("old") == []
    assert index.callers("new")[0].caller == "run"


def test_process_context_traverses_bounded_call_chain(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "flow.py").write_text(
        "def leaf(): return 1\ndef middle(): return leaf()\ndef entry(): return middle()\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    process = index.process_context("entry", max_depth=3, limit=10)

    assert [(item["caller"], item["callee"]) for item in process["edges"]] == [
        ("entry", "middle"), ("middle", "leaf"),
    ]
    assert process["truncated"] is False


def test_multilanguage_index_persists_references_calls_and_export_confidence(tmp_path):
    import pytest

    from nz_coder.intelligence.code_index import (
        AmbiguousSymbolError,
        PersistentCodeIndex,
    )

    (tmp_path / "service.ts").write_text(
        "export function normalize(v: string) { return v.trim() }\n"
        "export function handle(v: string) { return normalize(v) }\n",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        "package main\nfunc Normalize(v string) string { return v }\n"
        "func main() { Normalize(\"x\") }\n",
        encoding="utf-8",
    )
    (tmp_path / "lib.rs").write_text(
        "pub fn normalize() {}\npub fn run() { normalize(); }\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    entries, _stats = index.scan(tmp_path, max_files=20)

    assert {entry.language for entry in entries} == {"typescript", "go", "rust"}
    with pytest.raises(AmbiguousSymbolError):
        index.callers("normalize")
    normalize_ids = [
        item["symbol_id"] for item in index.symbol_context("normalize")["alternatives"]
    ]
    isolated_callers = {
        edge.path for symbol_id in normalize_ids for edge in index.callers(symbol_id)
    }
    assert isolated_callers == {"lib.rs", "service.ts"}
    assert index.callers("Normalize")[0].caller == "main"
    with pytest.raises(AmbiguousSymbolError):
        index.references("normalize", tmp_path)
    assert all(index.references(symbol_id, tmp_path) for symbol_id in normalize_ids)
    capsule = index.symbol_context("normalize")
    assert capsule["ambiguous"] is True
    assert capsule["definition"] is None
    assert capsule["references"] == []
    assert capsule["callers"] == []
    assert capsule["confidence"] == "mixed-structural"


def test_symbol_capsule_preserves_ambiguous_definitions_and_entrypoints(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "a.py").write_text("def run(): return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def run(): return 2\n", encoding="utf-8")
    (tmp_path / "cli.py").write_text("def main(): return run()\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    capsule = index.symbol_context("run")
    entrypoints = index.entrypoints()

    assert [item["path"] for item in capsule["definitions"]] == ["a.py", "b.py"]
    assert entrypoints[0]["name"] == "main"
    assert entrypoints[0]["path"] == "cli.py"


def test_python_nested_function_call_is_not_duplicated_into_outer_caller(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "nested.py").write_text(
        "def target(): return 1\n"
        "def outer():\n"
        "    def inner():\n"
        "        return target()\n"
        "    return inner()\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    assert [(edge.caller, edge.line) for edge in index.callers("target")] == [("inner", 4)]


def test_structural_search_localizes_symbol_without_known_file_or_exact_name(tmp_path):
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "unrelated.py").write_text("def render_page(): return ''\n", encoding="utf-8")
    (tmp_path / "pipeline.py").write_text(
        "def normalize_input(value): return value.strip()\n", encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    matches = index.search_symbols("normalize input", limit=5)

    assert matches[0]["path"] == "pipeline.py"
    assert matches[0]["name"] == "normalize_input"


def test_loop_refreshes_index_from_successful_write_results(tmp_path, monkeypatch):
    import nz_coder.runtime.loop as loop_module
    from nz_coder import config
    from nz_coder.loop import AgentLoop

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    calls = []
    monkeypatch.setattr(
        loop_module,
        "update_code_index_after_write",
        lambda paths, workspace: calls.append((paths, workspace))
        or SimpleNamespace(indexed=1, removed=0),
    )
    agent = object.__new__(AgentLoop)
    agent.tracer = SimpleNamespace(log=lambda *args, **kwargs: None)
    agent.change_tracker = SimpleNamespace(
        current_changed_paths=lambda: [],
        current_deleted_paths=lambda: [],
    )
    successful = SimpleNamespace(
        is_write=True,
        executed=True,
        dispatch_failed=False,
        name="write_file",
        tool_input={"path": "app.py"},
    )
    failed = SimpleNamespace(
        is_write=True,
        executed=True,
        dispatch_failed=True,
        name="write_file",
        tool_input={"path": "ignored.py"},
    )

    agent._refresh_code_index([
        (0, {"id": "ok"}, successful),
        (1, {"id": "failed"}, failed),
    ])

    assert calls == [(["app.py"], tmp_path.resolve())]
