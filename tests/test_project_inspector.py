"""Tests for generated-project inspection."""
from __future__ import annotations


def test_inspect_generated_project_reports_fastapi_crud(tmp_path):
    from nz_coder import config
    from nz_coder.project_creation.templates import scaffold_project
    from nz_coder.project_creation.inspector import inspect_generated_project

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        scaffold_project("todo_api", "fastapi_service")
        report = inspect_generated_project("todo_api", "fastapi_service")
        assert report["status"] == "ok"
        assert "CRUD endpoints" in report["implemented"]
        assert report["checks"]["crud_tests"] is True
        assert report["checks"]["sqlite_support"] is False
        assert report["checks"]["sqlite_fallback_documented"] is True
    finally:
        config.WORKDIR = old
