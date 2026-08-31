"""Tests for project scaffolding."""
from __future__ import annotations


def test_scaffold_project_creates_fastapi_demo(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.project_creation.templates import scaffold_project

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        result = scaffold_project("todo_api", "fastapi_service")
        assert result.startswith("Scaffold created:")
        main_py = tmp_path / "todo_api" / "app" / "main.py"
        test_py = tmp_path / "todo_api" / "tests" / "test_api.py"
        readme = tmp_path / "todo_api" / "README.md"
        assert main_py.exists()
        assert test_py.exists()
        assert readme.exists()
        content = main_py.read_text(encoding="utf-8")
        assert "@app.post('/todos'" in content
        assert "@app.get('/todos/{todo_id}'" in content
        assert "@app.patch('/todos/{todo_id}'" in content
        assert "@app.delete('/todos/{todo_id}'" in content
        assert "TodoCreate" in content
        assert "TodoUpdate" in content
        assert "reset_demo_state" in content
        readme_text = readme.read_text(encoding="utf-8")
        assert "in-memory storage" in readme_text
        assert "Swagger UI" in readme_text
        assert "Python 3.10+" in readme_text
        assert "test_delete_todo" in test_py.read_text(encoding="utf-8")
        again = scaffold_project("todo_api", "fastapi_service")
        assert again.startswith("Error:")
    finally:
        config.WORKDIR = old
