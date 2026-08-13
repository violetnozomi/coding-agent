"""Inspect generated project files for concrete implementation signals."""
from __future__ import annotations

import json
from pathlib import Path

from nz_coder.tools import register
from nz_coder.tools.files import _safe_path


_SQLITE_FALLBACK_NOTE = (
    "SQLite requested but the scaffold documents an in-memory fallback instead of "
    "real SQLite persistence."
)


def _project_root(project_dir: str) -> Path:
    return _safe_path(project_dir or ".")


def _read_text(root: Path, relative: str) -> str:
    path = root / relative
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _has_any(text: str, candidates: tuple[str, ...]) -> bool:
    return any(candidate in text for candidate in candidates)


def _route_exists(text: str, method: str, route: str) -> bool:
    return _has_any(
        text,
        (
            f"@app.{method}('{route}'",
            f'@app.{method}("{route}"',
        ),
    )


def _readme_has_quickstart(text: str) -> bool:
    lowered = text.lower()
    return "quickstart" in lowered or ("pytest" in lowered and "uvicorn" in lowered) or "usage" in lowered


def _infer_project_type(root: Path) -> str:
    if (root / "app" / "main.py").exists():
        return "fastapi_service"
    if (root / "pyproject.toml").exists() and (root / "src" / root.name / "cli.py").exists():
        return "python_cli"
    if (root / "pyproject.toml").exists() and (root / "src" / root.name / "__init__.py").exists():
        return "python_package"
    if (root / "app.py").exists():
        return "generic_python"
    return "generic_python"


def _status_from(implemented: list[str], missing: list[str]) -> str:
    if not missing:
        return "ok"
    if implemented:
        return "partial"
    return "missing"


def _inspect_fastapi(root: Path) -> dict:
    main_text = _read_text(root, "app/main.py")
    models_text = _read_text(root, "app/models.py")
    tests_text = _read_text(root, "tests/test_api.py")
    readme_text = _read_text(root, "README.md")

    checks = {
        "fastapi_app": "FastAPI(" in main_text,
        "health_endpoint": _route_exists(main_text, "get", "/health"),
        "list_todos_endpoint": _route_exists(main_text, "get", "/todos"),
        "create_todo_endpoint": _route_exists(main_text, "post", "/todos"),
        "get_todo_endpoint": _route_exists(main_text, "get", "/todos/{todo_id}"),
        "update_todo_endpoint": _route_exists(main_text, "patch", "/todos/{todo_id}"),
        "delete_todo_endpoint": _route_exists(main_text, "delete", "/todos/{todo_id}"),
        "todo_models": all(name in models_text for name in ("TodoCreate", "TodoUpdate", "Todo")),
        "pytest_tests": "def test_" in tests_text,
        "crud_tests": all(
            needle in tests_text
            for needle in (
                "client.get('/health')",
                "client.get('/todos')",
                "client.post('/todos'",
                "client.patch(",
                "client.delete(",
            )
        ),
        "readme_quickstart": _readme_has_quickstart(readme_text),
        "sqlite_support": any(token in (main_text + models_text) for token in ("sqlite3", "sqlalchemy", "create_engine")),
        "sqlite_fallback_documented": "in-memory" in readme_text.lower() and "sqlite" in readme_text.lower(),
    }

    implemented: list[str] = []
    missing: list[str] = []
    notes: list[str] = []

    if checks["fastapi_app"]:
        implemented.append("FastAPI app entrypoint")
    else:
        missing.append("FastAPI app entrypoint")

    if checks["todo_models"]:
        implemented.append("Todo models")
    else:
        missing.append("Todo models")

    crud_keys = (
        "health_endpoint",
        "list_todos_endpoint",
        "create_todo_endpoint",
        "get_todo_endpoint",
        "update_todo_endpoint",
        "delete_todo_endpoint",
    )
    if all(checks[key] for key in crud_keys):
        implemented.append("CRUD endpoints")
    else:
        missing.append("CRUD endpoints")

    if checks["crud_tests"]:
        implemented.append("CRUD pytest coverage")
    elif checks["pytest_tests"]:
        implemented.append("pytest tests")
        missing.append("CRUD pytest coverage")
    else:
        missing.append("pytest tests")

    if checks["readme_quickstart"]:
        implemented.append("README quickstart")
    else:
        missing.append("README quickstart")

    if checks["sqlite_support"]:
        implemented.append("SQLite persistence")
    elif checks["sqlite_fallback_documented"]:
        notes.append(_SQLITE_FALLBACK_NOTE)

    return {
        "project_type": "fastapi_service",
        "status": _status_from(implemented, missing),
        "implemented": implemented,
        "missing": missing,
        "notes": notes,
        "checks": checks,
    }


