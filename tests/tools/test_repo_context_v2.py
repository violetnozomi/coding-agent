"""Tool contracts for symbol and process repository contexts."""
from __future__ import annotations

import json


def test_repo_context_exposes_symbol_and_process_operations(tmp_path, monkeypatch) -> None:
    from nz_coder.foundation import config
    from nz_coder.tools.repo_context import repo_context

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    (tmp_path / "flow.py").write_text(
        "def leaf(): return 1\ndef entry(): return leaf()\n", encoding="utf-8",
    )

    symbol = json.loads(repo_context("symbol_context", module="leaf", refresh=True))
    process = json.loads(repo_context("process_context", module="entry"))
    search = json.loads(repo_context("symbol_search", module="leaf"))

    assert symbol["definition"]["name"] == "leaf"
    assert process["edges"][0]["callee"] == "leaf"
    assert search["matches"][0]["name"] == "leaf"
