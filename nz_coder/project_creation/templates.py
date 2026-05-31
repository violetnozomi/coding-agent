"""Project templates and safe scaffolding for Greenfield mode."""
from __future__ import annotations

from pathlib import Path

from nz_coder.tools import register
from nz_coder.tools.files import _safe_path, _write_files_batch_impl


class TemplateRegistry:
    """Registry for stable project skeleton templates."""

    @staticmethod
    def build(project_name: str, project_type: str) -> dict:
        builder = {
            "fastapi_service": _fastapi_template,
            "python_cli": _python_cli_template,
            "python_package": _python_package_template,
            "rag_demo": _rag_demo_template,
            "agent_demo": _agent_demo_template,
        }.get(project_type, _generic_python_template)
        return builder(project_name)


def _readme(project_name: str, body: str) -> str:
    return f"# {project_name}\n\n{body}\n"


def _fastapi_template(project_name: str) -> dict:
    return {
        "directories": ["app", "tests"],
        "files": [
            {"path": "app/__init__.py", "purpose": "package marker", "content": ""},
            {
                "path": "app/models.py",
                "purpose": "Todo request and response models",
                "content": (
                    "from __future__ import annotations\n\n"
                    "from pydantic import BaseModel, Field\n\n"
                    "class TodoCreate(BaseModel):\n"
                    "    title: str = Field(..., min_length=1)\n"
                    "    done: bool = False\n\n"
                    "class TodoUpdate(BaseModel):\n"
                    "    title: str | None = Field(default=None, min_length=1)\n"
                    "    done: bool | None = None\n\n"
                    "class Todo(BaseModel):\n"
                    "    id: int\n"
                    "    title: str\n"
                    "    done: bool = False\n"
                ),
            },
            {
                "path": "app/main.py",
                "purpose": "FastAPI entrypoint with in-memory CRUD routes",
                "content": (
                    "from __future__ import annotations\n\n"
                    "from fastapi import FastAPI, HTTPException, Response, status\n\n"
                    "from .models import Todo, TodoCreate, TodoUpdate\n\n"
                    f"app = FastAPI(title={project_name!r})\n\n"
                    "_TODOS: dict[int, Todo] = {}\n"
                    "_NEXT_ID = 1\n\n"
                    "def reset_demo_state() -> None:\n"
                    "    global _NEXT_ID\n"
                    "    _TODOS.clear()\n"
                    "    _TODOS[1] = Todo(id=1, title='demo task', done=False)\n"
                    "    _NEXT_ID = 2\n\n"
                    "def _model_dump(model: Todo | TodoCreate | TodoUpdate, *, exclude_unset: bool = False) -> dict:\n"
                    "    if hasattr(model, 'model_dump'):\n"
                    "        return model.model_dump(exclude_unset=exclude_unset)\n"
                    "    return model.dict(exclude_unset=exclude_unset)\n\n"
                    "def _get_todo_or_404(todo_id: int) -> Todo:\n"
                    "    todo = _TODOS.get(todo_id)\n"
                    "    if todo is None:\n"
                    "        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Todo not found')\n"
                    "    return todo\n\n"
                    "reset_demo_state()\n\n"
                    "@app.get('/health')\n"
                    "def health() -> dict[str, str]:\n"
                    "    return {'status': 'ok'}\n\n"
                    "@app.get('/todos', response_model=list[Todo])\n"
                    "def list_todos() -> list[Todo]:\n"
                    "    return list(_TODOS.values())\n\n"
                    "@app.post('/todos', response_model=Todo, status_code=status.HTTP_201_CREATED)\n"
                    "def create_todo(payload: TodoCreate) -> Todo:\n"
                    "    global _NEXT_ID\n"
                    "    todo = Todo(id=_NEXT_ID, title=payload.title, done=payload.done)\n"
                    "    _TODOS[todo.id] = todo\n"
                    "    _NEXT_ID += 1\n"
                    "    return todo\n\n"
                    "@app.get('/todos/{todo_id}', response_model=Todo)\n"
                    "def get_todo(todo_id: int) -> Todo:\n"
                    "    return _get_todo_or_404(todo_id)\n\n"
                    "@app.patch('/todos/{todo_id}', response_model=Todo)\n"
                    "def update_todo(todo_id: int, payload: TodoUpdate) -> Todo:\n"
                    "    current = _get_todo_or_404(todo_id)\n"
                    "    data = _model_dump(current)\n"
                    "    updates = _model_dump(payload, exclude_unset=True)\n"
                    "    for key, value in updates.items():\n"
                    "        data[key] = value\n"
                    "    updated = Todo(**data)\n"
                    "    _TODOS[todo_id] = updated\n"
                    "    return updated\n\n"
                    "@app.delete('/todos/{todo_id}', status_code=status.HTTP_204_NO_CONTENT)\n"
                    "def delete_todo(todo_id: int) -> Response:\n"
                    "    _get_todo_or_404(todo_id)\n"
                    "    del _TODOS[todo_id]\n"
                    "    return Response(status_code=status.HTTP_204_NO_CONTENT)\n"
                ),
            },
            {
                "path": "tests/test_api.py",
                "purpose": "CRUD API tests",
                "content": (
                    "from fastapi.testclient import TestClient\n\n"
                    "from app.main import app, reset_demo_state\n\n"
                    "def _client() -> TestClient:\n"
                    "    reset_demo_state()\n"
                    "    return TestClient(app)\n\n"
                    "def test_health() -> None:\n"
                    "    client = _client()\n"
                    "    response = client.get('/health')\n"
                    "    assert response.status_code == 200\n"
                    "    assert response.json() == {'status': 'ok'}\n\n"
                    "def test_list_todos() -> None:\n"
                    "    client = _client()\n"
                    "    response = client.get('/todos')\n"
                    "    assert response.status_code == 200\n"
                    "    assert len(response.json()) == 1\n\n"
                    "def test_create_todo() -> None:\n"
                    "    client = _client()\n"
                    "    response = client.post('/todos', json={'title': 'ship demo', 'done': False})\n"
                    "    assert response.status_code == 201\n"
                    "    body = response.json()\n"
                    "    assert body['id'] == 2\n"
                    "    assert body['title'] == 'ship demo'\n"
                    "    assert body['done'] is False\n\n"
                    "def test_get_todo() -> None:\n"
                    "    client = _client()\n"
                    "    created = client.post('/todos', json={'title': 'fetch me'})\n"
                    "    todo_id = created.json()['id']\n"
                    "    response = client.get(f'/todos/{todo_id}')\n"
                    "    assert response.status_code == 200\n"
                    "    assert response.json()['title'] == 'fetch me'\n\n"
                    "def test_update_todo() -> None:\n"
                    "    client = _client()\n"
                    "    created = client.post('/todos', json={'title': 'old title', 'done': False})\n"
                    "    todo_id = created.json()['id']\n"
                    "    response = client.patch(f'/todos/{todo_id}', json={'title': 'new title', 'done': True})\n"
                    "    assert response.status_code == 200\n"
                    "    assert response.json() == {'id': todo_id, 'title': 'new title', 'done': True}\n\n"
                    "def test_delete_todo() -> None:\n"
                    "    client = _client()\n"
                    "    created = client.post('/todos', json={'title': 'remove me'})\n"
                    "    todo_id = created.json()['id']\n"
                    "    response = client.delete(f'/todos/{todo_id}')\n"
                    "    assert response.status_code == 204\n"
                    "    missing = client.get(f'/todos/{todo_id}')\n"
                    "    assert missing.status_code == 404\n"
                ),
            },
            {
                "path": "requirements.txt",
                "purpose": "dependencies",
                "content": "fastapi\nuvicorn\npytest\nhttpx\npydantic\n",
            },
            {
                "path": "README.md",
                "purpose": "quickstart",
                "content": _readme(
                    project_name,
                    "A small FastAPI Todo API demo. The default scaffold uses in-memory storage for stability and does not persist data across restarts. SQLite requests are intentionally treated as a follow-up customization.\n\nUse Python 3.10+ for this scaffold.\n\n## Quickstart\n\n```bash\npip install -r requirements.txt\nuvicorn app.main:app --reload\npytest\n```\n\nOpen http://localhost:8000/docs for Swagger UI.\n\n## API\n\n- `GET /health`\n- `GET /todos`\n- `POST /todos`\n- `GET /todos/{todo_id}`\n- `PATCH /todos/{todo_id}`\n- `DELETE /todos/{todo_id}`",
                ),
            },
        ],
    }


