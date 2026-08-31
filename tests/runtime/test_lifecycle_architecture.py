"""Architecture gates for the focused production run lifecycle."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_production_run_lifecycle_consumes_focused_context() -> None:
    path = ROOT / "nz_coder" / "runtime" / "execution" / "run_lifecycle.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    runtime = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductionRunLifecycle"
    )
    segment = ast.get_source_segment(source, runtime) or ""

    assert "host." not in segment
    assert "LifecycleExecutionContext" in segment

    adapter = (
        ROOT / "nz_coder" / "runtime" / "adapters" / "lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "lifecycle_context_from_legacy_host" in adapter
    assert "commit" in adapter
