"""Tools for Python AST-level structure checks."""
from __future__ import annotations

import ast
import difflib
import io
import shlex
import tokenize
from pathlib import Path
from nz_coder.tools import ToolOutput
from nz_coder.runtime.verification.domain.models import ExecutionReceipt

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.protocol.public_error import format_public_error
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tools import register


def _safe_path(p: str) -> Path:
    return WorkspacePathPolicy(current_workdir()).validate_model_read(p)


def python_symbol_check(path: str, symbols: list = None, calls: list = None) -> str:
    """Check module/class/function symbols and simple call relationships with AST."""
    command = shlex.join(["python_symbol_check", path,
        *(part for value in symbols or [] for part in ("--symbol", str(value))),
        *(part for value in calls or [] for part in ("--call", str(value)))])
    receipts = []

    def receipt(capability: str, outcome: str, output: str) -> ExecutionReceipt:
        return ExecutionReceipt("python_symbol_check", command, str(current_workdir()), outcome,
            capabilities=(capability,), targets=(path,), in_process=True, reason=output[-2000:])

    def observed_result(output: str, outcome: str, capability: str) -> ToolOutput:
        return ToolOutput(output, execution_facts=(*receipts, receipt(capability, outcome, output)))

    try:
        fp = _safe_path(path)
        source = WorkspaceFileAccess(current_workdir()).read_text(path)
        tree = ast.parse(source, filename=str(fp))
        # ast.parse 不检查 return 等节点的作用域；syntax 证据须与编译检查同等有效。
        compile(tree, str(fp), "exec", dont_inherit=True)
    except SyntaxError as e:
        return observed_result(format_public_error(e, context=f"Python syntax error in {path}: "), "failed", "syntax")
    except Exception as e:
        return observed_result(format_public_error(e), "not_executed", "syntax")

    symbols = symbols or []
    calls = calls or []
    if symbols or calls:
        receipts.append(receipt("syntax", "succeeded", f"Python compilation passed: {path}"))
    lines = [f"Python AST check for {path}:"]
    ok = True

    module_funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    for symbol in symbols:
        result = _check_symbol(symbol, module_funcs, classes)
        ok = ok and result.startswith("OK:")
        lines.append(result)

    for call in calls:
        result = _check_call(call, module_funcs, classes)
        ok = ok and result.startswith("OK:")
        lines.append(result)

    prefix = "OK" if ok else "FAIL"
    return observed_result(f"{prefix}: " + "\n".join(lines), "succeeded" if ok else "failed",
                           "symbol" if symbols or calls else "syntax")


def python_structural_edit(path: str, insertions: list = None, replacements: list = None) -> str:
    """Apply AST-located insertions/replacements for Python functions and methods."""
    try:
        fp = _safe_path(path)
        source, expected = WorkspaceFileAccess(current_workdir()).read_text_with_identity(path)
        tree = ast.parse(source, filename=str(fp))
        lines = source.splitlines(keepends=True)
        edits = []
        decorator_lines = _decorator_statements(source)

        for item in replacements or []:
            target = item.get("target", "")
            code = item.get("code", "")
            if not target or not code:
                return "Error: each replacement requires target and code"
            node = _find_target_node(target, tree)
            if not node:
                return f"Error: target not found: {target}"
            replacement = _prepare_code_lines(code, indent=" " * node.col_offset)
            edits.append((_definition_start(node, decorator_lines), node.end_lineno, replacement))

        for item in insertions or []:
            before = item.get("before_symbol", "")
            code = item.get("code", "")
            if not before or not code:
                return "Error: each insertion requires before_symbol and code"
            node = _find_target_node(before, tree)
            if not node:
                return f"Error: before_symbol not found: {before}"
            if node.col_offset or "." in before:
                return "Error: insertion anchor must be a module-level definition"
            insertion = _prepare_code_lines(code, indent="")
            start = _definition_start(node, decorator_lines)
            edits.append((start, start, insertion))

        if not edits:
            return "Error: no insertions or replacements provided"

        for index, (start, end, _) in enumerate(edits):
            for other_start, other_end, _ in edits[index + 1:]:
                if (max(start, other_start) < min(end, other_end)
                        or start == end and other_start < start < other_end
                        or other_start == other_end and start < other_start < end):
                    return "Error: overlapping structural edits"

        # 同锚点先替换定义，再倒序插入，使最终顺序与调用者输入一致。
        for _, (start, end, replacement) in sorted(
            enumerate(edits), key=lambda pair: (pair[1][0], pair[1][1], pair[0]), reverse=True,
        ):
            lines[start:end] = replacement
        updated = "".join(lines)
        compile(updated, str(fp), "exec", dont_inherit=True)

        from nz_coder.tools.files import _write_file_version
        result = _write_file_version(path, updated, source, expected)
        diff = "".join(difflib.unified_diff(
            source.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))
        if not diff:
            diff = "(no changes)"
        return f"Applied Python structural edit to {path}\n\nStructural diff:\n{diff}\n\n{result}"
    except SyntaxError as e:
        return format_public_error(e, context=f"Python syntax error in {path}: ")
    except Exception as e:
        return format_public_error(e)


