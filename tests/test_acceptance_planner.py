"""Tests for project acceptance planning."""


def test_acceptance_planner_generates_fastapi_commands():
    from nz_coder.project_creation.acceptance_planner import plan_project_acceptance
    from nz_coder.project_creation.blueprint import create_project_blueprint

    spec = {
        "project_name": "todo_api",
        "project_type": "fastapi_service",
        "features": ["CRUD operations", "SQLite persistence"],
    }
    blueprint = create_project_blueprint(spec)
    plan = plan_project_acceptance(spec, blueprint)
    assert any(cmd == "pytest" for cmd in plan["verification_commands"])
    assert any("uvicorn" in cmd for cmd in plan["verification_commands"])
    assert any("health" in output.lower() for output in plan["expected_outputs"])
    assert any("crud" in output.lower() or "in-memory" in output.lower() for output in plan["expected_outputs"])
    assert any("sqlite" in output.lower() and "in-memory" in output.lower() for output in plan["expected_outputs"])
