"""Tests for structured RunEvidence collection."""
from __future__ import annotations

import json

from nz_coder.runtime.observability.run_evidence import RunEvidence


def test_child_outcome_keeps_lineage_and_applied_paths_separate():
    evidence = RunEvidence(run_id="run-child")
    child_metadata = {
        "child_session_id": "child-session",
        "child_agent_id": "child-agent",
        "child_parent_session_id": "parent-session",
        "child_trace_id": "trace-child",
        "child_status": "completed",
        "child_changed_files": ["src/worker.py"],
        "child_conflicts": [],
        "child_verification": "pytest tests/test_worker.py: passed",
    }

    evidence.record_tool_result("task", {}, "child done", metadata=child_metadata)

    assert evidence.modified_files == []
    assert evidence.child_outcomes == [{
        "session_id": "child-session",
        "agent_id": "child-agent",
        "parent_session_id": "parent-session",
        "trace_id": "trace-child",
        "status": "completed",
        "changed_files": ["src/worker.py"],
        "conflicts": [],
        "verification": "pytest tests/test_worker.py: passed",
    }]

    applied_metadata = dict(child_metadata, child_status="applied")
    evidence.record_tool_result(
        "apply_agent_changes",
        {"session_id": "child-session"},
        "applied",
        metadata=applied_metadata,
    )

    assert evidence.modified_files == ["src/worker.py"]
    assert evidence.actual_output_paths == ["src/worker.py"]
    assert evidence.child_outcomes[-1]["status"] == "applied"
    assert evidence.review_input()["child_outcomes"][-1]["trace_id"] == "trace-child"


def test_record_analyze_project_requirements_json_saves_project_spec():
    evidence = RunEvidence(run_id="run-1")
    payload = {
        "project_name": "todo_api",
        "project_type": "fastapi_service",
        "notes": [
            "SQLite requested but the default fastapi_service scaffold uses in-memory storage unless a sqlite-specific template is selected."
        ],
        "constraints": ["local only"],
    }

    evidence.record_tool_result("analyze_project_requirements", {"prompt": "todo"}, json.dumps(payload), success=True)

    assert evidence.project_spec["project_name"] == "todo_api"
    assert any("SQLite requested" in item for item in evidence.limitations)
    assert "local only" in evidence.notes


def test_record_create_project_blueprint_extracts_expected_files():
    evidence = RunEvidence(run_id="run-2")
    payload = {
        "files": [
            {"path": "todo_api/app/main.py", "purpose": "app"},
            {"path": "todo_api/tests/test_api.py", "purpose": "tests"},
        ],
        "notes": ["Blueprint expects CRUD coverage."],
    }

    evidence.record_tool_result("create_project_blueprint", {"project_spec": {}}, json.dumps(payload), success=True)

    assert evidence.blueprint["files"][0]["path"] == "todo_api/app/main.py"
    assert evidence.expected_files == ["todo_api/app/main.py", "todo_api/tests/test_api.py"]
    assert "Blueprint expects CRUD coverage." in evidence.notes


def test_record_scaffold_project_extracts_created_files():
    evidence = RunEvidence(run_id="run-3")
    output = "\n".join([
        "Scaffold created: todo_api",
        "Project type: fastapi_service",
        "Directories created: 3",
        "Files created: 2",
        "- todo_api/app/main.py",
        "- todo_api/tests/test_api.py",
        "Next steps:",
        "- Use create_project_blueprint or write_files_batch to fill project-specific business logic.",
    ])

    evidence.record_tool_result("scaffold_project", {"project_name": "todo_api"}, output, success=True)

    assert evidence.created_files == ["todo_api/app/main.py", "todo_api/tests/test_api.py"]
    assert evidence.actual_output_paths == ["todo_api/app/main.py", "todo_api/tests/test_api.py"]


def test_record_verify_project_build_parses_statuses():
    evidence = RunEvidence(run_id="run-4")
    output = "\n".join([
        "WARN: project build verification needs local dependencies",
        "- [passed] python -m py_compile app/main.py",
        "- [missing_dependency] pytest",
        "  No module named 'fastapi'",
        "- [failed] uvicorn app.main:app --help",
    ])

    evidence.record_tool_result("verify_project_build", {"project_dir": "todo_api"}, output, success=True)

    statuses = [item["status"] for item in evidence.verification_results]
    assert statuses == ["passed", "missing_dependency", "failed"]
    assert len(evidence.build_results) == 3
    assert any("local dependencies" in item for item in evidence.limitations)