def _python_cli_template(project_name: str) -> dict:
    return {
        "directories": [f"src/{project_name}", "tests"],
        "files": [
            {
                "path": f"src/{project_name}/__init__.py",
                "purpose": "package marker",
                "content": "__all__ = ['main']\n",
            },
            {
                "path": f"src/{project_name}/cli.py",
                "purpose": "CLI implementation",
                "content": (
                    "import argparse\n\n"
                    "def build_parser() -> argparse.ArgumentParser:\n"
                    f"    parser = argparse.ArgumentParser(prog={project_name!r})\n"
                    "    parser.add_argument('text', nargs='*', help='Text to echo.')\n"
                    "    return parser\n\n"
                    "def main(argv: list[str] | None = None) -> int:\n"
                    "    parser = build_parser()\n"
                    "    args = parser.parse_args(argv)\n"
                    "    print(' '.join(args.text))\n"
                    "    return 0\n"
                ),
            },
            {
                "path": f"src/{project_name}/__main__.py",
                "purpose": "python -m entrypoint",
                "content": (
                    "from .cli import main\n\n"
                    "if __name__ == '__main__':\n"
                    "    raise SystemExit(main())\n"
                ),
            },
            {
                "path": "tests/test_cli.py",
                "purpose": "CLI tests",
                "content": (
                    f"from {project_name}.cli import main\n\n"
                    "def test_main_prints_text(capsys) -> None:\n"
                    "    assert main(['hello', 'world']) == 0\n"
                    "    captured = capsys.readouterr()\n"
                    "    assert captured.out.strip() == 'hello world'\n"
                ),
            },
            {
                "path": "pyproject.toml",
                "purpose": "build metadata",
                "content": (
                    "[build-system]\n"
                    "requires = ['setuptools>=68', 'wheel']\n"
                    "build-backend = 'setuptools.build_meta'\n\n"
                    "[project]\n"
                    f"name = {project_name!r}\n"
                    "version = '0.1.0'\n"
                    "requires-python = '>=3.9'\n\n"
                    "[tool.pytest.ini_options]\n"
                    "pythonpath = ['src']\n"
                ),
            },
            {
                "path": "README.md",
                "purpose": "quickstart",
                "content": _readme(
                    project_name,
                    f"A small Python CLI skeleton.\n\n## Quickstart\n\n```bash\nPYTHONPATH=src python -m {project_name} --help\npytest\n```",
                ),
            },
        ],
    }


