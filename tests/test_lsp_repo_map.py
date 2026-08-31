"""Tests for best-effort LSP workspace-symbol Repo Map enrichment."""
from __future__ import annotations

from pathlib import Path


class _FakeClient:
    server_id = "fake"

    def __init__(self, response: object):
        self.response = response
        self.opened: list[Path] = []
        self.requests: list[tuple[str, dict[str, str]]] = []

    def open_document(self, path: Path) -> int:
        self.opened.append(path)
        return 0

    def request(self, method: str, params: dict[str, str]) -> object:
        self.requests.append((method, params))
        return self.response


def _symbol(
    path: Path,
    *,
    name: str,
    kind: int,
    line: int = 0,
    character: int = 0,
    container: str = "",
) -> dict[str, object]:
    from nz_coder.lsp.client import path_to_uri

    result: dict[str, object] = {
        "name": name,
        "kind": kind,
        "location": {
            "uri": path_to_uri(path),
            "range": {
                "start": {"line": line, "character": character},
                "end": {"line": line, "character": character},
            },
        },
    }
    if container:
        result["containerName"] = container
    return result


def test_workspace_symbols_filter_rank_bound_and_scope(tmp_path, monkeypatch):
    from nz_coder.lsp import workspace_symbols as module

    package = tmp_path / "pkg"
    package.mkdir()
    probe = package / "service.py"
    probe.write_text("class Service:\n    pass\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("outside = 1\n", encoding="utf-8")
    response = [
        _symbol(probe, name="ServiceFactory", kind=12, line=5),
        _symbol(probe, name="Service", kind=5, line=0, character=6, container="pkg"),
        _symbol(probe, name="Service", kind=5, line=0, character=6, container="pkg"),
        _symbol(probe, name="ignoredProperty", kind=7),
        _symbol(outside, name="ServiceOutside", kind=5),
        {"name": "malformed", "kind": 5, "location": {}},
    ]
    client = _FakeClient(response)
    monkeypatch.setattr(module, "get_client_for_file", lambda *_: client)

    result = module.collect_workspace_symbols(
        probe=probe,
        workspace=tmp_path,
        base=package,
        query="Service",
        limit=1,
    )

    assert result.source == "lsp/fake"
    assert len(result.symbols) == 1
    assert result.symbols[0].name == "Service"
    assert result.symbols[0].path == "pkg/service.py"
    assert result.symbols[0].line == 1
    assert result.symbols[0].character == 7
    assert result.symbols[0].container == "pkg"
    assert result.notice == "LSP symbols truncated at 1"
    assert client.opened == [probe]
    assert client.requests == [("workspace/symbol", {"query": "Service"})]


def test_workspace_symbols_retry_once_after_nonempty_query_empty_response(
    tmp_path,
    monkeypatch,
):
    from nz_coder.lsp import workspace_symbols as module

    probe = tmp_path / "service.py"
    probe.write_text("class Service:\n    pass\n", encoding="utf-8")
    response_values = [
        [],
        [
            _symbol(
                probe,
                name="Service",
                kind=5,
                line=0,
            )
        ],
    ]
    client = _FakeClient([])
    slept: list[float] = []

    def request(method: str, params: dict[str, str]) -> object:
        client.requests.append((method, params))
        return response_values.pop(0)

    monkeypatch.setattr(module, "get_client_for_file", lambda *_: client)
    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(module.time, "sleep", slept.append)

    result = module.collect_workspace_symbols(
        probe=probe,
        workspace=tmp_path,
        base=tmp_path,
        query="Service",
        limit=10,
    )

    assert result.symbols[0].name == "Service"
    assert len(client.requests) == 2
    assert slept == [module._WORKSPACE_SYMBOL_WARMUP_SECONDS]

def test_workspace_symbols_gracefully_fall_back_on_client_failure(
    tmp_path,
    monkeypatch,
):
    from nz_coder.lsp import workspace_symbols as module

    probe = tmp_path / "app.py"
    probe.write_text("value = 1\n", encoding="utf-8")

    def fail(*_args):
        raise RuntimeError("server failed")

    monkeypatch.setattr(module, "get_client_for_file", fail)

    result = module.collect_workspace_symbols(
        probe=probe,
        workspace=tmp_path,
        base=tmp_path,
        query="value",
        limit=10,
    )

    assert result.source == ""
    assert result.symbols == ()
    assert result.notice == "LSP semantic enrichment unavailable"


def test_repo_map_semantic_mode_adds_lsp_supplement(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.lsp.workspace_symbols import (
        WorkspaceSymbolEntry,
        WorkspaceSymbolResult,
    )
    from nz_coder.tools import repo_map as module

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    source = tmp_path / "app.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def collect(**kwargs):
        calls.append(kwargs)
        return WorkspaceSymbolResult(
            source="lsp/fake",
            symbols=(
                WorkspaceSymbolEntry(
                    path="app.py",
                    name="answer",
                    kind="function",
                    line=1,
                    character=5,
                ),
            ),
        )

    monkeypatch.setattr(module, "collect_workspace_symbols", collect)

    plain = module.repo_map(query="answer")
    semantic = module.repo_map(query="answer", semantic=True)

    assert "LSP workspace symbols" not in plain
    assert "LSP workspace symbols" in semantic
    assert "semantic_source: lsp/fake, symbols: 1" in semantic
    assert "app.py:1:5 | function answer" in semantic
    assert len(calls) == 1
    assert calls[0]["probe"] == source
    assert calls[0]["base"] == tmp_path


def test_workspace_symbols_enforce_infcode_per_client_cap(tmp_path, monkeypatch):
    from nz_coder.lsp import workspace_symbols as module

    probe = tmp_path / "items.py"
    probe.write_text("value = 1\n", encoding="utf-8")
    client = _FakeClient([
        _symbol(
            probe,
            name=f"Item{index}",
            kind=5,
            line=index,
        )
        for index in range(12)
    ])
    monkeypatch.setattr(module, "get_client_for_file", lambda *_: client)

    result = module.collect_workspace_symbols(
        probe=probe,
        workspace=tmp_path,
        base=tmp_path,
        query="Item",
        limit=100,
    )

    assert len(result.symbols) == module._WORKSPACE_SYMBOL_MAX == 10
    assert result.notice == "LSP symbols truncated at 10"


def test_uri_to_path_rejects_remote_file_authority():
    from nz_coder.lsp.client import uri_to_path

    assert uri_to_path("file://remote-host/workspace/app.py") is None
    assert uri_to_path("https://example.com/app.py") is None
    assert uri_to_path("file:///workspace/app.py") == Path("/workspace/app.py")
