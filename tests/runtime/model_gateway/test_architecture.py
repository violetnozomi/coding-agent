"""Enforce the sole Agent Core SDK/Provider call boundary."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCOPES = (
    ROOT / "nz_coder" / "runtime",
    ROOT / "nz_coder" / "state",
)


def _attribute_name(node: ast.AST) -> str:
    values = []
    current = node
    while isinstance(current, ast.Attribute):
        values.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        values.append(current.id)
    return ".".join(reversed(values))


def test_agent_core_has_one_model_sdk_boundary() -> None:
    forbidden = []
    files = [
        path
        for scope in SCOPES
        for path in scope.rglob("*.py")
        if "model_gateway" not in path.parts
    ] + [ROOT / "nz_coder" / "vision.py"]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _attribute_name(node.func)
            if (
                name.endswith(".create_completion")
                or name.endswith(".chat.completions.create")
                or name.endswith(".responses.create")
            ):
                forbidden.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:{name}"
                )
    assert forbidden == []