def _python_package_template(project_name: str) -> dict:
    return {
        "directories": [f"src/{project_name}", "tests"],
        "files": [
            {
                "path": f"src/{project_name}/__init__.py",
                "purpose": "public package API",
                "content": (
                    "import re\n\n"
                    "def slugify(text: str) -> str:\n"
                    "    lowered = text.strip().lower()\n"
                    "    cleaned = re.sub(r'[^a-z0-9]+', '-', lowered).strip('-')\n"
                    "    return cleaned\n\n"
                    "__all__ = ['slugify']\n"
                ),
            },
            {
                "path": "tests/test_package.py",
                "purpose": "package tests",
                "content": (
                    f"from {project_name} import slugify\n\n"
                    "def test_slugify() -> None:\n"
                    "    assert slugify('Hello, World!') == 'hello-world'\n"
                ),
            },
            {
                "path": "pyproject.toml",
                "purpose": "build metadata",
                "content": (
                    "[build-system]\n"
                    "requires = ['setuptools>=68', 'wheel']\n"
                    "build-backend = 'setuptools.build_meta'\n\n"
                    "[project]\n"
                    f"name = {project_name!r}\n"
                    "version = '0.1.0'\n"
                    "requires-python = '>=3.9'\n\n"
                    "[tool.pytest.ini_options]\n"
                    "pythonpath = ['src']\n"
                ),
            },
            {
                "path": "README.md",
                "purpose": "quickstart",
                "content": _readme(
                    project_name,
                    (
                        "A small Python package skeleton.\n\n## Quickstart\n\n```bash\n"
                        f"PYTHONPATH=src python -c \"from {project_name} import slugify; print(slugify('Hello World'))\"\n"
                        "pytest\n```"
                    ),
                ),
            },
        ],
    }


