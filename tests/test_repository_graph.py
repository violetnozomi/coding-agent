"""Persistent multi-language repository module graph contracts."""
from __future__ import annotations

from nz_coder.intelligence.repository_graph import RepositoryGraph


def test_graph_cold_warm_incremental_delete_and_rename(tmp_path) -> None:
    (tmp_path / "a.py").write_text("import b\ndef run(): return b.value\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("value = 1\n", encoding="utf-8")
    graph = RepositoryGraph(tmp_path)

    cold = graph.build(max_files=100)
    warm = graph.build(max_files=100)
    (tmp_path / "b.py").rename(tmp_path / "core.py")
    (tmp_path / "a.py").write_text("import core\ndef run(): return core.value\n", encoding="utf-8")
    changed = graph.build(max_files=100)

    assert cold.indexed == 2 and cold.reused == 0
    assert warm.reused == 2 and warm.indexed == 0
    assert changed.indexed == 2 and changed.removed == 1
    assert graph.module_context("a.py")["dependencies"] == ["core.py"]
    assert "b.py" not in graph.modules()


def test_graph_resolves_python_typescript_go_rust_and_cycles(tmp_path) -> None:
    (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")
    (tmp_path / "ui.ts").write_text("import { x } from './util'\n", encoding="utf-8")
    (tmp_path / "util.ts").write_text("export const x = 1\n", encoding="utf-8")
    (tmp_path / "main.go").write_text('package main\nimport "example/local/pkg"\n', encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "lib.go").write_text("package pkg\n", encoding="utf-8")
    (tmp_path / "lib.rs").write_text("mod helper;\nuse crate::helper;\n", encoding="utf-8")
    (tmp_path / "helper.rs").write_text("pub fn help() {}\n", encoding="utf-8")
    graph = RepositoryGraph(tmp_path)
    graph.build(max_files=100)

    assert graph.module_context("ui.ts")["dependencies"] == ["util.ts"]
    assert graph.module_context("main.go")["dependencies"] == ["pkg/lib.go"]
    assert graph.module_context("lib.rs")["dependencies"] == ["helper.rs"]
    assert graph.cycles() == (("a.py", "b.py"),)


def test_graph_overview_and_relationships_are_bounded(tmp_path) -> None:
    for index in range(12):
        dependency = f"m{index + 1}" if index < 11 else "m0"
        (tmp_path / f"m{index}.py").write_text(f"import {dependency}\n", encoding="utf-8")
    graph = RepositoryGraph(tmp_path)
    graph.build(max_files=100)

    overview = graph.overview(limit=5)
    relationships = graph.relationship_scan("m0.py", limit=3)

    assert overview["module_count"] == 12
    assert len(overview["modules"]) == 5
    assert relationships["module"] == "m0.py"
    assert len(relationships["related"]) <= 3


def test_module_and_changed_scope_include_symbol_call_evidence(tmp_path) -> None:
    (tmp_path / "helpers.py").write_text("def leaf(): return 1\n", encoding="utf-8")
    (tmp_path / "service.py").write_text(
        "from helpers import leaf\ndef entry(): return leaf()\n", encoding="utf-8",
    )
    graph = RepositoryGraph(tmp_path)
    graph.build(max_files=100)

    module = graph.module_context("service.py")
    scope = graph.changed_scope(changed_paths=["helpers.py"], limit=20)

    assert module["symbols"][0]["name"] == "entry"
    assert module["outgoing_calls"][0]["callee"] == "leaf"
    assert scope["changed_symbols"] == ["leaf"]
    assert scope["impacted_callers"] == ["service.py:entry"]


def test_module_capsule_includes_exports_entrypoints_and_related_tests(tmp_path) -> None:
    (tmp_path / "service.py").write_text(
        "def public_api(): return 1\ndef _private(): return public_api()\n",
        encoding="utf-8",
    )
    (tmp_path / "cli.py").write_text(
        "from service import public_api\ndef main(): return public_api()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(
        "from service import public_api\ndef test_api(): assert public_api() == 1\n",
        encoding="utf-8",
    )
    graph = RepositoryGraph(tmp_path)
    graph.build(max_files=100)

    capsule = graph.module_context("service.py")

    assert [item["name"] for item in capsule["exports"]] == ["public_api"]
    assert capsule["entrypoints"] == []
    assert capsule["related_tests"] == ["tests/test_service.py"]
    assert capsule["capsule_version"] == 2


def test_process_capsule_contains_definition_nodes_and_reverse_callers(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "flow.py").write_text(
        "def leaf(): return 1\ndef middle(): return leaf()\ndef entry(): return middle()\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=100)

    process = index.process_context("middle", max_depth=2, limit=20)

    assert {item["name"] for item in process["nodes"]} == {"entry", "leaf", "middle"}
    assert process["incoming_edges"][0]["caller"] == "entry"