def _decorator_statements(source: str) -> list[tuple[int, int]]:
    """AST 的表达式行号可能晚于 @，以词法逻辑行定位完整装饰器。"""
    statements = []
    first = None
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in {tokenize.INDENT, tokenize.DEDENT, tokenize.NL, tokenize.COMMENT}:
            continue
        if token.type == tokenize.NEWLINE:
            if first is not None and first.string == '@':
                statements.append((first.start[0], token.end[0]))
            first = None
        elif first is None:
            first = token
    return statements


def _definition_start(node, decorator_lines: list[tuple[int, int]]) -> int:
    if not node.decorator_list:
        return node.lineno - 1
    line = node.decorator_list[0].lineno
    return next(start - 1 for start, end in decorator_lines if start <= line <= end)


def _check_symbol(symbol: str, module_funcs: dict, classes: dict) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        return "FAIL: empty symbol"
    parts = symbol.split(".")
    if len(parts) == 1:
        name = parts[0]
        if name in module_funcs:
            return f"OK: module-level function {name} found"
        if name in classes:
            return f"OK: class {name} found"
        return f"FAIL: symbol {name} not found at module level"
    if len(parts) == 2:
        class_name, method_name = parts
        cls = classes.get(class_name)
        if not cls:
            return f"FAIL: class {class_name} not found"
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
                return f"OK: method {symbol} found"
        return f"FAIL: method {symbol} not found"
    return f"FAIL: unsupported symbol format {symbol}"


def _check_call(call: dict, module_funcs: dict, classes: dict) -> str:
    if not isinstance(call, dict):
        return "FAIL: call check must be an object"
    caller = call.get("caller", "")
    callee = call.get("callee", "")
    if not caller or not callee:
        return "FAIL: call check requires caller and callee"
    caller_node = _find_callable(caller, module_funcs, classes)
    if not caller_node:
        return f"FAIL: caller {caller} not found"
    for node in ast.walk(caller_node):
        if isinstance(node, ast.Call) and _call_name(node.func) == callee:
            return f"OK: {caller} calls {callee}"
    return f"FAIL: {caller} does not call {callee}"


def _find_callable(name: str, module_funcs: dict, classes: dict):
    parts = name.split(".")
    if len(parts) == 1:
        return module_funcs.get(parts[0])
    if len(parts) == 2:
        cls = classes.get(parts[0])
        if not cls:
            return None
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[1]:
                return node
    return None


def _find_target_node(name: str, tree):
    module_funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    parts = name.split(".")
    if len(parts) == 1:
        return module_funcs.get(parts[0]) or classes.get(parts[0])
    if len(parts) == 2:
        cls = classes.get(parts[0])
        if not cls:
            return None
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[1]:
                return node
    return None


def _prepare_code_lines(code: str, indent: str) -> list[str]:
    raw_lines = code.strip("\n").splitlines()
    if indent and raw_lines and not raw_lines[0].startswith(indent):
        raw_lines = [(indent + line if line.strip() else line) for line in raw_lines]
    return [line + "\n" for line in raw_lines] + ["\n"]


def _call_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


register(
    name="python_symbol_check",
    description="Check Python module-level functions/classes, class methods, and simple call relationships using AST. Useful after refactors.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative Python file path."},
            "symbols": {
                "type": "array",
                "description": "Symbols to check, e.g. ['validate_email', 'UserManager', 'UserManager.create_user'].",
                "items": {"type": "string"},
            },
            "calls": {
                "type": "array",
                "description": "Call relationships to check.",
                "items": {
                    "type": "object",
                    "properties": {
                        "caller": {"type": "string", "description": "Callable name, e.g. UserManager.create_user."},
                        "callee": {"type": "string", "description": "Called function name, e.g. validate_email."},
                    },
                    "required": ["caller", "callee"],
                },
            },
        },
        "required": ["path"],
    },
    handler=python_symbol_check,
    execution="read",
)

register(
    name="python_structural_edit",
    description="Edit Python code by AST symbol locations. Can insert module-level code before a symbol and replace a module function or class method. Prefer for refactors over fragile exact text patches.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative Python file path."},
            "insertions": {
                "type": "array",
                "description": "Module-level code insertions before an existing symbol.",
                "items": {
                    "type": "object",
                    "properties": {
                        "before_symbol": {"type": "string", "description": "Existing module-level function/class before which code is inserted."},
                        "code": {"type": "string", "description": "Module-level Python code to insert."},
                    },
                    "required": ["before_symbol", "code"],
                },
            },
            "replacements": {
                "type": "array",
                "description": "Function/method replacements by symbol name.",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Function or method target, e.g. validate_email or UserManager.create_user."},
                        "code": {"type": "string", "description": "Full replacement function/method source."},
                    },
                    "required": ["target", "code"],
                },
            },
        },
        "required": ["path"],
    },
    handler=python_structural_edit,
    execution="write",
    side_effect="mutates-fs",
)