def _rag_demo_template(project_name: str) -> dict:
    return {
        "directories": ["data", "tests"],
        "files": [
            {
                "path": "data/sample_docs.txt",
                "purpose": "local corpus",
                "content": "alpha: local retrieval example\nbeta: another tiny document\n",
            },
            {
                "path": "app.py",
                "purpose": "RAG demo entrypoint",
                "content": (
                    "from pathlib import Path\n\n"
                    "def search(query: str) -> list[str]:\n"
                    "    docs = Path('data/sample_docs.txt').read_text(encoding='utf-8').splitlines()\n"
                    "    return [line for line in docs if query.lower() in line.lower()]\n\n"
                    "if __name__ == '__main__':\n"
                    "    print(search('alpha'))\n"
                ),
            },
            {
                "path": "tests/test_rag_demo.py",
                "purpose": "demo tests",
                "content": (
                    "from app import search\n\n"
                    "def test_search() -> None:\n"
                    "    assert search('alpha')\n"
                ),
            },
            {
                "path": "requirements.txt",
                "purpose": "dependencies",
                "content": "pytest\n",
            },
            {
                "path": "README.md",
                "purpose": "quickstart",
                "content": _readme(
                    project_name,
                    "A tiny local RAG-style demo.\n\n## Quickstart\n\n```bash\npython app.py\npytest\n```",
                ),
            },
        ],
    }


def _agent_demo_template(project_name: str) -> dict:
    return {
        "directories": ["tests"],
        "files": [
            {
                "path": "app.py",
                "purpose": "agent demo entrypoint",
                "content": (
                    "def choose_action(text: str) -> str:\n"
                    "    lowered = text.lower()\n"
                    "    if 'search' in lowered:\n"
                    "        return 'search'\n"
                    "    if 'write' in lowered:\n"
                    "        return 'write'\n"
                    "    return 'reply'\n\n"
                    "if __name__ == '__main__':\n"
                    "    print(choose_action('search docs'))\n"
                ),
            },
            {
                "path": "tests/test_agent_demo.py",
                "purpose": "demo tests",
                "content": (
                    "from app import choose_action\n\n"
                    "def test_choose_action() -> None:\n"
                    "    assert choose_action('search docs') == 'search'\n"
                ),
            },
            {
                "path": "requirements.txt",
                "purpose": "dependencies",
                "content": "pytest\n",
            },
            {
                "path": "README.md",
                "purpose": "quickstart",
                "content": _readme(
                    project_name,
                    "A tiny local agent-style demo.\n\n## Quickstart\n\n```bash\npython app.py\npytest\n```",
                ),
            },
        ],
    }


