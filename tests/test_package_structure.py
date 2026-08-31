"""Architecture contracts for the public package root and domain boundaries."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
import subprocess
import sys

import pytest

import nz_coder


PACKAGE_ROOT = Path(nz_coder.__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent

EXPECTED_ROOT_MODULES = {
    "__init__.py",
    "__main__.py",
    "aider_benchmark.py",
    "benchmark.py",
    "cli.py",
    "eval_runner.py",
    "loop.py",
    "permissions.py",
    "sdk.py",
    "swebench_lite.py",
}

EXPECTED_RUNTIME_ROOT_MODULES = {"__init__.py"}
EXPECTED_RUNTIME_SUBPACKAGES = {
    "adapters",
    "agent",
    "conversation",
    "core",
    "execution",
    "model_gateway",
    "observability",
    "process",
    "session",
    "tool_runtime",
    "verification",
    "workflows",
    "worktree",
}

RUNTIME_LEAF_PACKAGES = (
    "adapters",
    "agent",
    "conversation",
    "core",
    "model_gateway",
    "observability",
    "process",
    "session",
    "tool_runtime",
    "verification",
    "workflows",
    "worktree",
)

RUNTIME_EXECUTION_IMPORT_ALLOWLIST = {
    (
        "nz_coder/runtime/adapters/runner.py",
        "nz_coder.runtime.execution.run_lifecycle",
    ),
    (
        "nz_coder/runtime/agent/subagent.py",
        "nz_coder.runtime.execution.composition",
    ),
}

PUBLIC_MODULES = (
    "nz_coder.sdk",
    "nz_coder.cli",
    "nz_coder.loop",
    "nz_coder.permissions",
    "nz_coder.benchmark",
    "nz_coder.aider_benchmark",
    "nz_coder.eval_runner",
    "nz_coder.swebench_lite",
)

INTERNAL_ROOT_MODULES = {
    "async_utils",
    "attachments",
    "changes",
    "command_policy",
    "config",
    "context",
    "doctor",
    "documents",
    "impact_analyzer",
    "initializer",
    "json_safety",
    "memory",
    "message_schema",
    "private_paths",
    "project_profile",
    "prompt",
    "recovery",
    "reviewer",
    "ripgrep",
    "run_evidence",
    "runtime_state",
    "session_events",
    "session_stats",
    "sessions",
    "skills",
    "subagent",
    "task_policy",
    "test",
    "tool_executor",
    "trace",
    "transaction",
    "verification",
    "verification_evidence",
    "verification_planner",
    "vision",
    "web_search",
    "workspace",
}

INTERNAL_RUNTIME_ROOT_MODULES = {
    "admission",
    "agent_manager",
    "agent_resilience",
    "agent_role_runtime",
    "agent_transition_runtime",
    "async_utils",
    "auto_mode",
    "child_contracts",
    "child_result",
    "completion_gate",
    "composition",
    "context_manager",
    "continuation_context",
    "execution_context",
    "guardrail_runtime",
    "guardrails",
    "handoffs",
    "hooks",
    "host",
    "input_preflight",
    "lineage",
    "llm_judge",
    "loop",
    "message_projection",
    "message_runtime",
    "model_result",
    "native_sdk",
    "planning_runtime",
    "platform_runtime",
    "process_backends",
    "process_service",
    "product_surfaces",
    "prompt",
    "prompt_builder",
    "provider_stream",
    "recovery",
    "ripgrep",
    "run_lifecycle",
    "runner",
    "runtime_state",
    "services",
    "session_cleanup",
    "session_processor",
    "session_repository",
    "session_revert",
    "sidecar_verifier",
    "snapshot_runtime",
    "stall_detector",
    "stall_sidecar",
    "structured_output",
    "subagent",
    "task_contract",
    "task_policy",
    "think_tags",
    "tool_executor",
    "tool_observers",
    "turn_economy",
    "usage_history",
    "verification_contract",
    "verification_scheduler",
    "work_budget",
    "workdir",
    "workflow_builtins",
    "workflow_capsule",
    "workflow_contracts",
    "workflow_features",
    "workflow_generation",
    "workflow_host",
    "workflow_library",
    "workflow_lifecycle",
    "workflow_manifest",
    "workflow_process",
    "workflow_resolver",
    "workflow_review",
    "workflow_run_store",
    "workflow_runtime",
    "workflow_sdk",
    "workflow_sweep",
    "workspace_snapshot",
}


def _imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                package_parts = []
                parent = path.parent
                while (parent / "__init__.py").is_file():
                    package_parts.append(parent.name)
                    parent = parent.parent
                package_parts.reverse()
                keep = len(package_parts) - node.level + 1
                if keep >= 0:
                    suffix = module.split(".") if module else []
                    module = ".".join(package_parts[:keep] + suffix)
            if module:
                imports.append((module, tuple(alias.name for alias in node.names)))
    return imports


def _runtime_execution_imports(path: Path) -> list[str]:
    prefix = "nz_coder.runtime.execution"
    matches = []
    for module, names in _imports(path):
        if module == prefix or module.startswith(prefix + "."):
            matches.append(module)
            continue
        for name in names:
            candidate = f"{module}.{name}"
            if candidate == prefix or candidate.startswith(prefix + "."):
                matches.append(candidate)
    return matches


def _python_files(*roots: Path):
    for root in roots:
        if root.is_dir():
            yield from root.rglob("*.py")


def test_relative_runtime_execution_import_is_detected(tmp_path):
    package = tmp_path / "nz_coder" / "runtime" / "agent"
    package.mkdir(parents=True)
    for parent in (package, package.parent, package.parent.parent):
        (parent / "__init__.py").write_text("", encoding="utf-8")
    module = package / "relative_imports.py"
    module.write_text(
        "from ..execution import tool_executor\n"
        "from ..execution.tool_executor import ToolExecutionResult\n"
        "from . import guardrails\n",
        encoding="utf-8",
    )

    assert _runtime_execution_imports(module) == [
        "nz_coder.runtime.execution",
        "nz_coder.runtime.execution.tool_executor",
    ]


def test_package_root_contains_only_public_facades():
    actual = {path.name for path in PACKAGE_ROOT.glob("*.py")}
    assert actual == EXPECTED_ROOT_MODULES


def test_runtime_root_contains_only_domain_packages():
    runtime_root = PACKAGE_ROOT / "runtime"
    actual_modules = {path.name for path in runtime_root.glob("*.py")}
    actual_packages = {
        path.name
        for path in runtime_root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert actual_modules == EXPECTED_RUNTIME_ROOT_MODULES
    assert actual_packages == EXPECTED_RUNTIME_SUBPACKAGES


@pytest.mark.parametrize(
    "package_name",
    ("core", "model_gateway", "session", "tool_runtime", "worktree"),
)
def test_runtime_package_initializers_are_dependency_light(package_name):
    module_name = f"nz_coder.runtime.{package_name}"
    code = (
        "import importlib, sys\n"
        f"module_name = {module_name!r}\n"
        "importlib.import_module(module_name)\n"
        "loaded = sorted(\n"
        "    name for name in sys.modules if name.startswith(module_name + '.')\n"
        ")\n"
        "assert loaded == [], loaded\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_runtime_leaf_packages_do_not_import_execution_orchestration():
    runtime_root = PACKAGE_ROOT / "runtime"
    violations = []
    for package_name in RUNTIME_LEAF_PACKAGES:
        for path in _python_files(runtime_root / package_name):
            for module in _runtime_execution_imports(path):
                violation = (
                    str(path.relative_to(REPOSITORY_ROOT)),
                    module,
                )
                if violation in RUNTIME_EXECUTION_IMPORT_ALLOWLIST:
                    continue
                violations.append(f"{violation[0]}: {violation[1]}")
    assert violations == []


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_public_module_remains_importable(module_name):
    assert importlib.import_module(module_name) is not None


def test_repository_uses_canonical_internal_imports():
    violations = []
    for path in _python_files(
        PACKAGE_ROOT,
        REPOSITORY_ROOT / "tests",
        REPOSITORY_ROOT / "scripts",
    ):
        for module, names in _imports(path):
            if module == "nz_coder":
                stale = sorted(INTERNAL_ROOT_MODULES.intersection(names))
                if stale:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}: from nz_coder import "
                        + ", ".join(stale)
                    )
                continue
            if not module.startswith("nz_coder."):
                continue
            root_name = module.split(".", 2)[1]
            if root_name in INTERNAL_ROOT_MODULES:
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}: {module}"
                )
    assert violations == []


def test_repository_uses_canonical_runtime_imports():
    violations = []
    for path in _python_files(
        PACKAGE_ROOT,
        REPOSITORY_ROOT / "tests",
        REPOSITORY_ROOT / "scripts",
    ):
        for module, names in _imports(path):
            if module == "nz_coder.runtime":
                stale = sorted(INTERNAL_RUNTIME_ROOT_MODULES.intersection(names))
                if stale:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}: "
                        "from nz_coder.runtime import " + ", ".join(stale)
                    )
                continue
            if not module.startswith("nz_coder.runtime."):
                continue
            runtime_name = module.split(".", 3)[2]
            if runtime_name in INTERNAL_RUNTIME_ROOT_MODULES:
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}: {module}"
                )
    assert violations == []


def test_foundation_and_protocol_do_not_import_runtime_or_product_layers():
    forbidden = (
        "nz_coder.runtime",
        "nz_coder.interface",
        "nz_coder.evaluation",
        "nz_coder.swebench",
        "nz_coder.tools",
        "nz_coder.tool_platform",
    )
    violations = []
    for path in _python_files(
        PACKAGE_ROOT / "foundation",
        PACKAGE_ROOT / "protocol",
    ):
        for module, _names in _imports(path):
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in forbidden
            ):
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}: {module}"
                )
    assert violations == []
