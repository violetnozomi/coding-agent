"""Tests for project completeness checks."""
from __future__ import annotations


def test_project_completeness_reports_sqlite_gap_with_documented_fallback(tmp_path):
    from nz_coder import config
    from nz_coder.project_creation.requirement_analyzer import analyze_project_requirements
    from nz_coder.project_creation.blueprint import create_project_blueprint
    from nz_coder.project_creation.templates import scaffold_project
    from nz_coder.project_creation.completeness import check_project_completeness

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        prompt = "创建一个名为 todo_api 的 FastAPI Todo API 项目，支持 CRUD、SQLite、pytest 测试和 README"
        spec = analyze_project_requirements(prompt)
        blueprint = create_project_blueprint(spec)
        scaffold_project("todo_api", "fastapi_service")
        report = check_project_completeness(spec, blueprint, "todo_api")
        assert report["status"] == "partial"
        assert "SQLite persistence" in report["missing"]
        assert any("in-memory" in note.lower() for note in report["notes"])
        assert any("sqlite" in step.lower() for step in report["recommended_next_steps"])
    finally:
        config.WORKDIR = old
