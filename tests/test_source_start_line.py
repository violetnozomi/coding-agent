"""Certified definition starts are derived in the original Python parse."""

import ast
from pathlib import Path
import sqlite3

import pytest

from nz_coder.intelligence.analyzers import PythonAstAnalyzer, LexicalFallbackAnalyzer
from nz_coder.intelligence.code_index import PersistentCodeIndex, SymbolEntry


@pytest.mark.parametrize(
    "source,start,line",
    [
        ("class Payload:\n    pass\n", 1, 1),
        ("@dataclass\nclass Payload:\n    pass\n", 1, 2),
        ("@first\n@second(x=True)\nclass Payload:\n    pass\n", 1, 3),
        ("@decorator(\n    frozen=True,\n)\nclass Payload:\n    pass\n", 1, 4),
        ("@(\n    decorator\n)\nclass Payload:\n    pass\n", None, 4),
        ("@first\n@(\n    second\n)\nclass Payload:\n    pass\n", None, 5),
        ("@decorator\ndef Payload():\n    pass\n", 1, 2),
        ("@decorator\nasync def Payload():\n    pass\n", 1, 2),
        (
            "class Outer:\n    @decorator(\n        x=True,\n    )\n    class Payload:\n        pass\n",
            2,
            5,
        ),
    ],
)
def test_certified_start(source, start, line, monkeypatch):
    original = ast.parse
    calls = []

    def parse(*a, **kw):
        calls.append(1)
        return original(*a, **kw)

    monkeypatch.setattr(ast, "parse", parse)
    result = PythonAstAnalyzer().analyze_file(
        path=Path("a.py"), relative="a.py", source=source, language="python"
    )
    symbol = next(s for s in result.symbols if s.name == "Payload")
    assert symbol.source_start_line == start
    assert symbol.line == line and symbol.end_line == len(source.splitlines())
    assert calls == [1]


def test_index_rebuild_and_transport(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("@dataclass\nclass Payload:\n    value: int\n")
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    with sqlite3.connect(index.database_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT source_start_line FROM symbols").fetchone()[0] == 1
        db.execute("ALTER TABLE symbols DROP COLUMN source_start_line")
        db.execute("PRAGMA user_version=4")
    rebuilt = PersistentCodeIndex(tmp_path)
    assert not rebuilt.snapshot().files
    _, cold = rebuilt.scan(tmp_path, max_files=10)
    _, warm = rebuilt.scan(tmp_path, max_files=10)
    assert cold.indexed == 1 and warm.reused == 1
    assert rebuilt.snapshot().files[0].symbols[0].source_start_line == 1
    assert rebuilt.symbol_context("Payload")["definition"]["source_start_line"] == 1
    path.write_text("\n@dataclass\nclass Payload:\n    value: int\n")
    generation = rebuilt.snapshot().generation
    rebuilt.scan(tmp_path, max_files=10)
    assert rebuilt.snapshot().generation > generation
    assert rebuilt.symbol_context("Payload")["definition"]["source_start_line"] == 2


def test_default_compatibility():
    assert SymbolEntry("class", "P", "P", 1, 2, None).source_start_line is None
    result = LexicalFallbackAnalyzer().analyze_file(
        path=Path("a.js"), relative="a.js", source="class P {}", language="javascript"
    )
    assert all(s.source_start_line is None for s in result.symbols)
