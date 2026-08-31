"""Correctness contracts for identity queries and production structural retrieval."""
from __future__ import annotations

import json

import pytest


def _duplicate_login_fixture(root) -> None:
    (root / "auth").mkdir()
    (root / "billing").mkdir()
    (root / "app").mkdir()
    (root / "auth" / "api.py").write_text(
        "def login(): return 'auth'\n", encoding="utf-8",
    )
    (root / "billing" / "api.py").write_text(
        "def login(): return 'billing'\n", encoding="utf-8",
    )
    (root / "app" / "main.py").write_text(
        "from auth.api import login as auth_login\n"
        "from billing.api import login as billing_login\n"
        "def run(): return auth_login(), billing_login()\n",
        encoding="utf-8",
    )


def test_ambiguous_symbol_withholds_definition_and_identity_relations(tmp_path) -> None:
    from nz_coder.intelligence.code_index import (
        AmbiguousSymbolError,
        PersistentCodeIndex,
    )

    _duplicate_login_fixture(tmp_path)
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    context = index.symbol_context("login")

    assert context["ambiguous"] is True
    assert context["definition"] is None
    assert context["symbol_id"] is None
    assert context["callers"] == context["callees"] == context["references"] == []
    assert {item["path"] for item in context["alternatives"]} == {
        "auth/api.py", "billing/api.py",
    }
    with pytest.raises(AmbiguousSymbolError):
        index.callers("login")
    with pytest.raises(AmbiguousSymbolError):
        index.callees("login")
    with pytest.raises(AmbiguousSymbolError):
        index.references("login")


