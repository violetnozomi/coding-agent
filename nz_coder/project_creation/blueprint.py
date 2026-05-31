"""Project blueprint generation for Greenfield project mode."""
from __future__ import annotations

import json

from nz_coder.tools import register


_SQLITE_FALLBACK_NOTE = (
    "SQLite was requested, but the default fastapi_service blueprint falls back "
    "to in-memory storage for demo stability."
)


def _file(path: str, purpose: str) -> dict:
    return {"path": path, "purpose": purpose}


def create_project_blueprint(project_spec: dict) -> dict:
    """Generate a stable file plan and verification flow for a project spec."""
    spec = dict(project_spec or {})
    project_name = str(spec.get("project_name") or "generated_project")
    project_type = str(spec.get("project_type") or "generic_python")
    features = [str(item) for item in spec.get("features", [])]
    root_dir = project_name
    notes: list[str] = list(spec.get("notes", []))

    if project_type == "fastapi_service":
        crud_requested = "CRUD operations" in features
        files = [
            _file(f"{root_dir}/app/__init__.py", "Python package marker"),
            _file(f"{root_dir}/app/models.py", "Todo request and response models"),
            _file(
                f"{root_dir}/app/main.py",
                "FastAPI app entrypoint with in-memory CRUD routes" if crud_requested else "FastAPI app entrypoint",
            ),
            _file(
                f"{root_dir}/tests/test_api.py",
                "CRUD API tests" if crud_requested else "API smoke and behavior tests",
            ),
            _file(f"{root_dir}/requirements.txt", "runtime and test dependencies"),
            _file(f"{root_dir}/README.md", "quickstart and run instructions"),
        ]
        verification_commands = [
            "python -m py_compile app/main.py",
            "pytest",
            "uvicorn app.main:app --help",
        ]
        demo_commands = [
            "uvicorn app.main:app --reload",
            "curl http://localhost:8000/health",
        ]
        if crud_requested:
            notes.append("Blueprint expects CRUD coverage in tests/test_api.py for list/create/get/update/delete todo flows.")
        if "SQLite persistence" in features and _SQLITE_FALLBACK_NOTE not in notes:
            notes.append(_SQLITE_FALLBACK_NOTE)
    elif project_type == "python_cli":
        files = [
            _file(f"{root_dir}/src/{project_name}/__init__.py", "package marker"),
            _file(f"{root_dir}/src/{project_name}/cli.py", "CLI implementation"),
            _file(f"{root_dir}/src/{project_name}/__main__.py", "python -m entrypoint"),
            _file(f"{root_dir}/tests/test_cli.py", "CLI tests"),
            _file(f"{root_dir}/pyproject.toml", "build metadata"),
            _file(f"{root_dir}/README.md", "quickstart and usage"),
        ]
        verification_commands = [
            f"python -m py_compile src/{project_name}/cli.py",
            "pytest",
        ]
        demo_commands = [f"PYTHONPATH=src python -m {project_name} --help"]
    elif project_type == "python_package":
        files = [
            _file(f"{root_dir}/src/{project_name}/__init__.py", "public package API"),
            _file(f"{root_dir}/tests/test_package.py", "package tests"),
            _file(f"{root_dir}/pyproject.toml", "build metadata"),
            _file(f"{root_dir}/README.md", "package usage guide"),
        ]
        verification_commands = [
            f"python -m py_compile src/{project_name}/__init__.py",
            "pytest",
        ]
        demo_commands = [
            f"PYTHONPATH=src python -c \"from {project_name} import slugify; print(slugify('Hello World'))\""
        ]
    elif project_type == "rag_demo":
        files = [
            _file(f"{root_dir}/app.py", "RAG demo entrypoint"),
            _file(f"{root_dir}/data/sample_docs.txt", "small local corpus"),
            _file(f"{root_dir}/tests/test_rag_demo.py", "demo tests"),
            _file(f"{root_dir}/requirements.txt", "dependencies"),
            _file(f"{root_dir}/README.md", "quickstart"),
        ]
        verification_commands = ["python -m py_compile app.py", "pytest"]
        demo_commands = ["python app.py --demo"]
    elif project_type == "agent_demo":
        files = [
            _file(f"{root_dir}/app.py", "agent demo entrypoint"),
            _file(f"{root_dir}/tests/test_agent_demo.py", "demo tests"),
            _file(f"{root_dir}/requirements.txt", "dependencies"),
            _file(f"{root_dir}/README.md", "quickstart"),
        ]
        verification_commands = ["python -m py_compile app.py", "pytest"]
        demo_commands = ["python app.py --demo"]
    else:
        files = [
            _file(f"{root_dir}/app.py", "project entrypoint"),
            _file(f"{root_dir}/tests/test_smoke.py", "smoke tests"),
            _file(f"{root_dir}/README.md", "quickstart"),
        ]
        verification_commands = ["python -m py_compile app.py", "pytest"]
        demo_commands = ["python app.py"]

    return {
        "project_name": project_name,
        "project_type": project_type,
        "root_dir": root_dir,
        "files": files,
        "milestones": [
            "create scaffold",
            "implement core app",
            "add tests",
            "run verification",
            "write README",
        ],
        "verification_commands": verification_commands,
        "demo_commands": demo_commands,
        "notes": notes,
    }


def create_project_blueprint_tool(project_spec: dict) -> str:
    try:
        payload = create_project_blueprint(project_spec)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="create_project_blueprint",
    description=(
        "Generate a file blueprint, milestones, verification commands, and notes for a new project spec."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_spec": {"type": "object", "description": "Structured project specification."},
        },
        "required": ["project_spec"],
    },
    handler=create_project_blueprint_tool,
)