def test_record_check_project_completeness_saves_review_and_limitations():
    evidence = RunEvidence(run_id="run-5")
    payload = {
        "status": "partial",
        "implemented": ["CRUD endpoints"],
        "missing": ["SQLite persistence"],
        "notes": ["SQLite requested but the scaffold uses documented in-memory storage instead of real persistence."],
        "recommended_next_steps": ["Implement sqlite repository."],
    }

    evidence.record_tool_result("check_project_completeness", {"project_dir": "todo_api"}, json.dumps(payload), success=True)

    assert evidence.completeness_review["status"] == "partial"
    assert any("SQLite requested" in item for item in evidence.limitations)
    assert any(item.startswith("completeness missing:") for item in evidence.notes)
    assert any(item.startswith("next:") for item in evidence.notes)


def test_record_error_output_adds_tool_failure():
    evidence = RunEvidence(run_id="run-6")

    evidence.record_tool_result("scaffold_project", {"project_name": "todo_api"}, "Error: project directory already exists", success=False)

    assert evidence.tool_failures
    assert evidence.tool_failures[0]["name"] == "scaffold_project"
    assert evidence.tool_failures[0]["status"] == "error"


def test_summary_text_is_compact_and_non_empty():
    evidence = RunEvidence(run_id="run-7")
    evidence.record_tool_result(
        "scaffold_project",
        {"project_name": "todo_api"},
        "Files created: 1\n- todo_api/app/main.py\nNext steps:",
        success=True,
    )
    evidence.record_tool_result(
        "verify_project_build",
        {"project_dir": "todo_api"},
        "OK: project build verification passed\n- [passed] python -m py_compile app/main.py",
        success=True,
    )

    summary = evidence.summary_text()

    assert summary.startswith("RunEvidence:")
    assert "created_files: 1" in summary
    assert "verification:" in summary
    assert len(summary) < 400


def test_record_tool_result_tolerates_non_json_output():
    evidence = RunEvidence(run_id="run-8")

    evidence.record_tool_result("create_project_blueprint", {"project_spec": {}}, "not json at all", success=True)
    evidence.record_tool_result("analyze_impact", {}, "Patch risk: medium\nReasons:\n- 2 source files changed", success=True)

    assert evidence.blueprint is None
    assert evidence.impact_review["risk"] == "medium"


def test_record_impact_parses_replan_metadata_from_text():
    evidence = RunEvidence(run_id="run-risk")

    evidence.record_tool_result(
        "analyze_impact",
        {},
        "Patch risk: high\nRisk fingerprint: abc123\nRequires replan: true\nReasons:\n- public API changed",
        success=True,
    )

    assert evidence.impact_review["risk"] == "high"
    assert evidence.impact_review["fingerprint"] == "abc123"
    assert evidence.impact_review["requires_replan"] is True


def test_record_verify_changed_files_has_static_stage():
    evidence = RunEvidence(run_id="run-static")

    evidence.record_tool_result(
        "verify_changed_files",
        {},
        "OK: py_compile changed files\nOK pkg/module.py",
        success=True,
    )

    assert evidence.verification_results == [{
        "tool": "verify_changed_files",
        "command": "verify_changed_files",
        "stage": "static",
        "status": "passed",
        "summary": "OK: py_compile changed files",
        "preview": "OK: py_compile changed files\nOK pkg/module.py",
    }]


def test_record_bash_verification_classifies_targeted_and_regression():
    evidence = RunEvidence(run_id="run-bash")

    evidence.record_tool_result(
        "bash",
        {"command": "pytest -q tests/test_module.py::test_fix"},
        "1 passed",
        success=True,
    )
    evidence.record_tool_result(
        "bash",
        {"command": "pytest -q"},
        "42 passed",
        success=True,
    )

    assert [item["stage"] for item in evidence.verification_results] == [
        "targeted", "regression",
    ]
    assert all(item["status"] == "passed" for item in evidence.verification_results)


def test_non_verification_bash_is_not_verification_evidence():
    evidence = RunEvidence(run_id="run-search")

    evidence.record_tool_result(
        "bash",
        {"command": "rg pytest tests"},
        "tests/test_module.py",
        success=True,
    )

    assert evidence.verification_results == []


