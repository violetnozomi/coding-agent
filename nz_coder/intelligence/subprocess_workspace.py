"""Static diagnosis for test helpers that launch Python from a stale cwd."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_FAILED_TEST_RE = re.compile(
    r"(?:FAILED|ERROR)\s+([^\s:]+\.py)(?:::[A-Za-z_][\w.]*)?",
    re.IGNORECASE,
)
_SUBPROCESS_METHODS = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
})


@dataclass(frozen=True)
class SubprocessWorkspaceDrift:
    """One statically proven subprocess cwd mismatch."""

    helper: str
    resolved_cwd: Path
    active_workspace: Path
    package: str
    line: int


def diagnose_subprocess_workspace_drift(
    output: str,
    *,
    workspace: str | Path,
) -> SubprocessWorkspaceDrift | None:
    """Inspect failing helpers without evaluating arbitrary Python expressions."""
    root = Path(workspace).resolve()
    for relative in _failing_test_paths(output):
        helper = (root / relative).resolve()
        try:
            helper.relative_to(root)
        except ValueError:
            continue
        if not helper.is_file() or helper.stat().st_size > 512_000:
            continue
        result = _diagnose_helper(helper, relative=relative, workspace=root)
        if result is not None:
            return result
    return None


def _diagnose_helper(
    helper: Path,
    *,
    relative: str,
    workspace: Path,
) -> SubprocessWorkspaceDrift | None:
    try:
        tree = ast.parse(helper.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None

    bindings: dict[str, Path] = {}
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for _ in range(3):
        changed = False
        for assignment in assignments:
            value = _resolve_path_expr(assignment.value, helper=helper, bindings=bindings)
            if value is None:
                continue
            for target in assignment.targets:
                if isinstance(target, ast.Name) and bindings.get(target.id) != value:
                    bindings[target.id] = value
                    changed = True
        if not changed:
            break

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_call(node.func):
            continue
        package = _python_module(node)
        if not package:
            continue
        cwd_node = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "cwd"),
            None,
        )
        if cwd_node is None:
            continue
        resolved_cwd = _resolve_path_expr(
            cwd_node,
            helper=helper,
            bindings=bindings,
        )
        if resolved_cwd is None or resolved_cwd == workspace:
            continue
        return SubprocessWorkspaceDrift(
            helper=PurePosixPath(relative.replace("\\", "/")).as_posix(),
            resolved_cwd=resolved_cwd,
            active_workspace=workspace,
            package=package.split(".", 1)[0],
            line=max(1, int(getattr(node, "lineno", 1) or 1)),
        )
    return None


def _failing_test_paths(output: str) -> tuple[str, ...]:
    result: list[str] = []
    for match in _FAILED_TEST_RE.finditer(str(output or "")):
        value = match.group(1).replace("\\", "/")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            continue
        normalized = path.as_posix()
        if normalized not in result:
            result.append(normalized)
    return tuple(result[:5])


def _is_subprocess_call(node: ast.AST) -> bool:
    return bool(
        isinstance(node, ast.Attribute)
        and node.attr in _SUBPROCESS_METHODS
        and isinstance(node.value, ast.Name)
        and node.value.id == "subprocess"
    )


def _python_module(call: ast.Call) -> str:
    if not call.args:
        return ""
    argv = call.args[0]
    if not isinstance(argv, (ast.List, ast.Tuple)):
        return ""
    values = [
        item.value if isinstance(item, ast.Constant) and isinstance(item.value, str) else None
        for item in argv.elts
    ]
    for index, value in enumerate(values[:-1]):
        if value == "-m" and values[index + 1]:
            module = str(values[index + 1])
            return module if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) else ""
    return ""


def _resolve_path_expr(
    node: ast.AST,
    *,
    helper: Path,
    bindings: dict[str, Path],
) -> Path | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _absolute(Path(node.value), helper=helper)
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return helper.resolve()
        return bindings.get(node.id)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "Path" and len(node.args) == 1:
            return _resolve_path_expr(node.args[0], helper=helper, bindings=bindings)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "resolve"
            and not node.args
        ):
            base = _resolve_path_expr(node.func.value, helper=helper, bindings=bindings)
            return base.resolve() if base is not None else None
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _resolve_path_expr(node.value, helper=helper, bindings=bindings)
        return base.parent if base is not None else None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
    ):
        base = _resolve_path_expr(node.value.value, helper=helper, bindings=bindings)
        index = _integer_literal(node.slice)
        if base is None or index is None or index < 0:
            return None
        try:
            return base.parents[index]
        except IndexError:
            return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _resolve_path_expr(node.left, helper=helper, bindings=bindings)
        if base is None or not isinstance(node.right, ast.Constant):
            return None
        if not isinstance(node.right.value, str):
            return None
        return (base / node.right.value).resolve()
    return None


def _absolute(path: Path, *, helper: Path) -> Path:
    return path.resolve() if path.is_absolute() else (helper.parent / path).resolve()


def _integer_literal(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


__all__ = [
    "SubprocessWorkspaceDrift",
    "diagnose_subprocess_workspace_drift",
]
