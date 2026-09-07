"""Architecture boundary tests for removed parallel product surfaces."""
from __future__ import annotations

from pathlib import Path
import ast


def test_legacy_dodo_product_sources_are_absent():
    """Dodo/PySide must not silently grow back beside the core Session API."""
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        root / "dodo_server_min.py",
        root / "nz_coder" / "dodo",
        root / "nz_coder" / "pyside_client",
        root / "requirements-dodo.txt",
        root / "requirements-client.txt",
    )
    for path in forbidden:
        if path.is_dir():
            assert not list(path.glob("*.py")), f"legacy product source returned: {path}"
        else:
            assert not path.exists(), f"legacy product source returned: {path}"


def test_tool_core_cannot_import_outer_host_adapters():
    """Conversion is one-way, including imports hidden in TYPE_CHECKING/functions."""
    root = Path(__file__).resolve().parents[1] / "nz_coder/runtime/tool_runtime"
    forbidden = ("nz_coder.runtime.adapters", "nz_coder.runtime.execution", "nz_coder.interface")
    violations = []
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                if node.level:
                    base = "nz_coder.runtime.tool_runtime".split(".")
                    modules = [".".join(base[:len(base) - node.level + 1] + modules)]
            else:
                continue
            violations.extend((path.name, node.lineno, module) for module in modules
                              if module.startswith(forbidden))
    assert not violations, violations


def test_focused_tool_owners_cannot_escape_through_public_product_facades():
    """Include local/type-only imports, aliases and literal dynamic imports."""
    root = Path(__file__).resolve().parents[1]
    files = [
        *root.joinpath("nz_coder/runtime/tool_runtime").glob("*.py"),
        root / "nz_coder/runtime/core/tool_context.py",
        root / "nz_coder/runtime/core/tool_contracts.py",
        root / "nz_coder/runtime/execution/tool_effects.py",
        root / "nz_coder/runtime/session/tool_progress.py",
        root / "nz_coder/runtime/process/tool_snapshots.py",
    ]
    forbidden = ("nz_coder.runtime.adapters", "nz_coder.runtime.execution.loop",
                 "nz_coder.loop", "nz_coder.interface", "nz_coder.sdk")
    violations = []
    for path in files:
        package = ".".join(path.relative_to(root).parts[:-1])
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    prefix = ".".join(package.split(".")[:len(package.split(".")) - node.level + 1] + ([prefix] if prefix else []))
                modules = [prefix, *(prefix + "." + alias.name for alias in node.names)]
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == "__import__"
                or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
            ):
                assert node.args and isinstance(node.args[0], ast.Constant), (path.name, node.lineno, "undeclared dynamic import")
                modules = [str(node.args[0].value)]
            violations.extend((path.name, node.lineno, module) for module in modules if module.startswith(forbidden))
    assert not violations, violations


def test_focused_tools_import_without_loading_product_environment():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import sys; import nz_coder.runtime.tool_runtime.pipeline; "
         "import nz_coder.runtime.execution.tool_effects; import nz_coder.runtime.session.tool_progress; "
         "assert 'nz_coder.runtime.execution.loop' not in sys.modules; "
         "assert 'nz_coder.interface.cli' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