def _inspect_python_cli(root: Path) -> dict:
    package_name = root.name
    cli_text = _read_text(root, f"src/{package_name}/cli.py")
    main_text = _read_text(root, f"src/{package_name}/__main__.py")
    tests_text = _read_text(root, "tests/test_cli.py")
    readme_text = _read_text(root, "README.md")

    checks = {
        "cli_entrypoint": "def main(" in cli_text and "argparse.ArgumentParser" in cli_text,
        "main_module": "raise SystemExit(main())" in main_text,
        "pytest_tests": "def test_" in tests_text,
        "readme_quickstart": _readme_has_quickstart(readme_text),
    }
    implemented: list[str] = []
    missing: list[str] = []
    if checks["cli_entrypoint"] and checks["main_module"]:
        implemented.append("CLI entrypoint")
    else:
        missing.append("CLI entrypoint")
    if checks["pytest_tests"]:
        implemented.append("pytest tests")
    else:
        missing.append("pytest tests")
    if checks["readme_quickstart"]:
        implemented.append("README quickstart")
    else:
        missing.append("README quickstart")
    return {
        "project_type": "python_cli",
        "status": _status_from(implemented, missing),
        "implemented": implemented,
        "missing": missing,
        "notes": [],
        "checks": checks,
    }


def _inspect_python_package(root: Path) -> dict:
    package_name = root.name
    init_text = _read_text(root, f"src/{package_name}/__init__.py")
    tests_text = _read_text(root, "tests/test_package.py")
    readme_text = _read_text(root, "README.md")

    checks = {
        "package_api": "def " in init_text,
        "slugify_function": "def slugify(" in init_text,
        "pytest_tests": "def test_" in tests_text,
        "readme_quickstart": _readme_has_quickstart(readme_text),
    }
    implemented: list[str] = []
    missing: list[str] = []
    if checks["package_api"]:
        implemented.append("package API")
    else:
        missing.append("package API")
    if checks["slugify_function"]:
        implemented.append("slugify utility")
    if checks["pytest_tests"]:
        implemented.append("pytest tests")
    else:
        missing.append("pytest tests")
    if checks["readme_quickstart"]:
        implemented.append("README quickstart")
    else:
        missing.append("README quickstart")
    return {
        "project_type": "python_package",
        "status": _status_from(implemented, missing),
        "implemented": implemented,
        "missing": missing,
        "notes": [],
        "checks": checks,
    }


def _inspect_generic(root: Path) -> dict:
    app_text = _read_text(root, "app.py")
    tests_text = _read_text(root, "tests/test_smoke.py")
    readme_text = _read_text(root, "README.md")
    checks = {
        "app_entrypoint": bool(app_text),
        "pytest_tests": "def test_" in tests_text,
        "readme_quickstart": _readme_has_quickstart(readme_text),
    }
    implemented: list[str] = []
    missing: list[str] = []
    if checks["app_entrypoint"]:
        implemented.append("app entrypoint")
    else:
        missing.append("app entrypoint")
    if checks["pytest_tests"]:
        implemented.append("pytest tests")
    else:
        missing.append("pytest tests")
    if checks["readme_quickstart"]:
        implemented.append("README quickstart")
    else:
        missing.append("README quickstart")
    return {
        "project_type": "generic_python",
        "status": _status_from(implemented, missing),
        "implemented": implemented,
        "missing": missing,
        "notes": [],
        "checks": checks,
    }


def inspect_generated_project(project_dir: str, project_type: str = "") -> dict:
    root = _project_root(project_dir)
    if not root.exists() or not root.is_dir():
        return {
            "project_type": project_type or "unknown",
            "status": "missing",
            "implemented": [],
            "missing": ["project directory"],
            "notes": ["Project directory does not exist yet."],
            "checks": {},
        }

    detected_type = project_type or _infer_project_type(root)
    if detected_type == "fastapi_service":
        payload = _inspect_fastapi(root)
    elif detected_type == "python_cli":
        payload = _inspect_python_cli(root)
    elif detected_type == "python_package":
        payload = _inspect_python_package(root)
    else:
        payload = _inspect_generic(root)

    payload["project_dir"] = root.as_posix()
    return payload


def inspect_generated_project_tool(project_dir: str, project_type: str = "") -> str:
    try:
        payload = inspect_generated_project(project_dir, project_type)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="inspect_generated_project",
    description=(
        "Inspect a generated project directory and report concrete implementation signals such as endpoints, tests, "
        "README quickstart coverage, and documented fallbacks."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_dir": {"type": "string", "description": "Project directory relative to the workspace."},
            "project_type": {"type": "string", "description": "Optional expected project type such as fastapi_service."},
        },
        "required": ["project_dir"],
    },
    handler=inspect_generated_project_tool,
)
