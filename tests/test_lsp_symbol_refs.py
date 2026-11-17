"""Exact, version-bound names are the shared index and LSP query contract."""

import json
import time
from types import SimpleNamespace

import pytest

from nz_coder.intelligence.analyzers import (
    AnalyzerRegistry,
    LexicalFallbackAnalyzer,
    PythonAstAnalyzer,
    TreeSitterAnalyzer,
)
from nz_coder.intelligence.code_index import PersistentCodeIndex


def selection(line, start, end, encoding="utf-32"):
    return {
        "start": {"line": line, "character": start},
        "end": {"line": line, "character": end},
        "encoding": encoding,
    }


def test_python_calls_select_final_name_after_emoji_and_across_lines(tmp_path):
    source = (
        '@decorator("😀")\n'
        "async def run(obj):\n"
        '    "😀"; obj.method(); obj.method()\n'
        "    return (\n"
        "        obj\n"
        "        .method\n"
        "    )()\n"
    )
    result = PythonAstAnalyzer().analyze_file(
        path=tmp_path / "app.py", relative="app.py", source=source, language="python"
    )

    assert [call.selection for call in result.calls if call.raw_name == "method"] == [
        selection(2, 13, 19),
        selection(2, 27, 33),
        selection(5, 9, 15),
    ]
    assert result.symbols[0].selection == selection(1, 10, 13)


@pytest.mark.parametrize(
    "language,filename,source,want",
    [
        (
            "typescript",
            "app.ts",
            'export function run() { "😀"; obj.method(); obj.method() }',
            [selection(0, 36, 42, "utf-8"), selection(0, 50, 56, "utf-8")],
        ),
        (
            "javascript",
            "app.js",
            "function run() { return obj\n .method() }",
            [selection(1, 2, 8, "utf-8")],
        ),
        (
            "go",
            "app.go",
            "package main\nfunc run() { obj.Method() }",
            [selection(1, 17, 23, "utf-8")],
        ),
    ],
)
def test_tree_sitter_uses_callee_name_nodes(tmp_path, language, filename, source, want):
    analyzer = TreeSitterAnalyzer()
    if not analyzer.available_for(language):
        pytest.skip(f"existing {language} parser unavailable")
    result = analyzer.analyze_file(
        path=tmp_path / filename, relative=filename, source=source, language=language
    )
    assert [call.selection for call in result.calls] == want
    assert result.symbols[0].selection is not None


