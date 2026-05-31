"""Tests for Greenfield requirement analysis."""


def test_requirement_analyzer_detects_fastapi_sqlite_and_pytest():
    from nz_coder.project_creation.requirement_analyzer import analyze_project_requirements

    spec = analyze_project_requirements("创建一个 FastAPI Todo API，带 CRUD、SQLite 和 pytest")
    assert spec["project_type"] == "fastapi_service"
    assert spec["framework"] == "fastapi"
    assert any("SQLite" in item for item in spec["features"])
    assert any("pytest" in item.lower() for item in spec["features"])
    assert any("in-memory storage" in item for item in spec["constraints"])
    assert any("in-memory CRUD" in item for item in spec["notes"])
    assert not any("SQLite-backed behavior works" in item for item in spec["acceptance_criteria"])