def test_bash_verification_retry_replaces_failure_with_latest_pass():
    evidence = RunEvidence(run_id="run-retry")
    command = "pytest tests/test_module.py::test_fix"

    evidence.record_tool_result(
        "bash", {"command": command}, "1 failed", success=False,
    )
    evidence.record_tool_result(
        "bash", {"command": command}, "1 passed", success=True,
    )

    assert len(evidence.verification_results) == 1
    assert evidence.verification_results[0]["status"] == "passed"
    assert evidence.verification_results[0]["stage"] == "targeted"
    assert evidence.tool_failures == []


def test_bash_retry_upsert_normalizes_python_module_runner_and_flags():
    evidence = RunEvidence(run_id="run-canonical-retry")
    target = "tests/test_module.py::test_fix"

    evidence.record_tool_result(
        "bash", {"command": f"pytest {target}"}, "1 failed", success=False,
    )
    evidence.record_tool_result(
        "bash",
        {"command": f"poetry run python -m pytest -q {target}"},
        "1 passed",
        success=True,
    )

    assert len(evidence.verification_results) == 1
    assert evidence.verification_results[0]["status"] == "passed"


def test_unreliable_retry_cannot_overwrite_prior_failed_evidence():
    evidence = RunEvidence(run_id="run-untrusted-retry")
    target = "tests/test_module.py::test_fix"

    evidence.record_tool_result(
        "bash", {"command": f"pytest {target}"}, "1 failed", success=False,
    )
    evidence.record_tool_result(
        "bash", {"command": f"pytest {target} || true"}, "failure hidden", success=True,
    )

    assert evidence.verification_results[0]["status"] == "failed"

    evidence.record_tool_result(
        "bash", {"command": f"python -m pytest -q {target}"}, "1 passed", success=True,
    )

    assert len(evidence.verification_results) == 1
    assert evidence.verification_results[0]["status"] == "passed"


def test_bash_compound_records_each_verification_stage():
    evidence = RunEvidence(run_id="run-compound")

    evidence.record_tool_result(
        "bash",
        {
            "command": "python -m py_compile pkg/module.py && "
            "pytest tests/test_module.py::test_fix"
        },
        "1 passed",
        success=True,
    )

    assert [item["stage"] for item in evidence.verification_results] == [
        "static", "targeted",
    ]


def test_unreliable_shell_success_is_not_recorded_as_verification():
    evidence = RunEvidence(run_id="run-hidden-failure")

    evidence.record_tool_result(
        "bash",
        {"command": "cargo check || true"},
        "error: could not compile demo",
        success=True,
    )

    assert evidence.verification_results == []


def test_bash_dispatch_failure_is_tool_failure_not_verification_failure():
    evidence = RunEvidence(run_id="run-dispatch-failure")

    evidence.record_tool_result(
        "bash",
        {"command": "pytest tests/test_module.py::test_fix"},
        "Error: command blocked by policy",
        success=False,
        dispatch_failed=True,
    )

    assert evidence.verification_results == []
    assert len(evidence.tool_failures) == 1


def test_bash_environment_status_uses_trusted_exit_metadata():
    fake = RunEvidence(run_id="run-fake-environment")
    fake.record_tool_result(
        "bash",
        {"command": "pytest tests/test_module.py::test_fix"},
        (
            "Command exited with code 1\n"
            "Command exited with code 127\npytest: command not found"
        ),
        success=False,
        command_failed=True,
        metadata={"exit": 1},
    )

    real = RunEvidence(run_id="run-real-environment")
    real.record_tool_result(
        "bash",
        {"command": "pytest tests/test_module.py::test_fix"},
        "Command exited with code 127\npytest: command not found",
        success=False,
        command_failed=True,
        metadata={"exit": 127},
    )

    assert fake.verification_results[0]["status"] == "failed"
    assert real.verification_results[0]["status"] == "missing_dependency"


def test_cargo_harness_list_is_not_recorded_as_executed_test():
    evidence = RunEvidence(run_id="run-cargo-list")

    evidence.record_tool_result(
        "bash",
        {"command": "cargo test test_parser -- --list"},
        "test_parser: test",
        success=True,
    )

    assert evidence.verification_results == []


