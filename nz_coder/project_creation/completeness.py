"""Check whether a generated project satisfies its requested requirements."""
from __future__ import annotations

import json
from pathlib import Path

from nz_coder.project_creation.inspector import inspect_generated_project
from nz_coder.tools import register
from nz_coder.tools.files import _safe_path


_SQLITE_FALLBACK_NOTE = (
    "SQLite requested but the scaffold uses documented in-memory storage instead of real persistence."
)


def _project_root(project_dir: str) -> Path:
    return _safe_path(project_dir or ".")


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _normalize_blueprint_path(path: str, root: Path) -> str:
    normalized = (path or "").strip().lstrip("./")
    prefix = f"{root.name}/" if root.name else ""
    if prefix and normalized.startswith(prefix):
        return normalized[len(prefix):]
    return normalized


def _planned_file_state(root: Path, blueprint: dict) -> tuple[list[str], list[str]]:
    present: list[str] = []
    missing: list[str] = []
    for item in blueprint.get("files", []):
        raw_path = str(item.get("path", "")).strip()
        if not raw_path:
            continue
        relative = _normalize_blueprint_path(raw_path, root)
        target = root / relative
        if target.exists():
            present.append(raw_path)
        else:
            missing.append(raw_path)
    return present, missing


def _feature_state(features: list[str], checks: dict, implemented: list[str], missing: list[str], notes: list[str]) -> None:
    if "CRUD operations" in features:
        if checks.get("create_todo_endpoint") and checks.get("update_todo_endpoint") and checks.get("delete_todo_endpoint"):
            _append_unique(implemented, "CRUD endpoints")
        else:
            _append_unique(missing, "CRUD endpoints")
        if checks.get("crud_tests"):
            _append_unique(implemented, "CRUD pytest coverage")
        else:
            _append_unique(missing, "CRUD pytest coverage")

    if "SQLite persistence" in features:
        if checks.get("sqlite_support"):
            _append_unique(implemented, "SQLite persistence")
        else:
            _append_unique(missing, "SQLite persistence")
            if checks.get("sqlite_fallback_documented"):
                _append_unique(notes, _SQLITE_FALLBACK_NOTE)

    if "pytest tests" in features:
        if checks.get("pytest_tests"):
            _append_unique(implemented, "pytest tests")
        else:
            _append_unique(missing, "pytest tests")

    if "README quickstart" in features:
        if checks.get("readme_quickstart"):
            _append_unique(implemented, "README quickstart")
        else:
            _append_unique(missing, "README quickstart")

    if "health endpoint" in features:
        if checks.get("health_endpoint"):
            _append_unique(implemented, "health endpoint")
        else:
            _append_unique(missing, "health endpoint")

    if "CLI entrypoint" in features:
        if checks.get("cli_entrypoint") and checks.get("main_module"):
            _append_unique(implemented, "CLI entrypoint")
        else:
            _append_unique(missing, "CLI entrypoint")

    if "importable package API" in features:
        if checks.get("package_api"):
            _append_unique(implemented, "package API")
        else:
            _append_unique(missing, "package API")

    if "slugify utility" in features:
        if checks.get("slugify_function"):
            _append_unique(implemented, "slugify utility")
        else:
            _append_unique(missing, "slugify utility")

    if "REST API" in features:
        if checks.get("fastapi_app"):
            _append_unique(implemented, "REST API")
        else:
            _append_unique(missing, "REST API")


def _next_steps(missing: list[str], notes: list[str]) -> list[str]:
    steps: list[str] = []
    if any(item.startswith("planned files:") for item in missing):
        steps.append("Create the remaining planned files with scaffold_project or write_files_batch.")
    if "CRUD endpoints" in missing or "CRUD pytest coverage" in missing:
        steps.append("Implement the missing CRUD routes and expand tests/test_api.py coverage.")
    if "SQLite persistence" in missing:
        if _SQLITE_FALLBACK_NOTE in notes:
            steps.append("Keep the in-memory demo claim explicit, or switch to a sqlite-specific template before claiming persistence.")
        else:
            steps.append("Add real SQLite persistence or remove the SQLite requirement from the request.")
    if "pytest tests" in missing:
        steps.append("Add or fix pytest coverage for the generated project.")
    if "README quickstart" in missing:
        steps.append("Add Quickstart and limitations to README.md.")
    if "CLI entrypoint" in missing:
        steps.append("Add the CLI entrypoint and python -m module wrapper.")
    if "package API" in missing or "slugify utility" in missing:
        steps.append("Implement the requested package API in src/<package>/__init__.py.")
    return steps[:4]


def check_project_completeness(project_spec: dict, blueprint: dict, project_dir: str) -> dict:
    root = _project_root(project_dir)
    if not root.exists() or not root.is_dir():
        return {
            "status": "missing",
            "implemented": [],
            "missing": ["project directory"],
            "notes": ["Project directory does not exist yet."],
            "recommended_next_steps": ["Run scaffold_project to create the initial project files."],
        }

    spec = dict(project_spec or {})
    plan = dict(blueprint or {})
    features = [str(item) for item in spec.get("features", [])]
    inspection = inspect_generated_project(str(root), str(spec.get("project_type") or plan.get("project_type") or ""))
    checks = dict(inspection.get("checks", {}))

    implemented: list[str] = []
    missing: list[str] = []
    notes: list[str] = list(plan.get("notes", []))
    for item in inspection.get("notes", []):
        _append_unique(notes, str(item))

    present_files, missing_files = _planned_file_state(root, plan)
    if present_files:
        implemented.append("planned blueprint files")
    if missing_files:
        preview = ", ".join(missing_files[:3])
        if len(missing_files) > 3:
            preview += ", ..."
        missing.append(f"planned files: {preview}")

    _feature_state(features, checks, implemented, missing, notes)

    status = "ok" if not missing else ("partial" if implemented else "missing")
    return {
        "status": status,
        "implemented": implemented,
        "missing": missing,
        "notes": notes,
        "recommended_next_steps": _next_steps(missing, notes),
    }


def check_project_completeness_tool(project_spec: dict, blueprint: dict, project_dir: str) -> str:
    try:
        payload = check_project_completeness(project_spec, blueprint, project_dir)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="check_project_completeness",
    description=(
        "Compare a generated project directory against the requested spec and blueprint, then report what is "
        "implemented, what is missing, and what follow-up steps are recommended."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_spec": {"type": "object", "description": "Structured project spec from analyze_project_requirements."},
            "blueprint": {"type": "object", "description": "Blueprint from create_project_blueprint."},
            "project_dir": {"type": "string", "description": "Generated project directory relative to the workspace."},
        },
        "required": ["project_spec", "blueprint", "project_dir"],
    },
    handler=check_project_completeness_tool,
)