def test_symbol_id_queries_isolate_duplicate_callers_and_references(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    _duplicate_login_fixture(tmp_path)
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    alternatives = index.symbol_context("login")["alternatives"]
    identities = {item["path"]: item["symbol_id"] for item in alternatives}

    auth_callers = index.callers(identities["auth/api.py"])
    billing_callers = index.callers(identities["billing/api.py"])
    auth_refs = index.references(identities["auth/api.py"])
    billing_refs = index.references(identities["billing/api.py"])

    assert [edge.raw_name if hasattr(edge, "raw_name") else edge.callee for edge in auth_callers] == ["login"]
    assert [edge.caller for edge in auth_callers] == ["run"]
    assert [edge.caller for edge in billing_callers] == ["run"]
    assert {item.raw_name for item in auth_refs} == {"auth_login"}
    assert {item.raw_name for item in billing_refs} == {"billing_login"}
    assert all(item.target_symbol_id == identities["auth/api.py"] for item in auth_refs)
    assert all(item.target_symbol_id == identities["billing/api.py"] for item in billing_refs)


def test_duplicate_class_methods_self_and_qualified_calls_remain_isolated(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "workers.py").write_text(
        "class A:\n"
        "    def run(self): return 1\n"
        "    def again(self): return self.run()\n"
        "class B:\n"
        "    def run(self): return 2\n"
        "def dispatch(): return A.run(None), B.run(None)\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    alternatives = index.symbol_context("run")["alternatives"]
    ids = {item["qualified_name"].rsplit(".", 2)[-2]: item["symbol_id"] for item in alternatives}

    assert {edge.caller for edge in index.callers(ids["A"])} == {"again", "dispatch"}
    assert {edge.caller for edge in index.callers(ids["B"])} == {"dispatch"}


def test_reexport_and_unresolved_dynamic_reference_contract(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "api.py").write_text("def login(): return 1\n", encoding="utf-8")
    (tmp_path / "auth" / "__init__.py").write_text(
        "from .api import login\n", encoding="utf-8",
    )
    (tmp_path / "main.py").write_text(
        "from auth import login\n"
        "def direct(): return login()\n"
        "def dynamic(handler): return handler.login()\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    login_id = index.symbol_context("login")["symbol_id"]
    edges = index.file_calls("main.py")
    references = index.references(login_id)

    assert edges[0].resolution_kind == "re-exported-binding"
    assert edges[0].callee_symbol_id == login_id
    assert edges[1].callee_symbol_id is None
    assert edges[1].unresolved_target.raw_name == "login"
    assert edges[1].unresolved_target.candidates == (login_id,)
    assert any(item.target_symbol_id == login_id for item in references)
    unresolved = [item for item in index.references("missing") if item.unresolved_target]
    assert unresolved == []


def test_process_context_requires_disambiguated_entry(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    _duplicate_login_fixture(tmp_path)
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    result = index.process_context("login")

    assert result["ambiguous"] is True
    assert result["process_id"] is None
    assert len(result["alternatives"]) == 2
    assert result["steps"] == []


@pytest.mark.parametrize("language", ["typescript", "javascript", "go"])
def test_production_registry_selects_tree_sitter_when_declared_wheels_exist(language) -> None:
    pytest.importorskip("tree_sitter")
    from nz_coder.intelligence.analyzers import AnalyzerRegistry

    probe = AnalyzerRegistry().capability_probe()[language]

    assert probe["available"] is True
    assert probe["capability_tier"] == "tree-sitter"


def test_registry_honestly_reports_lexical_fallback_without_tree_sitter(monkeypatch) -> None:
    from nz_coder.intelligence.analyzers import (
        AnalyzerRegistry,
        LexicalFallbackAnalyzer,
        TreeSitterAnalyzer,
    )

    tree_sitter = TreeSitterAnalyzer()
    monkeypatch.setattr(tree_sitter, "available_for", lambda _language: False)
    registry = AnalyzerRegistry((tree_sitter, LexicalFallbackAnalyzer()))

    probe = registry.capability_probe()

    assert probe["typescript"]["available"] is False
    assert probe["javascript"]["capability_tier"] == "lexical-fallback"
    assert probe["go"]["analyzer"] == "LexicalFallbackAnalyzer"


def test_typescript_namespace_and_alias_calls_resolve_by_import_identity(tmp_path) -> None:
    pytest.importorskip("tree_sitter_typescript")
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "auth.ts").write_text(
        "export function login() { return 1 }\n", encoding="utf-8",
    )
    (tmp_path / "main.ts").write_text(
        "import { login as authLogin } from './auth'\n"
        "import * as auth from './auth'\n"
        "export function run() { return [authLogin(), auth.login()] }\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    entries, _stats = index.scan(tmp_path, max_files=20)

    edges = index.file_calls("main.ts")

    assert {item.capability_tier for item in entries} == {"tree-sitter"}
    assert [item.resolution_kind for item in edges] == [
        "imported-binding", "qualified-import-member",
    ]
    assert edges[0].callee_symbol_id == edges[1].callee_symbol_id


def test_javascript_require_and_dynamic_member_are_not_name_bound(tmp_path) -> None:
    pytest.importorskip("tree_sitter_javascript")
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "auth.js").write_text(
        "export function login() { return 1 }\n", encoding="utf-8",
    )
    (tmp_path / "main.js").write_text(
        "const auth = require('./auth')\n"
        "export function direct() { return auth.login() }\n"
        "export function dynamic(object) { return object.login() }\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    entries, _stats = index.scan(tmp_path, max_files=20)
    edges = index.file_calls("main.js")

    assert {item.capability_tier for item in entries} == {"tree-sitter"}
    assert edges[0].resolution_kind == "qualified-import-member"
    assert edges[1].callee_symbol_id is None
    assert edges[1].resolution_kind == "heuristic-candidates"


def test_go_package_call_resolves_and_unknown_receiver_stays_unresolved(tmp_path) -> None:
    pytest.importorskip("tree_sitter_go")
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "api.go").write_text(
        "package foo\nfunc Bar() int { return 1 }\n", encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        'package main\nimport "example/foo"\n'
        "func localFunction() int { return 2 }\n"
        "func run(receiver interface{ Method() int }) int { "
        "return foo.Bar() + localFunction() + receiver.Method() }\n",
        encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    entries, _stats = index.scan(tmp_path, max_files=20)
    edges = index.file_calls("main.go")

    assert {item.capability_tier for item in entries} == {"tree-sitter"}
    by_name = {item.callee: item for item in edges}
    assert by_name["Bar"].resolution_kind == "qualified-import-member"
    assert by_name["localFunction"].resolution_kind == "exact-same-module"
    assert by_name["Method"].callee_symbol_id is None


def test_module_boundaries_split_src_areas_and_nested_manifests(tmp_path) -> None:
    from nz_coder.intelligence.code_index import PersistentCodeIndex

    for area in ("auth", "payment"):
        target = tmp_path / "src" / area
        target.mkdir(parents=True)
        (target / "api.py").write_text(f"def {area}(): return 1\n", encoding="utf-8")
    package = tmp_path / "components" / "identity"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname='identity'\n", encoding="utf-8")
    (package / "src" / "identity").mkdir(parents=True)
    (package / "src" / "identity" / "api.py").write_text(
        "def verify(): return True\n", encoding="utf-8",
    )
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)

    assert index.symbol_context("auth")["module"] == "module:src/auth"
    assert index.symbol_context("payment")["module"] == "module:src/payment"
    assert index.symbol_context("verify")["module"] == "module:components/identity"


def test_unified_structural_lookup_returns_symbol_module_and_process(tmp_path) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService

    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "__init__.py").write_text(
        "def refresh_token_if_expired(token): return token\n", encoding="utf-8",
    )
    service = RepoIntelligenceService(tmp_path)
    try:
        service.prewarm(max_files=20).result(timeout=10)
        service.process_context("refresh_token_if_expired")
        result = service.intent_lookup("expired token refresh", limit=20)
    finally:
        service.close()

    assert result["embedding"] is False
    assert {item["kind"] for item in result["items"]} == {
        "symbol", "module", "process",
    }
    assert all({
        "kind", "title", "locator", "snippet", "score", "identity",
        "confidence", "source",
    } <= set(item) for item in result["items"])
    assert result["items"] == sorted(
        result["items"], key=lambda item: (-item["score"], item["kind"], item["title"]),
    )


def test_repo_context_lookup_operation_uses_single_tool_entry(tmp_path, monkeypatch) -> None:
    from nz_coder.foundation import config
    from nz_coder.tools.repo_context import repo_context

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    (tmp_path / "auth.py").write_text(
        "def refresh_expired_token(token): return token\n", encoding="utf-8",
    )

    result = json.loads(repo_context(
        "lookup", module="expired token refresh", refresh=True,
    ))

    assert result["source"] == "unified-structural-intent-lookup"
    assert result["items"][0]["kind"] == "symbol"