def test_manual_fail_output_with_zero_exit_is_failed_evidence():
    evidence = RunEvidence(run_id="run-fake-pass")

    evidence.record_tool_result(
        "bash",
        {"command": "python -c \"assert True\""},
        "Test 1 FAIL: wrong\nTest 2 PASS",
        success=True,
    )

    assert evidence.verification_results[0]["status"] == "failed"


def test_setup_failed_signal_matches_manager_failure_semantics():
    evidence = RunEvidence(run_id="run-setup-failed")

    evidence.record_tool_result(
        "bash",
        {"command": "python -c \"assert True\""},
        "Django setup failed while loading settings",
        success=True,
    )

    assert evidence.verification_results[0]["status"] == "failed"


def test_unasserted_python_probe_can_record_negative_evidence_only():
    evidence = RunEvidence(run_id="run-negative-probe")

    evidence.record_tool_result(
        "bash",
        {"command": "python -c \"import missing_package\""},
        "No module named 'missing_package'",
        success=True,
    )

    assert evidence.verification_results == [{
        "tool": "bash",
        "command": "python -c \"import missing_package\"",
        "stage": "static",
        "status": "failed",
        "preview": "No module named 'missing_package'",
    }]


def test_project_build_commands_receive_verification_stages():
    evidence = RunEvidence(run_id="run-build-stages")
    output = "\n".join([
        "OK: project build verification passed",
        "- [passed] python -m py_compile app/main.py",
        "- [passed] pytest",
    ])

    evidence.record_tool_result("verify_project_build", {}, output, success=True)

    assert [item["stage"] for item in evidence.build_results] == [
        "static", "regression",
    ]


def test_code_write_invalidates_verification_from_previous_diff_generation():
    evidence = RunEvidence(run_id="run-generation")
    evidence.record_tool_result(
        "bash",
        {"command": "pytest tests/test_module.py::test_fix"},
        "1 failed",
        success=False,
    )
    assert evidence.verification_results

    evidence.record_tool_result(
        "edit_file",
        {"path": "pkg/module.py", "old_text": "bad", "new_text": "good"},
        "Updated pkg/module.py",
        success=True,
    )

    assert evidence.verification_results == []
    assert evidence.build_results == []


def test_applied_child_source_changes_invalidate_previous_verification():
    """A parent merge is a workspace mutation even though the child did edits."""
    evidence = RunEvidence(run_id="run-child-generation")
    evidence.record_tool_result(
        "bash",
        {"command": "pytest tests/test_parser.py"},
        "12 passed",
        success=True,
    )

    evidence.record_tool_result(
        "apply_agent_changes",
        {"reviewed_files": ["src/parser.py", "tests/test_parser.py"]},
        "Applied reviewed child changes",
        success=True,
    )

    assert evidence.verification_results == []
    assert evidence.modified_files == ["src/parser.py", "tests/test_parser.py"]


def test_mutating_bash_invalidates_previous_verification_without_guessing_paths():
    """Shell writes are unattributed but still stale prior verification."""
    evidence = RunEvidence(run_id="run-shell-generation")
    evidence.record_tool_result(
        "bash", {"command": "pytest -q tests"}, "12 passed", success=True,
    )

    evidence.record_tool_result(
        "bash", {"command": "mv src/old.py src/new.py"}, "", success=True,
    )

    assert evidence.verification_results == []
    assert evidence.modified_files == []


def test_root_document_write_keeps_current_verification_evidence():
    evidence = RunEvidence(run_id="run-doc")
    evidence.record_tool_result(
        "bash", {"command": "pytest"}, "10 passed", success=True,
    )

    evidence.record_tool_result(
        "write_file",
        {"path": "CHANGES.md", "content": "notes"},
        "Created CHANGES.md",
        success=True,
    )

    assert len(evidence.verification_results) == 1
    assert evidence.verification_results[0]["status"] == "passed"


def test_symbol_check_evidence_is_keyed_by_file_path():
    evidence = RunEvidence(run_id="run-symbol-paths")

    evidence.record_tool_result(
        "python_symbol_check", {"path": "pkg/a.py"}, "Error: missing symbol", success=False,
    )
    evidence.record_tool_result(
        "python_symbol_check", {"path": "pkg/b.py"}, "OK: symbols verified", success=True,
    )

    assert len(evidence.verification_results) == 2
    assert [item["path"] for item in evidence.verification_results] == ["pkg/a.py", "pkg/b.py"]
    assert [item["status"] for item in evidence.verification_results] == ["error", "passed"]
