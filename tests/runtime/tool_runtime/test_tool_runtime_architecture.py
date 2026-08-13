"""Architectural guards for the canonical Tool Runtime boundary."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _method_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_loop_keeps_only_tool_runtime_compatibility_facades() -> None:
    source = (ROOT / "nz_coder" / "runtime" / "loop.py").read_text(
        encoding="utf-8",
    )
    tree = ast.parse(source)
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("_execute_tools", "_execute_tools_async", "_dispatch_tool_calls", "_dispatch_tool_calls_async"):
        segment = ast.get_source_segment(source, methods[name]) or ""
        assert "ProductionToolRuntime" in segment or "tool_runtime" in segment
        assert "txn.begin" not in segment
        assert "_execute_scheduled" not in segment


def test_tool_runtime_does_not_import_agent_loop() -> None:
    failures = []
    scope = ROOT / "nz_coder" / "runtime" / "tool_runtime"
    for path in scope.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "nz_coder.runtime.loop":
                failures.append(str(path.relative_to(ROOT)))
    assert failures == []


def test_scheduler_has_single_production_owner() -> None:
    loop_methods = _method_names(ROOT / "nz_coder" / "runtime" / "loop.py")
    scheduler_methods = _method_names(
        ROOT / "nz_coder" / "runtime" / "tool_runtime" / "scheduler.py",
    )
    for name in ("_execute_scheduled", "_execute_scheduled_async", "_execute_concurrent", "_execute_concurrent_async"):
        assert name in scheduler_methods
        assert name not in loop_methods
