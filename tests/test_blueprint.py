"""Tests for project blueprint generation."""


def test_blueprint_fastapi_contains_expected_files_and_notes():
    from nz_coder.project_creation.blueprint import create_project_blueprint

    blueprint = create_project_blueprint({
        "project_name": "todo_api",
        "project_type": "fastapi_service",
        "features": ["CRUD operations", "SQLite persistence"],
    })
    paths = {item["path"] for item in blueprint["files"]}
    assert "todo_api/app/main.py" in paths
    assert "todo_api/tests/test_api.py" in paths
    assert "todo_api/requirements.txt" in paths
    assert blueprint["verification_commands"]
    assert blueprint["demo_commands"]
    assert any("CRUD" in note for note in blueprint["notes"])
    assert any("in-memory storage" in note for note in blueprint["notes"])