def _generic_python_template(project_name: str) -> dict:
    return {
        "directories": ["tests"],
        "files": [
            {
                "path": "app.py",
                "purpose": "project entrypoint",
                "content": (
                    "def main() -> int:\n"
                    "    return 0\n\n"
                    "if __name__ == '__main__':\n"
                    "    raise SystemExit(main())\n"
                ),
            },
            {
                "path": "tests/test_smoke.py",
                "purpose": "smoke tests",
                "content": (
                    "from app import main\n\n"
                    "def test_main() -> None:\n"
                    "    assert main() == 0\n"
                ),
            },
            {
                "path": "README.md",
                "purpose": "quickstart",
                "content": _readme(
                    project_name,
                    "A small Python project skeleton.\n\n## Quickstart\n\n```bash\npython app.py\npytest\n```",
                ),
            },
        ],
    }


def scaffold_project(project_name: str, project_type: str, target_dir: str = ".", overwrite: bool = False) -> str:
    """Create a stable project skeleton inside the current workspace."""
    created_dirs: list[str] = []
    try:
        project_name = str(project_name or "").strip()
        project_type = str(project_type or "generic_python").strip() or "generic_python"
        if not project_name:
            return "Error: project_name is required"

        target_base = _safe_path(target_dir or ".")
        if not target_base.exists() or not target_base.is_dir():
            return f"Error: target_dir is not a directory: {target_dir}"

        project_rel = (Path(target_dir) / project_name).as_posix()
        project_root = _safe_path(project_rel)
        if project_root.exists() and any(project_root.iterdir()) and not overwrite:
            return f"Error: project directory already exists and is not empty: {project_rel}"

        template = TemplateRegistry.build(project_name, project_type)
        if not project_root.exists():
            project_root.mkdir(parents=True, exist_ok=True)
            created_dirs.append(project_rel)

        for directory in template.get("directories", []):
            rel_dir = (Path(project_rel) / directory).as_posix()
            abs_dir = _safe_path(rel_dir)
            if not abs_dir.exists():
                abs_dir.mkdir(parents=True, exist_ok=True)
                created_dirs.append(rel_dir)

        batch_files = []
        for item in template.get("files", []):
            rel_path = (Path(project_rel) / item["path"]).as_posix()
            batch_files.append({
                "path": rel_path,
                "content": str(item.get("content", "")),
                "purpose": str(item.get("purpose", "")),
            })

        result = _write_files_batch_impl(batch_files, overwrite=overwrite)
        created_files = result.get("created", []) + result.get("updated", [])
        lines = [
            f"Scaffold created: {project_rel}",
            f"Project type: {project_type}",
            f"Directories created: {len(created_dirs)}",
            f"Files created: {len(created_files)}",
        ]
        lines.extend(f"- {path}" for path in created_files[:20])
        lines.extend([
            "Next steps:",
            "- Use create_project_blueprint or write_files_batch to fill project-specific business logic.",
            "- Run plan_project_acceptance and verify_project_build before finalizing.",
        ])
        return "\n".join(lines)
    except Exception as exc:
        for rel_dir in reversed(created_dirs):
            try:
                abs_dir = _safe_path(rel_dir)
                if abs_dir.exists() and abs_dir.is_dir() and not any(abs_dir.iterdir()):
                    abs_dir.rmdir()
            except Exception:
                pass
        return f"Error: {exc}"


register(
    name="scaffold_project",
    description=(
        "Create a stable project skeleton from a built-in template. "
        "Returns created files and next-step guidance."
    ),
    parameters={
        "type": "object",
        "properties": {
            "project_name": {"type": "string", "description": "Project directory name to create."},
            "project_type": {"type": "string", "description": "Template type, e.g. fastapi_service or python_cli."},
            "target_dir": {"type": "string", "description": "Base directory relative to workspace. Default: ."},
            "overwrite": {"type": "boolean", "description": "Allow overwriting existing files. Default: false."},
        },
        "required": ["project_name", "project_type"],
    },
    handler=scaffold_project,
)
