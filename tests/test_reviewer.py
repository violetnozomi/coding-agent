"""Tests for read-only run evidence review."""
from __future__ import annotations

import json

from nz_coder.reviewer import review_run_evidence, review_run_evidence_tool


def test_project_creation_without_created_files_fails():
    review = review_run_evidence({
        "task_mode": "project_creation",
        "expected_files": ["todo_api/app/main.py"],
    })

    assert review["review_status"] == "failed"
    assert "created_files" in review["missing_evidence"]


def test_project_creation_with_files_but_no_verification_needs_fix():
    review = review_run_evidence({
        "task_mode": "project_creation",
        "created_files": ["todo_api/app/main.py"],
        "expected_files": ["todo_api/app/main.py"],
    })

    assert review["review_status"] == "needs_fix"
    assert "verification_results" in review["missing_evidence"]


def test_project_creation_complete_and_verified_is_approved():
    review = review_run_evidence({
        "task_mode": "project_creation",
        "created_files": ["todo_api/app/main.py", "todo_api/tests/test_api.py"],
        "expected_files": ["todo_api/app/main.py", "todo_api/tests/test_api.py"],
        "verification_results": [{"status": "passed", "command": "pytest"}],
        "completeness_review": {"status": "ok"},
    })

    assert review["review_status"] == "approved"


def test_project_creation_sqlite_limitation_is_approved_with_limitations():
    review = review_run_evidence({
        "task_mode": "project_creation",
        "created_files": ["todo_api/app/main.py"],
        "expected_files": ["todo_api/app/main.py"],
        "verification_results": [{"status": "passed", "command": "python -m py_compile app/main.py"}],
        "completeness_review": {"status": "ok_with_limitations"},
        "limitations": ["SQLite requested but scaffold uses in-memory storage."],
    })

    assert review["review_status"] == "approved_with_limitations"


def test_bugfix_modified_without_verification_needs_fix():
    review = review_run_evidence({
        "task_mode": "bugfix",
        "modified_files": ["nz_coder/loop.py"],
    }, runtime={"has_diff": True})

    assert review["review_status"] == "needs_fix"
    assert "verification_results" in review["missing_evidence"]



def test_bugfix_with_missing_requested_tests_needs_fix():
    review = review_run_evidence({
        "task_mode": "feature",
        "modified_files": ["pkg/user_manager.py"],
        "verification_results": [{"status": "passed", "summary": "OK: py_compile changed files"}],
    }, runtime={"has_diff": True, "wants_tests": True, "tests_modified": False})

    assert review["review_status"] == "needs_fix"
    assert any("tests" in item.lower() for item in review["required_next_steps"])

def test_bugfix_same_basename_in_other_directory_needs_fix():
    review = review_run_evidence({
        "task_mode": "bugfix",
        "modified_files": ["tmp/user_manager.py"],
        "actual_output_paths": ["tmp/user_manager.py"],
        "verification_results": [{"status": "passed", "summary": "OK: py_compile changed files"}],
    }, runtime={"has_diff": True, "requested_paths": ["pkg/services/user_manager.py"]})

    assert review["review_status"] == "needs_fix"
    assert any("same-basename" in item for item in review["reasons"])


def test_bugfix_modified_and_verified_is_approved():
    review = review_run_evidence({
        "task_mode": "bugfix",
        "modified_files": ["nz_coder/loop.py"],
        "verification_results": [{"status": "passed", "summary": "OK: py_compile changed files"}],
    }, runtime={"has_diff": True, "diff_chars": 200})

    assert review["review_status"] == "approved"


def test_unreviewed_patch_risk_is_approved_with_limitation():
    review = review_run_evidence({
        "task_mode": "bugfix",
        "modified_files": ["pkg/api.py"],
        "verification_results": [{"status": "passed", "summary": "OK"}],
        "impact_review": {
            "risk": "high",
            "requires_replan": True,
            "risk_signals": [{"category": "deleted_public_symbols"}],
        },
    }, runtime={"has_diff": True, "patch_risk_reviewed": False})

    assert review["review_status"] == "approved_with_limitations"
    assert any("deleted_public_symbols" in item for item in review["limitations"])


def test_replanned_patch_risk_is_recorded_as_reviewed():
    review = review_run_evidence({
        "task_mode": "bugfix",
        "modified_files": ["pkg/api.py"],
        "verification_results": [{"status": "passed", "summary": "OK"}],
        "impact_review": {
            "risk": "high",
            "requires_replan": True,
            "risk_signals": [{"category": "public_signature_change"}],
        },
    }, runtime={"has_diff": True, "patch_risk_reviewed": True})

    assert review["review_status"] == "approved"
    assert any("risk=high" in item for item in review["reasons"])


def test_unknown_empty_evidence_is_approved():
    review = review_run_evidence({})

    assert review["review_status"] == "approved"


def test_tool_failures_force_needs_fix():
    review = review_run_evidence({
        "task_mode": "bugfix",
        "modified_files": ["nz_coder/loop.py"],
        "verification_results": [{"status": "passed", "summary": "OK"}],
        "tool_failures": [{"name": "edit_file", "status": "error", "preview": "Error: failed"}],
    }, runtime={"has_diff": True})

    assert review["review_status"] == "needs_fix"


def test_review_handler_returns_valid_json():
    text = review_run_evidence_tool({
        "task_mode": "project_creation",
        "created_files": ["todo_api/app/main.py"],
        "verification_results": [{"status": "missing_dependency", "command": "pytest"}],
    })

    payload = json.loads(text)
    assert payload["review_status"] in {"approved_with_limitations", "needs_fix"}
    assert payload["summary"]