def test_symbol_ref_persists_snapshot_hash_and_declared_name_on_id_reuse(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    path = tmp_path / "app.py"
    path.write_bytes(b"@decorator\r\ndef target(): pass\r\n")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    match = index.search_symbols("target")[0]
    get_ref = index.symbol_ref
    ref = get_ref(match["symbol_id"])
    assert ref is not None
    assert ref == {
        "symbol_id": match["symbol_id"],
        "file_path": "app.py",
        "content_hash": WorkspaceFileAccess(tmp_path)
        .read_text_with_identity("app.py")[1]
        .content_hash,
        "selection": selection(1, 4, 10),
        "index_generation": index.generation(),
    }
    assert match["symbol_ref"] == ref
    assert index.symbol_context(match["symbol_id"])["definition"]["symbol_ref"] == ref
    assert get_ref("target") is None  # ID 模式不能按同名定义猜测目标。
    path.write_text("\n\ndef target(): return 2\n", encoding="utf-8")
    index.update_paths(["app.py"])
    changed = get_ref(ref["symbol_id"])
    assert changed["symbol_id"] == ref["symbol_id"]
    assert changed["content_hash"] != ref["content_hash"]
    assert changed["selection"] == selection(2, 4, 10)
    assert changed["index_generation"] > ref["index_generation"]


def test_unproved_lexical_declarations_have_no_symbol_ref(tmp_path):
    (tmp_path / "app.rs").write_text("pub fn target() {}\n", encoding="utf-8")
    index = PersistentCodeIndex(
        tmp_path, analyzers=AnalyzerRegistry((LexicalFallbackAnalyzer(),))
    )
    index.scan(tmp_path, max_files=10)
    match = index.search_symbols("target")[0]
    assert match.get("symbol_ref") is None
    assert index.symbol_ref(match["symbol_id"]) is None


@pytest.mark.parametrize(
    "encoding,columns",
    [
        ("utf-8", [16, 30]),
        ("utf-16", [14, 28]),
        ("utf-32", [13, 27]),
    ],
)
def test_lsp_augmentation_preserves_each_exact_name_and_converts_encoding(
    tmp_path,
    monkeypatch,
    encoding,
    columns,
):
    import nz_coder.intelligence.lsp_resolver as resolver_module

    (tmp_path / "app.py").write_text(
        'def run():\n    "😀"; obj.method(); obj.method()\n', encoding="utf-8"
    )
    (tmp_path / "target.py").write_text("def method(): pass\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    observed = []

    class Client:
        position_encoding = encoding

        def open_document(self, path, *, reader, operation):
            reader()
            assert operation.deadline > time.monotonic()
            return 1

        def document_matches(self, *args):
            return True

        def request(self, method, params, *, operation):
            observed.append(params["position"])
            return {
                "uri": (tmp_path / "target.py").as_uri(),
                "range": {"start": {"line": 0, "character": 4}},
            }

    monkeypatch.setattr(
        resolver_module, "get_client_for_file", lambda *_args, operation: Client()
    )
    stats = index.augment_call_targets(resolver_module.LspCallTargetResolver(tmp_path))
    assert observed == [{"line": 1, "character": column} for column in columns]
    assert stats.resolved == 2


def test_lsp_augmentation_rejects_changed_source_and_unproved_lexical_call(
    tmp_path, monkeypatch
):
    import nz_coder.intelligence.lsp_resolver as resolver_module

    source = tmp_path / "app.py"
    source.write_text("def run(): unknown.method()\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    source.write_text("def run(): other.method()\n", encoding="utf-8")
    monkeypatch.setattr(
        resolver_module,
        "get_client_for_file",
        lambda *_args, **_kwargs: pytest.fail("stale source must not start LSP"),
    )
    stats = index.augment_call_targets(resolver_module.LspCallTargetResolver(tmp_path))
    assert stats.failed == 0
    assert stats.resolved == 0
    (tmp_path / "lexical").mkdir()
    lexical = PersistentCodeIndex(
        tmp_path / "lexical", analyzers=AnalyzerRegistry((LexicalFallbackAnalyzer(),))
    )
    # 单独工作区只验证词法回退，避免混入其他源码身份。
    (tmp_path / "lexical" / "app.rs").write_text(
        "fn run() { unknown(); }\n", encoding="utf-8"
    )
    lexical.scan(tmp_path / "lexical", max_files=10)
    stats = lexical.augment_call_targets(
        resolver_module.LspCallTargetResolver(tmp_path / "lexical")
    )
    assert stats.failed == 0
    assert stats.resolved == 0


def test_repo_context_returns_compact_complete_symbol_ref(tmp_path, monkeypatch):
    import nz_coder.tools.repo_context as tool_module

    (tmp_path / "app.py").write_text("def target(): pass\n", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    symbol_id = index.search_symbols("target")[0]["symbol_id"]
    monkeypatch.setattr(
        tool_module,
        "workspace_repo_intelligence",
        lambda *_args, **_kwargs: SimpleNamespace(index=index),
    )
    result = json.loads(tool_module.repo_context("symbol_ref", module=symbol_id))
    assert result["symbol_id"] == symbol_id
    assert result["selection"] == selection(0, 4, 10)
    assert result["content_hash"]


def test_python_declaration_name_after_explicit_line_join_is_proven(tmp_path):
    source = '@decorator("😀")\nasync def \\\n    target(): pass\n'
    result = PythonAstAnalyzer().analyze_file(
        path=tmp_path / "app.py", relative="app.py", source=source, language="python"
    )
    assert result.symbols[0].selection == selection(2, 4, 10)


def test_tree_sitter_utf8_callee_name_preserves_unicode_identifier(
    tmp_path, monkeypatch
):
    import nz_coder.intelligence.lsp_resolver as resolver_module

    if not TreeSitterAnalyzer().available_for("typescript"):
        pytest.skip("existing TypeScript parser unavailable")
    (tmp_path / "app.ts").write_text(
        'export function run() { "😀"; obj.K() }', encoding="utf-8"
    )
    (tmp_path / "target.ts").write_text("export function K() {}", encoding="utf-8")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    observed = []

    class Client:
        position_encoding = "utf-16"

        def open_document(self, *_args, reader, operation):
            reader()
            return 1

        def document_matches(self, *args):
            return True

        def request(self, _method, params, *, operation):
            observed.append(params["position"])
            return {
                "uri": (tmp_path / "target.ts").as_uri(),
                "range": {"start": {"line": 0, "character": 16}},
            }

    monkeypatch.setattr(
        resolver_module, "get_client_for_file", lambda *_args, operation: Client()
    )
    stats = index.augment_call_targets(resolver_module.LspCallTargetResolver(tmp_path))
    assert observed == [{"line": 0, "character": 34}]
    assert stats.resolved == 1


def test_computed_declaration_name_has_no_proven_identifier_selection(tmp_path):
    analyzer = TreeSitterAnalyzer()
    if not analyzer.available_for("javascript"):
        pytest.skip("existing JavaScript parser unavailable")
    result = analyzer.analyze_file(
        path=tmp_path / "app.js",
        relative="app.js",
        source='class Target { ["dynamic"]() {} }',
        language="javascript",
    )
    method = next(symbol for symbol in result.symbols if symbol.kind == "method")
    assert method.selection is None


def test_lsp_resolver_never_synchronizes_pre_initialization_old_read(
    tmp_path, monkeypatch
):
    import sys
    import nz_coder.intelligence.lsp_resolver as module
    from nz_coder.lsp.client import LSPClient
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    source = tmp_path / "app.py"
    source.write_text("def run(): obj.method()\n")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    access = WorkspaceFileAccess(tmp_path)
    client = LSPClient(
        server_id="fixture",
        command=(sys.executable,),
        root=tmp_path,
        language_id="python",
    )
    client.capabilities = {"capabilities": {"textDocumentSync": 1}}
    sent = []
    client.notify = lambda *a, **k: sent.append(a)
    client.request = lambda *a, **k: None

    def acquire(*a, **k):
        source.write_text("def run(): other.method()\n")
        client.open_document(
            source, reader=lambda: access.read_text_with_identity("app.py")
        )
        return client

    monkeypatch.setattr(module, "get_client_for_file", acquire)
    index.augment_call_targets(module.LspCallTargetResolver(tmp_path))
    assert client._documents[source][1] == source.read_text()
    assert len(sent) == 1


def test_lsp_line_coordinates_preserve_unicode_separator_and_bare_cr(tmp_path):
    from nz_coder.tools.lsp import _position_from_ref

    source = 'marker="\u2028"\rdef target(): pass\r'
    result = PythonAstAnalyzer().analyze_file(
        path=tmp_path / "app.py", relative="app.py", source=source, language="python"
    )
    ref = {"selection": result.symbols[0].selection}
    assert _position_from_ref(ref, source, "utf-16") == (2, 5)
    analyzer = TreeSitterAnalyzer()
    if analyzer.available_for("javascript"):
        source = "const x=1;\rfunction target(){}"
        result = analyzer.analyze_file(
            path=tmp_path / "a.js",
            relative="a.js",
            source=source,
            language="javascript",
        )
        assert _position_from_ref(
            {"selection": result.symbols[0].selection}, source, "utf-16"
        ) == (2, 10)
