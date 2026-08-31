"""Requirement analysis for Greenfield project creation requests."""
from __future__ import annotations

import json
import re

from nz_coder.tools import register


_SQLITE_FALLBACK_NOTE = (
    "SQLite requested but the default fastapi_service scaffold uses in-memory storage "
    "unless a sqlite-specific template is selected."
)


def _sanitize_project_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return text or "generated_project"


def _default_project_name(prompt: str, project_type: str) -> str:
    lowered = (prompt or "").lower()
    if "todo" in lowered:
        return "todo_api" if project_type == "fastapi_service" else "todo_project"
    if "word counter" in lowered or "wordcount" in lowered or "词频" in lowered:
        return "word_counter"
    if "slugify" in lowered:
        return "slugify"
    if project_type == "fastapi_service":
        return "fastapi_service"
    if project_type == "python_cli":
        return "python_cli_tool"
    if project_type == "python_package":
        return "python_package"
    if project_type == "rag_demo":
        return "rag_demo"
    if project_type == "agent_demo":
        return "agent_demo"
    return "generated_project"


def _extract_project_name(prompt: str, project_type: str) -> str:
    patterns = (
        r"named\s+([A-Za-z0-9_-]+)",
        r"名为\s*([A-Za-z0-9_\-一-龥]+)",
        r"叫\s*([A-Za-z0-9_\-一-龥]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, prompt, re.IGNORECASE)
        if match:
            return _sanitize_project_name(match.group(1))
    return _default_project_name(prompt, project_type)


def _detect_project_type(prompt: str) -> tuple[str, str, str]:
    lowered = (prompt or "").lower()
    if "fastapi" in lowered or "uvicorn" in lowered:
        return "fastapi_service", "python", "fastapi"
    if "cli" in lowered or "command line" in lowered or "命令行" in lowered:
        return "python_cli", "python", "argparse"
    if "package" in lowered or "library" in lowered or "sdk" in lowered or "库" in lowered:
        return "python_package", "python", "setuptools"
    if "rag" in lowered or "retrieval" in lowered:
        return "rag_demo", "python", "python"
    if "agent demo" in lowered or ("agent" in lowered and "demo" in lowered):
        return "agent_demo", "python", "python"
    return "generic_python", "python", "python"


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _extract_features(prompt: str, project_type: str) -> list[str]:
    lowered = (prompt or "").lower()
    features: list[str] = []
    if "crud" in lowered or "增删改查" in lowered:
        _append_unique(features, "CRUD operations")
    if "todo" in lowered:
        _append_unique(features, "Todo item management")
    if "sqlite" in lowered:
        _append_unique(features, "SQLite persistence")
    if "pytest" in lowered or "test" in lowered or "测试" in lowered:
        _append_unique(features, "pytest tests")
    if "readme" in lowered:
        _append_unique(features, "README quickstart")
    if "health" in lowered:
        _append_unique(features, "health endpoint")
    if "word counter" in lowered or "wordcount" in lowered:
        _append_unique(features, "word counting")
    if "slugify" in lowered:
        _append_unique(features, "slugify utility")
    if project_type == "fastapi_service":
        _append_unique(features, "REST API")
        _append_unique(features, "health endpoint")
    elif project_type == "python_cli":
        _append_unique(features, "CLI entrypoint")
    elif project_type == "python_package":
        _append_unique(features, "importable package API")
    elif project_type == "rag_demo":
        _append_unique(features, "local retrieval demo")
    elif project_type == "agent_demo":
        _append_unique(features, "local agent demo")
    _append_unique(features, "README quickstart")
    return features[:8]


def _entrypoints(project_name: str, project_type: str) -> list[str]:
    if project_type == "fastapi_service":
        return ["app/main.py"]
    if project_type == "python_cli":
        return [f"src/{project_name}/__main__.py"]
    if project_type == "python_package":
        return [f"src/{project_name}/__init__.py"]
    return ["app.py"]


def _notes(project_type: str, features: list[str]) -> list[str]:
    notes: list[str] = []
    if project_type == "fastapi_service" and "CRUD operations" in features:
        notes.append("Default FastAPI scaffold should expose full in-memory CRUD endpoints with CRUD-focused pytest coverage.")
    if project_type == "fastapi_service" and "SQLite persistence" in features:
        notes.append(_SQLITE_FALLBACK_NOTE)
    return notes


def _constraints(prompt: str, project_type: str, features: list[str]) -> list[str]:
    constraints = ["local only", "no external network dependency"]
    if "overwrite" in (prompt or "").lower() or "覆盖" in (prompt or ""):
        constraints.append("explicit overwrite confirmation required")
    if project_type == "fastapi_service" and "SQLite persistence" in features:
        constraints.append(_SQLITE_FALLBACK_NOTE)
    return constraints


def _acceptance(project_type: str, features: list[str], project_name: str) -> list[str]:
    criteria: list[str] = []
    if project_type == "fastapi_service":
        criteria.extend([
            "server starts with uvicorn",
            "GET /health returns ok",
            "pytest passes",
        ])
        if "CRUD operations" in features:
            criteria.insert(1, "Todo CRUD endpoints work with in-memory storage")
    elif project_type == "python_cli":
        criteria.extend([
            f"PYTHONPATH=src python -m {project_name} --help works",
            "pytest passes",
        ])
    elif project_type == "python_package":
        criteria.extend([
            "package imports cleanly",
            "pytest passes",
        ])
    else:
        criteria.extend([
            "py_compile passes",
            "pytest passes",
        ])
    return criteria[:5]


def analyze_project_requirements(prompt: str) -> dict:
    project_type, language, framework = _detect_project_type(prompt)
    project_name = _extract_project_name(prompt, project_type)
    features = _extract_features(prompt, project_type)
    notes = _notes(project_type, features)
    spec = {
        "project_name": project_name,
        "project_type": project_type,
        "language": language,
        "framework": framework,
        "features": features,
        "entrypoints": _entrypoints(project_name, project_type),
        "constraints": _constraints(prompt, project_type, features),
        "acceptance_criteria": _acceptance(project_type, features, project_name),
        "notes": notes,
    }
    return spec


def _format_spec(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2)


def analyze_project_requirements_tool(prompt: str) -> str:
    try:
        return _format_spec(analyze_project_requirements(prompt))
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="analyze_project_requirements",
    description=(
        "Parse a new-project request into a structured spec with project type, "
        "framework, features, entrypoints, constraints, and acceptance criteria."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Natural-language project request."},
        },
        "required": ["prompt"],
    },
    handler=analyze_project_requirements_tool,
    side_effect="readonly",
)
