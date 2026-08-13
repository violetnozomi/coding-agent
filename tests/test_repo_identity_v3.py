"""Identity, resolution, and incremental repository intelligence contracts."""
from __future__ import annotations

import subprocess

import pytest


def test_symbol_id_is_stable_across_rebuild_and_line_shift(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    source = tmp_path / "api.py"
    source.write_text("def login(user):\n    return user\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    first = index.symbol_context("login")["symbol_id"]

    source.write_text("# header moved\n\ndef login(user):\n    return user\n", encoding="utf-8")
    index.update_paths(["api.py"])
    second = index.symbol_context("login")["symbol_id"]

    assert first == second


def test_duplicate_names_resolve_import_aliases_and_qualified_calls(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "auth").mkdir()
    (tmp_path / "payment").mkdir()
    (tmp_path / "auth" / "api.py").write_text("def login(): return 'auth'\n", encoding="utf-8")
    (tmp_path / "payment" / "api.py").write_text("def login(): return 'payment'\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from auth.api import login as auth_login\n"
        "import payment.api as pay\n"
        "def run(): return auth_login(), pay.login()\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    edges = index.file_calls("main.py")

    assert [edge.resolution_kind for edge in edges] == [
        "imported-binding", "qualified-import-member",
    ]
    assert edges[0].callee_symbol_id != edges[1].callee_symbol_id
    assert "auth/api.py" in edges[0].callee_symbol_id
    assert "payment/api.py" in edges[1].callee_symbol_id
    assert index.symbol_context("login")["warnings"]


def test_self_method_and_class_qualified_calls_resolve_by_identity(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "service.py").write_text(
        "class Auth:\n"
        "    def login(self): return 1\n"
        "    def run(self): return self.login()\n"
        "def external(): return Auth.login(None)\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    edges = index.file_calls("service.py")

    assert edges[0].resolution_kind == "self-method"
    assert edges[1].resolution_kind == "qualified-same-module"
    assert edges[0].callee_symbol_id == edges[1].callee_symbol_id


def test_unresolved_call_retains_raw_target_and_candidates(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "a.py").write_text("def dispatch(): return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def dispatch(): return 2\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("def run(handler): return handler.dispatch()\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    edge = index.file_calls("main.py")[0]

    assert edge.callee_symbol_id is None
    assert edge.resolution_kind == "heuristic-candidates"
    assert edge.unresolved_target.raw_name == "dispatch"
    assert len(edge.unresolved_target.candidates) == 2


def test_delete_and_rename_leave_no_ghost_identity_or_edges(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    target = tmp_path / "old.py"
    target.write_text("def target(): return 1\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text("from old import target\ndef run(): return target()\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    old_id = index.symbol_context("target")["symbol_id"]

    target.rename(tmp_path / "new.py")
    (tmp_path / "caller.py").write_text("from new import target\ndef run(): return target()\n", encoding="utf-8")
    index.update_paths(["old.py", "new.py", "caller.py"])

    context = index.symbol_context("target")
    assert context["symbol_id"] != old_id
    assert context["definition"]["path"] == "new.py"
    assert index.callers(context["symbol_id"])[0].path == "caller.py"
    assert all(old_id not in edge.caller_symbol_id for edge in index.snapshot().calls)


def test_graph_incremental_update_consumes_snapshot_without_scan(tmp_path, monkeypatch) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex
    from nz_coder.intelligence.repository_graph import RepositoryGraph

    (tmp_path / "a.py").write_text("import b\ndef run(): return b.value\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("value = 1\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    graph = RepositoryGraph(tmp_path, index=index)
    graph.build(snapshot=index.snapshot())
    (tmp_path / "b.py").unlink()
    (tmp_path / "c.py").write_text("value = 2\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("import c\ndef run(): return c.value\n", encoding="utf-8")
    index.update_paths(["a.py", "c.py", "b.py"])
    monkeypatch.setattr(index, "scan", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scan")))

    stats = graph.update_paths(["a.py", "c.py"], ["b.py"], snapshot=index.snapshot())

    assert stats.indexed == 2
    assert stats.relationships_updated == 2
    assert graph.module_context("a.py")["dependencies"] == ["c.py"]
    assert "b.py" not in graph.modules()

    reopened = RepositoryGraph(tmp_path, index=index)
    assert reopened.module_context("a.py")["dependencies"] == ["c.py"]
    assert "b.py" not in reopened.modules()


def test_typescript_fixture_reports_explicit_parser_capability(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "a.ts").write_text("export function login() { return 1 }\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("export function login() { return 2 }\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    entries, _stats = index.scan(tmp_path, max_files=20)

    assert {entry.capability_tier for entry in entries} <= {"tree-sitter", "lexical-fallback"}
    assert len(index.symbol_context("login")["definitions"]) == 2


def test_typescript_tree_sitter_resolves_import_alias_among_duplicate_names(tmp_path) -> None:
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_typescript")
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "auth.ts").write_text(
        "export function login() { return 'auth' }\n", encoding="utf-8",
    )
    (tmp_path / "billing.ts").write_text(
        "export function login() { return 'billing' }\n", encoding="utf-8",
    )
    (tmp_path / "main.ts").write_text(
        "import { login as authLogin } from './auth'\n"
        "export function run() { return authLogin() }\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    entries, _stats = index.scan(tmp_path, max_files=20)
    edge = index.file_calls("main.ts")[0]

    assert {entry.capability_tier for entry in entries} == {"tree-sitter"}
    assert edge.resolution_kind == "imported-binding"
    assert "auth.ts" in edge.callee_symbol_id
    assert "billing.ts" not in edge.callee_symbol_id


def test_package_area_module_identity_aggregates_multiple_files(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex
    from nz_coder.intelligence.repository_graph import RepositoryGraph

    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "tokens.py").write_text(
        "def refresh(): return 1\n", encoding="utf-8",
    )
    (tmp_path / "auth" / "client.py").write_text(
        "from auth.tokens import refresh\ndef rotate(): return refresh()\n", encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    graph = RepositoryGraph(tmp_path, index=index)
    graph.build(snapshot=index.snapshot())

    capsule = graph.module_context("module:auth")

    assert capsule["module_id"] == "module:auth"
    assert capsule["files"] == ["auth/client.py", "auth/tokens.py"]
    assert capsule["file_count"] == 2
    assert {item["name"] for item in capsule["top_symbols"]} == {"refresh", "rotate"}


def test_incremental_resolution_only_revisits_affected_call_names(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    for index in range(80):
        (tmp_path / f"m{index}.py").write_text(
            f"def target_{index}(): return {index}\n"
            f"def caller_{index}(): return target_{index}()\n",
            encoding="utf-8",
        )
    code_index = PersistentCodeIndex(tmp_path)
    code_index.scan(tmp_path, max_files=100)
    source = tmp_path / "m40.py"
    source.write_text(
        "def target_40(): return 400\ndef caller_40(): return target_40()\n",
        encoding="utf-8",
    )

    stats = code_index.update_paths(["m40.py"])

    assert stats.calls_resolved == 1
    assert stats.calls_resolved < code_index.metrics()["call_edges"]


def test_bounded_lsp_augmentation_upgrades_unresolved_call(tmp_path) -> None:
    from nz_coder.intelligence.code_index import (
        PersistentCodeIndex, ResolvedCallLocation,
    )

    (tmp_path / "target.py").write_text("def dispatch(): return 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def dispatch(): return 2\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "def run(handler): return handler.dispatch()\n", encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    class Resolver:
        def resolve(self, request):
            assert request.file_path == "main.py"
            return ResolvedCallLocation("target.py", 1, name="dispatch")

    stats = index.augment_call_targets(Resolver(), max_calls=1, time_budget_ms=100)
    edge = index.file_calls("main.py")[0]

    assert stats.attempted == stats.resolved == 1
    assert edge.resolution_kind == "lsp-definition"
    assert "target.py" in edge.callee_symbol_id


def test_go_tree_sitter_resolves_qualified_package_call(tmp_path) -> None:
    pytest.importorskip("tree_sitter_go")
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.go").write_text(
        "package pkg\nfunc Login() int { return 1 }\n", encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        'package main\nimport "example/pkg"\nfunc run() int { return pkg.Login() }\n',
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    entries, _stats = index.scan(tmp_path, max_files=20)
    edge = index.file_calls("main.go")[0]

    assert {entry.capability_tier for entry in entries} == {"tree-sitter"}
    assert edge.resolution_kind == "qualified-import-member"
    assert "pkg/api.go" in edge.callee_symbol_id


def test_symbol_and_process_context_include_tests_and_changed_status(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "tests").mkdir()
    source = tmp_path / "service.py"
    source.write_text(
        "def persist(value): return value\n"
        "def handle(value): return persist(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_service.py").write_text(
        "from service import handle\n"
        "def test_handle(): assert handle(1) == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Benchmark", "-c",
            "user.email=benchmark@example.invalid", "commit", "-qm", "baseline",
        ],
        cwd=tmp_path, check=True,
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    before = index.symbol_context("handle")
    process = index.process_context("handle", max_depth=3)
    source.write_text(
        "def persist(value): return value\n"
        "def handle(value): return persist(value) + 1\n",
        encoding="utf-8",
    )
    index.update_paths(["service.py"])
    after = index.symbol_context("handle")

    assert before["related_tests"] == ["tests/test_service.py"]
    assert before["changed"] is False
    assert process["related_tests"] == ["tests/test_service.py"]
    assert after["changed"] is True
