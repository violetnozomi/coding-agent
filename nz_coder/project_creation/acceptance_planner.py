"""Acceptance planning for Greenfield projects."""
from __future__ import annotations

import json

from nz_coder.tools import register


def plan_project_acceptance(project_spec: dict, blueprint: dict) -> dict:
    """Generate acceptance criteria, verification commands, and demo commands."""
    spec = dict(project_spec or {})
    plan = dict(blueprint or {})
    project_name = str(spec.get("project_name") or plan.get("project_name") or "generated_project")
    project_type = str(spec.get("project_type") or "generic_python")
    features = [str(item) for item in spec.get("features", [])]

    acceptance = list(spec.get("acceptance_criteria", []))
    verification = list(plan.get("verification_commands", []))
    demo_commands = list(plan.get("demo_commands", []))
    expected_outputs: list[str] = []

    if project_type == "fastapi_service":
        if not acceptance:
            acceptance = [
                "pytest passes",
                "health endpoint returns ok",
                "README contains quickstart",
            ]
        if not verification:
            verification = ["python -m py_compile app/main.py", "pytest", "uvicorn app.main:app --help"]
        if not demo_commands:
            demo_commands = ["uvicorn app.main:app --reload", "curl http://localhost:8000/health"]
        expected_outputs = ["pytest: all tests passed", 'GET /health returns {"status":"ok"}']
        if "CRUD operations" in features:
            expected_outputs.append("Todo CRUD endpoints work with in-memory storage")
        if "SQLite persistence" in features:
            expected_outputs.append(
                "SQLite persistence is recorded as a follow-up customization; default scaffold uses in-memory storage."
            )
    elif project_type == "python_cli":
        if not acceptance:
            acceptance = ["pytest passes", "CLI help renders", "README contains usage"]
        if not verification:
            verification = [f"python -m py_compile src/{project_name}/cli.py", "pytest"]
        if not demo_commands:
            demo_commands = [f"PYTHONPATH=src python -m {project_name} --help"]
        expected_outputs = ["pytest: all tests passed", "--help exits successfully"]
    elif project_type == "python_package":
        if not acceptance:
            acceptance = ["package imports cleanly", "pytest passes", "README contains examples"]
        if not verification:
            verification = [f"python -m py_compile src/{project_name}/__init__.py", "pytest"]
        if not demo_commands:
            demo_commands = [
                f"PYTHONPATH=src python -c \"from {project_name} import slugify; print(slugify('Hello World'))\""
            ]
        expected_outputs = ["pytest: all tests passed", "import works"]
    elif project_type in {"rag_demo", "agent_demo"}:
        if not acceptance:
            acceptance = ["py_compile passes", "pytest passes", "README contains quickstart"]
        if not verification:
            verification = ["python -m py_compile app.py", "pytest"]
        if not demo_commands:
            demo_commands = ["python app.py --demo"]
        expected_outputs = ["pytest: all tests passed", "demo command exits successfully"]
    else:
        if not acceptance:
            acceptance = ["py_compile passes", "pytest passes", "README contains quickstart"]
        if not verification:
            verification = ["python -m py_compile app.py", "pytest"]
        if not demo_commands:
            demo_commands = ["python app.py"]
        expected_outputs = ["pytest: all tests passed"]

    return {
        "acceptance_criteria": acceptance,
        "verification_commands": verification,
        "demo_commands": demo_commands,
        "expected_outputs": expected_outputs,
    }


def plan_project_acceptance_tool(project_spec: dict, blueprint: dict) -> str:
    try:
        payload = plan_project_acceptance(project_spec, blueprint)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="plan_project_acceptance",
    description=(
        "Generate acceptance criteria, verification commands, and demo commands for a new project plan."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_spec": {"type": "object", "description": "Structured project spec."},
            "blueprint": {"type": "object", "description": "Project blueprint."},
        },
        "required": ["project_spec", "blueprint"],
    },
    handler=plan_project_acceptance_tool,
    side_effect="readonly",
)
