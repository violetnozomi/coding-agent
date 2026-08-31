"""Tests for RuntimeState persistence and acceptance criteria."""
from __future__ import annotations


def test_retired_investigation_hard_limit_remains_importable_for_compatibility():
    """Existing integrations may import the retired public policy constant."""
    from nz_coder.runtime.execution.runtime_state import STRICT_INVESTIGATION_HARD_LIMIT

    assert STRICT_INVESTIGATION_HARD_LIMIT == 20


def test_runtime_state_persists_active_state(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    path = tmp_path / "runtime_state.json"
    state = RuntimeState()
    state.reset(max_turns=12, timeout_seconds=90)
    state.turn_count = 4
    state.edits_this_run = 1
    state.has_diff = True
    state.diff_chars = 300
    state.changed_files = ["app.py"]
    state.acceptance_criteria = ["tests/test_app.py::test_bug"]
    state.current_round_instruction_text = "Continue and update app.py."
    state.save(path, active=True)

    restored = RuntimeState()
    assert restored.load(path) is True
    assert restored.turn_count == 4
    assert restored.has_diff is True
    assert restored.task_complexity() == "L1"
    assert restored.acceptance_criteria == ["tests/test_app.py::test_bug"]
    assert restored.current_round_instruction_text == "Continue and update app.py."

    restored.save(path, active=False)
    inactive = RuntimeState()
    assert inactive.load(path) is False


def test_runtime_state_save_is_atomic_on_commit_failure(tmp_path, monkeypatch):
    """An interrupted checkpoint must preserve the last resumable snapshot."""
    from pathlib import Path

    import pytest

    from nz_coder.runtime.execution.runtime_state import RuntimeState

    path = tmp_path / "runtime_state.json"
    path.write_text('{"active": true, "turn_count": 3}\n', encoding="utf-8")
    state = RuntimeState()
    state.reset(max_turns=12)
    state.turn_count = 4

    def fail_commit(_self, _target):
        raise OSError("simulated commit failure")

    monkeypatch.setattr(Path, "replace", fail_commit)

    with pytest.raises(OSError, match="simulated commit failure"):
        state.save(path)

    assert path.read_text(encoding="utf-8") == (
        '{"active": true, "turn_count": 3}\n'
    )
    assert not list(tmp_path.glob(".runtime_state.json.*.tmp"))


def test_runtime_state_records_recovery_diagnostic_and_repair_target():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.record_recovery_diagnostic(
        "<test-failure-diagnostic>\n"
        "primary_classification: subprocess_workspace_drift\n"
        "supporting_classification: widespread_test_regression\n"
        "repair_target: tests/test_cli.py\n"
        "</test-failure-diagnostic>"
    )

    assert state.primary_recovery_classification == "subprocess_workspace_drift"
    assert state.supporting_recovery_classifications == [
        "widespread_test_regression",
    ]
    assert state.recovery_repair_targets == ["tests/test_cli.py"]
    assert "tests/test_cli.py" in state._known_closure_paths()


def test_runtime_state_clears_stale_recovery_facts_for_generic_diagnostic():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.record_recovery_diagnostic(
        "primary_classification: subprocess_workspace_drift\n"
        "repair_target: tests/test_cli.py"
    )

    state.record_recovery_diagnostic(
        "<test-failure-diagnostic>\nFailing tests: test_parser.py\n"
        "</test-failure-diagnostic>"
    )

    assert state.primary_recovery_classification == ""
    assert state.supporting_recovery_classifications == []
    assert state.recovery_repair_targets == []


def test_runtime_state_treats_package_root_failure_as_localized_repair():
    """A known subprocess helper target must remain eligible for repair reserve."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.has_diff = True
    state.changed_files = ["cron_engine/tests/test_cli.py"]
    state.verification_failures = 1
    state.needs_broad_exploration = True

    state.record_recovery_diagnostic(
        "<test-failure-diagnostic>\n"
        "primary_classification: subprocess_package_root\n"
        "repair_target: cron_engine/tests/test_cli.py\n"
        "</test-failure-diagnostic>"
    )

    assert state.recovery_repair_targets == ["cron_engine/tests/test_cli.py"]
    assert state.needs_broad_exploration is False
    assert state.emergency_eligibility().eligible is True


def test_runtime_state_records_semantic_review_against_current_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
    }))
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_requirement_verification(
        "pytest -q tests",
        passed=True,
        acceptance=True,
    )

    assert state.semantic_review_pending_only() is True
    state.observe_requirement_semantic_review(
        accepted=True,
        fingerprint="verifier_ok:compatibility",
    )

    assert state.requirement_ledger_snapshot().status("R1") == "satisfied"
    assert state.semantic_review_pending_only() is False


def test_diff_status_does_not_reopen_verified_requirements():
    """Refreshing same-generation changed paths must be ledger-idempotent."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Change behavior without breaking compatibility",
        "requirements": [
            {
                "id": "R1",
                "description": "Change app behavior",
                "kind": "behavior",
                "expected_artifacts": ["app.py"],
            },
            {
                "id": "R2",
                "description": "Preserve compatibility",
                "kind": "compatibility",
            },
        ],
    }))
    state.observe_tool("edit_file", {"path": "app.py"}, "Done", succeeded=True)
    state.observe_requirement_verification(
        "pytest -q tests",
        passed=True,
        acceptance=True,
    )

    assert state.requirement_ledger_snapshot().status("R1") == "satisfied"
    assert state.semantic_review_pending_only() is True

    state.observe_tool(
        "diff_status",
        {},
        "has_non_empty_diff: true\n"
        "diff_chars: 12\n"
        "changed_files_count: 1\n\n"
        "Changed files:\n"
        "  app.py\n\n"
        "Recommendation: verify",
        succeeded=True,
    )

    assert state.requirement_ledger_snapshot().status("R1") == "satisfied"
    assert state.semantic_review_pending_only() is True


def test_failed_write_does_not_create_a_mutation_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    state.observe_tool(
        "apply_patch",
        {"changes": [{"path": "app.py"}]},
        "Error: old_text not found",
        succeeded=False,
    )

    assert state.mutation_generation == 0
    assert state.source_mutation_generation == 0


def test_applied_child_changes_advance_generation_and_attribute_reviewed_files():
    """A parent merge must invalidate stale acceptance for every reviewed file."""
    import nz_coder.runtime.agent.agent_manager  # noqa: F401
    from nz_coder.runtime.agent.task_contract import TaskContract
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Apply the reviewed child implementation",
        "requirements": [{
            "id": "R1",
            "description": "Update the parser",
            "kind": "artifact",
            "expected_artifacts": ["src/parser.py"],
            "satisfaction_mode": "deterministic",
        }],
    }))

    state.observe_tool(
        "apply_agent_changes",
        {"reviewed_files": ["src/parser.py", "tests/test_parser.py"]},
        "Applied reviewed child changes",
        succeeded=True,
    )

    assert state.mutation_generation == 1
    assert state.acceptance_mutation_generation == 1
    assert state.source_mutation_generation == 1
    assert state.requirement_ledger_snapshot().status("R1") == "satisfied"


def test_registered_write_tool_automatically_advances_mutation_generation():
    """A newly registered local write must not require another runtime name list."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
    )

    name = "_test_workspace_writer"
    try:
        register(
            name,
            "Write one workspace file",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            lambda path: f"Wrote {path}",
            execution="write",
        )
        state = RuntimeState()

        state.observe_tool(name, {"path": "src/new.py"}, "Wrote src/new.py")

        assert state.mutation_generation == 1
        assert state.source_mutation_generation == 1
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]


def test_internal_state_write_does_not_invalidate_workspace_acceptance():
    """Saving product metadata is not evidence that task files changed."""
    import nz_coder.runtime.workflows.workflow_library  # noqa: F401
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    state.observe_tool(
        "workflow_save",
        {"name": "review", "capsule": {}},
        "Saved workflow",
        succeeded=True,
    )

    assert state.mutation_generation == 0
    assert state.acceptance_mutation_generation is None


def test_apply_patch_dry_run_does_not_create_mutation_evidence():
    """A preview must not invalidate evidence for an unchanged workspace."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    state.observe_tool(
        "apply_patch",
        {
            "changes": [{"op": "replace", "path": "src/app.py"}],
            "dry_run": True,
        },
        "Patch preview",
        succeeded=True,
    )

    assert state.mutation_generation == 0
    assert state.source_mutation_generation == 0


def test_successful_mutating_bash_conservatively_invalidates_acceptance():
    """An unattributed shell write cannot leave source evidence current."""
    from nz_coder.runtime.agent.task_contract import TaskContract
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Fix parser behavior",
        "requirements": [{
            "id": "R1",
            "description": "Fix parser behavior",
            "kind": "behavior",
            "expected_artifacts": ["src/parser.py"],
        }],
    }))
    state.observe_tool("edit_file", {"path": "src/parser.py"}, "Done")
    state.observe_requirement_verification(
        "pytest -q tests/test_parser.py",
        passed=True,
        acceptance=True,
    )

    state.observe_tool(
        "bash",
        {"command": "touch src/generated.py"},
        "",
        succeeded=True,
    )

    assert state.mutation_generation == 2
    assert state.acceptance_mutation_generation == 2
    assert state.source_mutation_generation == 2
    assert state.requirement_ledger_snapshot().status("R1") == "candidate"


def test_successful_read_or_verification_bash_does_not_claim_mutation():
    """Shell mutation tracking must not reclassify reads or test commands."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.observe_tool(
        "bash", {"command": "git status"}, "clean", succeeded=True,
    )
    state.observe_tool(
        "bash", {"command": "pytest -q tests/test_parser.py"}, "1 passed",
        succeeded=True,
    )

    assert state.mutation_generation == 0


def test_verification_with_fd_duplication_does_not_claim_mutation():
    """A bounded test-output pipeline is verification, not a phantom edit."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.turn_count = 6

    state.observe_tool(
        "bash",
        {
            "command": (
                "python3 -m pytest tests/forms_tests/tests/test_media.py "
                "2>&1 | tail -20"
            )
        },
        "Command exited with code 1",
        succeeded=False,
    )

    assert state.mutation_generation == 0
    assert state.edits_this_run == 0
    assert state.last_edit_turn == 0


def test_source_mutation_generation_ignores_tests_and_documentation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.observe_tool("edit_file", {"path": "src/app.py"}, "Done")
    assert state.source_mutation_generation == 1

    state.observe_tool("edit_file", {"path": "tests/test_app.py"}, "Done")
    state.observe_tool("edit_file", {"path": "README.md"}, "Done")

    assert state.mutation_generation == 3
    assert state.source_mutation_generation == 1


def test_ephemeral_scratch_test_writes_do_not_advance_mutation_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.observe_tool("edit_file", {"path": "src/app.py"}, "Done")
    assert state.mutation_generation == 1

    scratch = "scratch/probe_scratch.py"
    state.observe_tool(
        "write_file",
        {"path": scratch},
        f"Created {scratch} (21 bytes)",
    )
    state.observe_tool(
        "apply_patch",
        {"changes": [{"path": scratch, "op": "delete", "old_text": "test"}]},
        "Deleted",
    )

    assert state.mutation_generation == 1
    assert state.source_mutation_generation == 1
    assert state.edits_this_run == 1


def test_editing_existing_scratch_file_advances_mutation_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.observe_tool(
        "edit_file",
        {"path": "tests/unit/test_app_scratch.py"},
        "Edited",
    )

    assert state.mutation_generation == 1
    assert state.edits_this_run == 1


def test_replacing_existing_scratch_file_advances_mutation_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.observe_tool(
        "apply_patch",
        {
            "changes": [
                {
                    "path": "tests/unit/test_app_scratch.py",
                    "op": "replace",
                    "old_text": "old",
                    "new_text": "new",
                }
            ]
        },
        "Applied patch",
    )

    assert state.mutation_generation == 1
    assert state.edits_this_run == 1


def test_contradictory_scratch_write_output_advances_mutation_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    scratch = "scratch/probe_scratch.py"
    state.observe_tool(
        "write_file",
        {"path": scratch},
        f"Updated {scratch} (21 bytes)\nCreated {scratch} (21 bytes)",
    )

    assert state.mutation_generation == 1
    assert state.edits_this_run == 1


def test_restore_discards_unserialized_scratch_lifecycle_state():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    scratch = "scratch/probe_scratch.py"
    state = RuntimeState()
    state.observe_tool(
        "write_file",
        {"path": scratch},
        f"Created {scratch} (21 bytes)",
    )
    assert state.mutation_generation == 0

    restored_snapshot = RuntimeState().to_dict(active=True)
    assert state.restore(restored_snapshot) is True
    state.observe_tool(
        "apply_patch",
        {"changes": [{"path": scratch, "op": "delete"}]},
        "Applied patch",
    )

    assert state.mutation_generation == 1
    assert state.edits_this_run == 1


def test_non_git_workspace_blocks_redundant_git_observation_during_closure():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.work_phase = "closure_repair"
    state.workspace_git_available = False

    assert state.closure_phase_decision(
        "bash", {"command": "git diff -- app.py"},
    ) == ("block", "git_required_but_unavailable")
    assert state.closure_phase_action(
        "bash", {"command": "git diff -- app.py"},
    ) == "block"
    assert state.closure_phase_action("diff_status", {}) == "allow"


def test_git_workspace_allows_read_only_git_observation_during_closure():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    for phase in ("closure_repair", "closure_finalize", "bounded_emergency"):
        state = RuntimeState()
        state.work_phase = phase
        state.workspace_git_available = True

        assert state.closure_phase_action(
            "bash", {"command": "git diff -- app.py"},
        ) == "allow"
        assert state.closure_phase_action(
            "bash", {"command": "git status --short"},
        ) == "allow"
        assert state.closure_phase_action(
            "bash", {"command": "git diff && git checkout -- app.py"},
        ) == "block"


def test_budget_pressure_blocks_non_git_observation_before_closure_phase():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.work_phase = "normal"
    state.budget_pressure_zone = "orange"
    state.workspace_git_available = False

    assert state.closure_phase_decision(
        "bash", {"command": "git status --short"},
    ) == ("block", "git_required_but_unavailable")


def test_green_zone_keeps_git_observation_available_for_ordinary_work():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.work_phase = "normal"
    state.budget_pressure_zone = "green"
    state.workspace_git_available = False

    assert state.closure_phase_decision(
        "bash", {"command": "git status --short"},
    ) == ("allow", "")


def test_workspace_git_fact_survives_runtime_state_round_trip():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    for available in (True, False, None):
        state = RuntimeState()
        state.workspace_git_available = available
        restored = RuntimeState()

        assert restored.restore(state.to_dict(active=True)) is True
        assert restored.workspace_git_available is available


def test_runtime_state_captures_and_restores_verification_contract(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    path = tmp_path / "runtime_state.json"
    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text(
        "完成后运行 python -m pytest -q cron_engine/tests，并总结。"
    )

    assert state.verification_contract["command"] == (
        "python -m pytest -q cron_engine/tests"
    )
    assert state.verification_contract["targets"] == ["cron_engine/tests"]

    state.save(path, active=True)
    restored = RuntimeState()
    assert restored.load(path) is True
    assert restored.verification_contract == state.verification_contract


def test_runtime_state_tracks_contract_artifacts_without_todo(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    contract = TaskContract.from_dict({
        "objective": "Create an artifact",
        "requirements": [{
            "id": "R1",
            "description": "Create app.py",
            "kind": "artifact",
            "expected_artifacts": ["app.py"],
            "satisfaction_mode": "deterministic",
        }],
    }, workspace=tmp_path)
    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_task_contract(contract)

    state.observe_tool(
        "write_file",
        {"path": "app.py", "content": "VALUE = 1\n"},
        "Created app.py",
        succeeded=True,
    )

    assert state.open_todo_items == 0
    assert state.requirement_ledger_snapshot().status("R1") == "satisfied"


def test_acceptance_criteria_extracts_tests_and_should_sentences():
    from nz_coder.runtime.execution.runtime_state import extract_acceptance_criteria

    text = (
        "FAILED tests/test_dates.py::test_http_date_tz\n"
        "HTTP date parsing should preserve timezone offsets."
    )

    criteria = extract_acceptance_criteria(text)

    assert "tests/test_dates.py::test_http_date_tz" in criteria
    assert any("should preserve timezone" in item for item in criteria)



def test_acceptance_criteria_extracts_numbered_requirements():
    from nz_coder.runtime.execution.runtime_state import extract_acceptance_criteria

    text = (
        "1. 用户注册时校验 email 格式\n"
        "2. 新增 update_user(user_id, **kwargs) 函数\n"
        "3. 写 test_user_manager.py，覆盖正常流程和异常情况"
    )

    criteria = extract_acceptance_criteria(text)

    assert any("校验 email 格式" in item for item in criteria)
    assert any("update_user" in item for item in criteria)
    assert any("test_user_manager.py" in item for item in criteria)


def test_swebench_problem_title_seeds_coding_mode_and_acceptance_criterion():
    """A wrapped benchmark feature request must not degrade to unknown/L0 work."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text(
        "Solve SWE-bench Lite instance `pytest-dev__pytest-5221`.\n"
        "Repository: pytest-dev/pytest\n\n"
        "Problem statement:\n"
        "Display fixture scope with `pytest --fixtures`\n"
        "It would be useful to show fixture scopes in the command output.\n\n"
        "When finished, leave the repository with only the intended "
        "source-code changes."
    )

    assert state.task_mode == "feature"
    assert state.acceptance_criteria == [
        "Display fixture scope with `pytest --fixtures`"
    ]


def test_l0_runtime_state_omits_routine_prompt_block():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=50)
    state.turn_count = 1

    assert state.task_complexity() == "L0"
    assert state.build_prompt_block() == ""


def test_runtime_state_keeps_all_small_numbered_requirements_visible():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text(
        "1. 修改 api/user.py\n2. 新增 tests/test_user_api.py\n3. 更新 docs/user.md\n4. 写回归测试覆盖异常分支"
    )
    state.turn_count = 2

    block = state.build_prompt_block()

    assert "Acceptance criteria (4)" in block
    assert "写回归测试覆盖异常分支" in block


def test_runtime_state_discuss_mode_does_not_push_source_edit():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("How should we design this API?")
    state.turn_count = 12

    block = state.build_prompt_block()

    assert "task_mode=discuss" in block
    assert "No source edit" not in block
    assert "smallest relevant code change" not in block


def test_runtime_state_tests_modified_is_neutral_when_requested():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("add unit tests for parser")
    state.turn_count = 2
    state.has_diff = True
    state.diff_chars = 500
    state.changed_files = ["src/parser.py", "tests/test_parser.py"]
    state.tests_modified = True

    block = state.build_prompt_block()

    assert "task_mode=test" in block
    assert "test updates requested" in block
    assert "confirm this matches" not in block



def test_runtime_state_warns_when_task_requests_tests_but_none_changed():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("在 test/user_manger.py 的基础上新增 update_user，并写 test_user_manager.py")
    state.turn_count = 3
    state.has_diff = True
    state.diff_chars = 400
    state.changed_files = ["test/user_manger.py"]
    state.tests_modified = False

    block = state.build_prompt_block()

    assert "User named target files" in block
    assert "test/user_manger.py" in block
    assert "no test files have been changed yet" in block.lower()


def test_runtime_state_enforces_explicit_immutable_test_constraint():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text(
        "Fix app.py, do not modify tests, then run pytest -q tests/test_app.py"
    )

    assert state.wants_tests is False
    assert state.forbids_test_changes is True
    assert state.task_constraint_action(
        "apply_patch", {"path": "tests/test_app.py", "patch": "..."}
    ) == "block"
    assert state.task_constraint_action(
        "edit_file", {"path": "app.py", "old_text": "a", "new_text": "b"}
    ) == "allow"


def test_runtime_state_does_not_promote_traceback_paths_to_user_targets():
    """SWE issue evidence paths are retrieval hints, not requested artifacts."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text(
        "Solve SWE-bench Lite instance pytest-dev__pytest-11148.\n\n"
        "Problem statement:\n"
        "Tests fail after core.initialize() has no effect.\n"
        "Traceback (most recent call last):\n"
        "  File \"/usr/local/lib/python3.8/site-packages/_pytest/runner.py\"\n"
        "  File \"/Users/dev/code/pmxbot/pmxbot/tests/unit/test_commands.py\"\n"
        "The extension collector.c is also loaded.\n"
    )

    assert state.requested_paths == []


def test_runtime_state_requested_paths_require_positive_mutation_intent():
    """Only paths coupled to requested writes receive hard target authority."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text(
        "Fix app.py and update src/parser.py. "
        "Do not modify tests/test_app.py; then run pytest tests/test_app.py."
    )

    assert state.requested_paths == ["app.py", "src/parser.py"]


def test_requested_paths_exclude_same_clause_verification_target():
    """A pytest target is execution evidence even when it follows a source edit."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text(
        "Fix app.py and then run python -m pytest -q tests/test_app.py."
    )

    assert state.requested_paths == ["app.py"]


def test_requested_paths_exclude_traceback_tail_after_fix_intent():
    """A leading repair verb must not promote later traceback frames."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text(
        "Fix initialization: Traceback File src/_pytest/runner.py, line 42."
    )

    assert state.requested_paths == []


def test_requested_paths_reject_absolute_host_paths():
    """Strong target authority is always workspace-relative."""
    from nz_coder.runtime.execution.runtime_state import extract_explicit_mutation_paths

    assert extract_explicit_mutation_paths(
        "Fix /usr/local/lib/python3.8/site-packages/_pytest/runner.py"
    ) == []
    assert extract_explicit_mutation_paths(
        r"Fix C:\Users\dev\project\src\runner.py"
    ) == []


def test_follow_up_does_not_promote_verification_path_to_mutation_target():
    """A run-only follow-up must not overwrite current workspace target facts."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text("Fix src/parser.py")
    state.apply_current_round_instruction(
        "Do not edit tests/test_parser.py; run pytest tests/test_parser.py."
    )

    assert state.requested_paths == ["src/parser.py"]
    assert state.task_constraint_action(
        "write_files_batch",
        {"files": [{"path": "tests/test_app.py", "content": "..."}]},
    ) == "block"
    assert state.task_constraint_action(
        "apply_agent_changes",
        {"reviewed_files": ["app.py", "tests/test_app.py"]},
    ) == "block"
    assert state.task_constraint_action(
        "scaffold_project",
        {"project_name": "demo", "project_type": "python_cli"},
    ) == "block"


def test_current_round_test_request_replaces_old_immutable_test_constraint():
    """The newest explicit User constraint controls whether tests may change."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.set_acceptance_criteria_from_text(
        "Fix app.py without modifying tests."
    )

    state.apply_current_round_instruction(
        "Continue the fix and add regression tests in tests/test_app.py."
    )

    assert state.forbids_test_changes is False
    assert state.wants_tests is True


def test_current_round_requirements_extend_a_satisfied_task_contract(
    monkeypatch,
    tmp_path,
):
    """Completion must wait for explicit deliverables added by a follow-up."""
    from nz_coder.runtime.verification.completion_gate import CompletionGate
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    monkeypatch.chdir(tmp_path)
    (tmp_path / "parser.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "update.py").write_text(
        "def update():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "parser.md").write_text("old\n", encoding="utf-8")
    original_task = (
        "Fix parser.py and run python -m pytest -q tests/test_parser.py"
    )
    state = RuntimeState()
    state.initial_task_text = original_task
    state.set_acceptance_criteria_from_text(original_task)
    state.set_task_contract(derive_task_contract(
        original_task,
        acceptance_command=state.verification_contract["command"],
        workspace=tmp_path,
    ))
    ledger = state.requirement_ledger_snapshot()
    preserved_ids = {
        requirement_id
        for requirement_id, progress in ledger.items.items()
        if progress.requirement.kind != "verification"
    }
    for progress in ledger.items.values():
        progress.status = "satisfied"
    state.requirement_ledger = ledger.to_dict()
    original_objective = state.task_contract["objective"]

    state.apply_current_round_instruction(
        "Continue the fix, update docs/parser.md, and run "
        "python -m pytest -q tests/test_new_parser.py."
    )

    merged = state.requirement_ledger_snapshot()
    unresolved = merged.unresolved()
    decision = CompletionGate().evaluate(
        merged,
        mutation_generation=state.mutation_generation,
    )
    assert state.task_contract["objective"] == original_objective
    assert all(
        merged.status(requirement_id) == "satisfied"
        for requirement_id in preserved_ids
    )
    assert state.task_contract["acceptance_commands"] == [
        "python -m pytest -q tests/test_new_parser.py",
    ]
    assert any(
        progress.requirement.kind == "docs"
        and progress.requirement.expected_artifacts == ("docs/parser.md",)
        for progress in unresolved
    )
    assert any(
        progress.requirement.kind == "verification"
        and "tests/test_new_parser.py" in progress.requirement.description
        for progress in unresolved
    )
    assert all(
        set(progress.requirement.expected_artifacts) <= {"docs/parser.md"}
        for progress in unresolved
    )
    assert decision.ready is False
    assert decision.missing_ids


def test_no_command_followup_requires_explicit_artifact_write(
    monkeypatch,
    tmp_path,
):
    """A named write target must not disappear without a new pytest command."""
    from nz_coder.runtime.verification.completion_gate import CompletionGate
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    monkeypatch.chdir(tmp_path)
    (tmp_path / "parser.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "parser.md").write_text("old\n", encoding="utf-8")
    original_task = (
        "Fix parser.py and run python -m pytest -q tests/test_parser.py"
    )
    state = RuntimeState()
    state.initial_task_text = original_task
    state.set_acceptance_criteria_from_text(original_task)
    state.set_task_contract(derive_task_contract(
        original_task,
        acceptance_command=state.verification_contract["command"],
        workspace=tmp_path,
    ))
    ledger = state.requirement_ledger_snapshot()
    for progress in ledger.items.values():
        progress.status = "satisfied"
    state.requirement_ledger = ledger.to_dict()

    state.apply_current_round_instruction(
        "Continue and update docs/parser.md.",
        workspace=tmp_path,
    )

    ledger = state.requirement_ledger_snapshot()
    artifact = next(
        progress
        for progress in ledger.items.values()
        if progress.requirement.expected_artifacts == ("docs/parser.md",)
        and progress.requirement.kind == "docs"
    )
    decision = CompletionGate().evaluate(
        ledger,
        mutation_generation=state.mutation_generation,
    )
    assert artifact.status == "pending"
    assert artifact.requirement.satisfaction_mode == "deterministic"
    assert artifact.requirement.required_evidence == ()
    assert decision.ready is False
    assert artifact.requirement.id in decision.missing_ids
    assert state.task_contract["acceptance_commands"] == [
        "python -m pytest -q tests/test_parser.py"
    ]
    assert sum(
        progress.requirement.kind == "verification"
        for progress in ledger.items.values()
    ) == 1

    state.observe_tool(
        "edit_file",
        {
            "path": "docs/parser.md",
            "old_text": "old",
            "new_text": "new",
        },
        "Done",
        succeeded=True,
    )

    assert state.requirement_ledger_snapshot().status(
        artifact.requirement.id
    ) == "satisfied"


def test_no_command_read_only_followup_does_not_create_artifact_requirement(
    monkeypatch,
    tmp_path,
):
    """Mentioning a path for inspection is not a deterministic write promise."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    monkeypatch.chdir(tmp_path)
    (tmp_path / "parser.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "parser.md").write_text("old\n", encoding="utf-8")
    original_task = (
        "Fix parser.py and run python -m pytest -q tests/test_parser.py"
    )
    state = RuntimeState(initial_task_text=original_task)
    state.set_acceptance_criteria_from_text(original_task)
    state.set_task_contract(derive_task_contract(
        original_task,
        acceptance_command=state.verification_contract["command"],
        workspace=tmp_path,
    ))
    before = state.task_contract

    state.apply_current_round_instruction(
        "Continue by reading docs/parser.md and explain what it means.",
        workspace=tmp_path,
    )

    assert state.task_contract == before


def test_no_command_followup_keeps_positive_path_before_test_freeze(tmp_path):
    """A later negative clause must not erase an earlier explicit write target."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState(initial_task_text="Fix the parser")
    instruction = (
        "Update app.py without modifying tests/test_app.py."
    )

    state.apply_current_round_instruction(instruction, workspace=tmp_path)
    state.apply_current_round_instruction(instruction, workspace=tmp_path)

    requirements = state.task_contract["requirements"]
    assert [item["expected_artifacts"] for item in requirements] == [["app.py"]]
    assert requirements[0]["kind"] == "artifact"
    assert state.forbids_test_changes is True

    negated_first = RuntimeState(initial_task_text="Fix the parser")
    negated_first.apply_current_round_instruction(
        "Don't edit README.md, but update app.py.",
        workspace=tmp_path,
    )
    assert [
        item["expected_artifacts"]
        for item in negated_first.task_contract["requirements"]
    ] == [["app.py"]]


def test_runtime_state_project_creation_guides_greenfield_flow():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.set_acceptance_criteria_from_text("帮我创建一个 FastAPI todo API 项目")
    state.turn_count = 6

    block = state.build_prompt_block()

    assert "analyze_project_requirements -> create_project_blueprint -> scaffold_project" in block
    assert "Do not start with grep_search unless you are intentionally reusing local code." in block


def test_strict_progress_emits_one_nudge_after_twelve_investigations():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.set_acceptance_criteria_from_text("Fix the parser bug")
    for index in range(12):
        state.observe_tool("grep_search", {"pattern": f"token-{index}"}, "match")

    first = state.build_prompt_block(strict=True)
    second = state.build_prompt_block(strict=True)

    assert "STRICT CONVERGENCE" in first
    assert "12 investigation calls" in first
    assert "STRICT CONVERGENCE" not in second


def test_strict_progress_keeps_investigation_available_after_twenty_calls():
    """A read-count threshold must never withdraw investigation tools."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    for index in range(20):
        state.observe_tool("read_file", {"path": f"file-{index}.py"}, "source")

    assert state.strict_progress_action("grep_search") == "allow"
    assert state.strict_progress_action("read_symbol") == "allow"
    assert state.strict_progress_action("edit_file") == "allow"
    assert state.strict_progress_action("diff_status") == "allow"


def test_strict_progress_keeps_soft_boundary_after_source_and_test_localization():
    """Localization must not turn the 12-call advisory into a hard rejection."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.set_acceptance_criteria_from_text("Fix the concat regression")
    state.observe_tool(
        "read_symbol",
        {"path": "xarray/core/concat.py", "symbol": "_dataset_concat"},
        "source",
    )
    state.observe_tool(
        "read_file",
        {"path": "xarray/tests/test_concat.py"},
        "<path>/workspace/xarray/tests/test_concat.py</path>\n"
        "<type>file</type>\n<content>tests</content>",
    )
    for index in range(10):
        state.observe_tool("grep_search", {"pattern": f"clue-{index}"}, "match")

    assert state.investigation_calls_since_edit == 12
    assert "STRICT CONVERGENCE" in state.build_prompt_block(strict=True)
    assert state.strict_progress_action("read_file") == "allow"
    assert state.strict_progress_action("edit_file") == "allow"


def test_nominal_closure_keeps_worker_investigation_surface_unrestricted():
    """Closure pressure is prompt guidance, not a read/search policy gate."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    for phase in ("closure_repair", "closure_finalize"):
        state = RuntimeState()
        state.work_phase = phase

        assert state.closure_phase_action(
            "read_file", {"path": "new/evidence.py"},
        ) == "allow"
        assert state.closure_phase_action(
            "grep_search", {"pattern": "clue", "path": "."},
        ) == "allow"
        assert state.closure_phase_action("repo_map", {"path": "."}) == "allow"
        assert state.closure_phase_action("task", {"prompt": "inspect evidence"}) == "allow"


def test_successful_file_scoped_grep_localizes_test_evidence():
    """A content grep of one test file is objective localization evidence."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.set_acceptance_criteria_from_text("Fix the stacked-array regression")
    state.observe_tool(
        "read_symbol",
        {"path": "xarray/core/dataarray.py", "symbol": "to_stacked_array"},
        "source",
    )
    state.observe_tool(
        "grep_search",
        {
            "pattern": "test_to_stacked_array",
            "path": "xarray/tests/test_dataarray.py",
            "output_mode": "content",
        },
        "Found 2 matches\n  Line 100: def test_to_stacked_array():",
        succeeded=True,
    )

    assert state.read_files == [
        "xarray/core/dataarray.py",
        "xarray/tests/test_dataarray.py",
    ]


def test_grep_does_not_localize_failed_or_directory_scoped_searches():
    """Only a successful content match against one file is read evidence."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool(
        "grep_search",
        {
            "pattern": "missing",
            "path": "xarray/tests/test_dataarray.py",
            "output_mode": "content",
        },
        "No files found",
        succeeded=True,
    )
    state.observe_tool(
        "grep_search",
        {
            "pattern": "test_",
            "path": "xarray/tests",
            "output_mode": "content",
        },
        "Found 10 matches",
        succeeded=True,
    )
    state.observe_tool(
        "grep_search",
        {
            "pattern": "test_",
            "path": "xarray/tests/test_dataset.py",
            "output_mode": "content",
        },
        "Error: Search timed out (30s)",
        succeeded=False,
    )

    assert state.read_files == []


def test_directory_reads_do_not_create_false_source_and_test_localization():
    """Directory listings are navigation, not evidence that files were read."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.set_acceptance_criteria_from_text("Fix the runtime regression")
    state.observe_tool(
        "read_file",
        {"path": "nz_coder"},
        "<path>/workspace/nz_coder</path>\n<type>directory</type>",
        succeeded=True,
    )
    state.observe_tool(
        "read_file",
        {"path": "tests/runtime"},
        "<path>/workspace/tests/runtime</path>\n<type>directory</type>",
        succeeded=True,
    )
    for index in range(10):
        state.observe_tool(
            "grep_search",
            {"pattern": f"clue-{index}", "path": "."},
            "Found 1 match",
            succeeded=True,
        )

    assert state.read_files == []
    assert state.strict_progress_action("read_file") == "allow"


def test_missing_symbol_and_repository_scope_are_not_exact_read_evidence():
    """Only successful reads of exact files may feed localization policy."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.set_acceptance_criteria_from_text("Fix the parser bug")
    state.observe_tool(
        "read_symbol",
        {"path": "src/parser.py", "symbol": "missing_parser"},
        "symbol 'missing_parser' not found in src/parser.py\n\nAvailable symbols:",
        succeeded=True,
    )
    state.observe_tool(
        "repo_map",
        {"path": "src"},
        "Source repository map\nfiles_scanned: 20",
        succeeded=True,
    )
    state.observe_tool(
        "code_references",
        {"path": "tests"},
        "3 references",
        succeeded=True,
    )

    assert state.read_files == []


def test_strict_progress_keeps_broad_limit_until_test_scope_is_known():
    """Source-only discovery may still need the original cross-repo allowance."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.set_acceptance_criteria_from_text("Fix the concat regression")
    state.observe_tool(
        "read_symbol",
        {"path": "xarray/core/concat.py", "symbol": "_dataset_concat"},
        "source",
    )
    for index in range(11):
        state.observe_tool("grep_search", {"pattern": f"clue-{index}"}, "match")

    assert state.investigation_calls_since_edit == 12
    assert state.strict_progress_action("read_file") == "allow"


def test_successful_write_resets_strict_investigation_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    for index in range(12):
        state.observe_tool("grep_search", {"pattern": f"token-{index}"}, "match")
    state.build_prompt_block(strict=True)

    state.observe_tool("edit_file", {"path": "parser.py"}, "Done")

    assert state.investigation_calls_since_edit == 0
    assert state.mutation_generation == 1
    assert state.strict_progress_nudges == 0
    assert state.strict_progress_blocks == 0
    assert state.strict_progress_action("grep_search") == "allow"


def test_strict_terminal_evidence_is_order_independent_within_generation():
    """Regression: verify-before-diff must settle the same mutation generation."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_tool("verify_changed_files", {}, "OK: py_compile changed files")

    assert state.strict_generation_terminal_ready() is False

    state.observe_tool(
        "diff_status",
        {},
        "\n".join([
            "has_non_empty_diff: true",
            "diff_chars: 20",
            "tests_modified: false",
            "source_only: true",
            "Changed files:",
            "  app.py",
        ]),
    )

    assert state.strict_generation_terminal_ready() is True


def test_new_mutation_invalidates_previous_generation_terminal_evidence():
    """A verification from generation one cannot finalize generation two."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_tool("diff_status", {}, "has_non_empty_diff: true\ntests_modified: false\nsource_only: true")
    state.observe_tool("verify_changed_files", {}, "OK: passed")
    assert state.strict_generation_terminal_ready() is True

    state.observe_tool("edit_file", {"path": "app.py"}, "Done")

    assert state.strict_generation_terminal_ready() is False


def test_requested_test_changes_can_settle_strict_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.wants_tests = True
    state.observe_tool("edit_file", {"path": "pkg/app.py"}, "Done")
    state.observe_tool(
        "diff_status",
        {},
        "\n".join([
            "has_non_empty_diff: true",
            "tests_modified: true",
            "source_only: false",
            "Changed files:",
            "  pkg/app.py",
            "  tests/test_app.py",
        ]),
    )
    state.observe_tool("verify_changed_files", {}, "OK: passed")

    assert state.diff_generation == state.mutation_generation
    assert state.strict_generation_terminal_ready() is True


def test_unrequested_test_changes_do_not_settle_strict_generation():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.wants_tests = False
    state.observe_tool("edit_file", {"path": "tests/test_app.py"}, "Done")
    state.observe_tool(
        "diff_status",
        {},
        "has_non_empty_diff: true\ntests_modified: true\nsource_only: false",
    )
    state.observe_tool("verify_changed_files", {}, "OK: passed")

    assert state.diff_generation == -1
    assert state.strict_generation_terminal_ready() is False


def test_read_only_bash_source_inspection_consumes_strict_investigation_budget():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("bash", {"command": "grep -n token pkg/app.py"}, "match")
    state.observe_tool("bash", {"command": "cat pkg/app.py"}, "source")

    assert state.investigation_calls_since_edit == 2
    state.investigation_calls_since_edit = 20
    assert state.strict_progress_action(
        "bash", tool_input={"command": "head -20 pkg/app.py"}
    ) == "allow"


def test_bash_status_and_verification_do_not_consume_investigation_budget():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=80)
    state.observe_tool("bash", {"command": "git status --short"}, "")
    state.observe_tool("bash", {"command": "python3 -m py_compile pkg/app.py"}, "")

    assert state.investigation_calls_since_edit == 0


def _state_with_current_verification_contract():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=30)
    state.set_acceptance_criteria_from_text(
        "完成后运行 python -m pytest -q cron_engine/tests。"
    )
    state.observe_tool("edit_file", {"path": "cron_engine/parser.py"}, "Done")
    state.has_diff = True
    return state


def test_model_bash_settles_current_verification_contract_success():
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()

    observation = state.observe_tool(
        "bash",
        {"command": "python  -m pytest -q 'cron_engine/tests'"},
        "59 passed in 0.20s",
        succeeded=True,
    )

    contract = VerificationContract.from_dict(state.verification_contract)
    assert observation == {
        "command": "python  -m pytest -q 'cron_engine/tests'",
        "output": "59 passed in 0.20s",
        "passed": True,
    }
    assert contract.attempted_generation == state.mutation_generation == 1
    assert contract.attempts == 1
    assert contract.passed is True
    assert state.verification_generation == 1
    assert state.changed_files_verified is True


def test_model_bash_records_failed_contract_without_settling_generation():
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()

    observation = state.observe_tool(
        "bash",
        {"command": "python -m pytest -q cron_engine/tests"},
        "1 failed, 58 passed",
        succeeded=False,
    )

    contract = VerificationContract.from_dict(state.verification_contract)
    assert observation["passed"] is False
    assert contract.attempted_generation == 1
    assert contract.attempts == 1
    assert contract.passed is False
    assert state.verification_generation == -1
    assert state.changed_files_verified is False


def test_nonmatching_or_synthetic_bash_does_not_settle_contract():
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()

    assert state.observe_tool(
        "bash",
        {"command": "python -m pytest -q other/tests"},
        "10 passed",
        succeeded=True,
    ) is None
    assert state.observe_tool(
        "bash",
        {
            "command": "python -m pytest -q cron_engine/tests",
            "_nz_runtime_contract": True,
        },
        "59 passed",
        succeeded=True,
    ) is None

    contract = VerificationContract.from_dict(state.verification_contract)
    assert contract.attempts == 0
    assert contract.attempted_generation == -1


def test_new_edit_rearms_model_settled_verification_contract():
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()
    state.observe_tool(
        "bash",
        {"command": "python -m pytest -q cron_engine/tests"},
        "59 passed",
        succeeded=True,
    )
    state.observe_tool("edit_file", {"path": "cron_engine/parser.py"}, "Done")

    contract = VerificationContract.from_dict(state.verification_contract)
    assert contract.is_due(
        zone="yellow",
        has_diff=state.has_diff,
        mutation_generation=state.mutation_generation,
    ) is False
    assert contract.is_due(
        zone="red",
        has_diff=state.has_diff,
        mutation_generation=state.mutation_generation,
    ) is True
    assert state.verification_generation != state.mutation_generation


def test_docs_edit_does_not_rearm_model_settled_verification_contract():
    """Exact code acceptance remains valid across an attributed docs-only edit."""
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()
    state.observe_tool(
        "bash",
        {"command": "python -m pytest -q cron_engine/tests"},
        "59 passed",
        succeeded=True,
    )
    state.observe_tool("edit_file", {"path": "docs/parser.md"}, "Done")

    contract = VerificationContract.from_dict(state.verification_contract)
    assert state.mutation_generation == 2
    assert state.acceptance_mutation_generation == 1
    assert contract.is_due(
        zone="completion",
        has_diff=state.has_diff,
        mutation_generation=state.acceptance_mutation_generation,
    ) is False


def test_docs_only_task_runs_declared_acceptance_once():
    """Generation zero remains a valid first acceptance scope for docs-only work."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = RuntimeState()
    state.reset(max_turns=30)
    state.set_acceptance_criteria_from_text(
        "Update docs/parser.md and run pytest -q tests/test_parser.py"
    )
    state.observe_tool("edit_file", {"path": "docs/parser.md"}, "Done")
    state.has_diff = True

    contract = VerificationContract.from_dict(state.verification_contract)
    assert state.acceptance_mutation_generation == 0
    assert contract.is_due(
        zone="completion",
        has_diff=True,
        mutation_generation=state.acceptance_mutation_generation,
    ) is True

    state.observe_tool(
        "bash",
        {"command": "pytest -q tests/test_parser.py"},
        "1 passed",
        succeeded=True,
    )

    contract = VerificationContract.from_dict(state.verification_contract)
    assert contract.attempted_generation == 0
    assert contract.attempts == 1
    assert contract.is_due(
        zone="completion",
        has_diff=True,
        mutation_generation=state.acceptance_mutation_generation,
    ) is False


def test_test_edit_rearms_model_settled_verification_contract():
    """Changing acceptance inputs must still invalidate an earlier test pass."""
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()
    state.observe_tool(
        "bash",
        {"command": "python -m pytest -q cron_engine/tests"},
        "59 passed",
        succeeded=True,
    )
    state.observe_tool(
        "edit_file",
        {"path": "cron_engine/tests/test_parser.py"},
        "Done",
    )

    contract = VerificationContract.from_dict(state.verification_contract)
    assert state.acceptance_mutation_generation == 2
    assert contract.is_due(
        zone="completion",
        has_diff=state.has_diff,
        mutation_generation=state.acceptance_mutation_generation,
    ) is True


def test_docs_edit_keeps_semantic_review_on_last_behavior_generation():
    """A Sidecar verdict after docs work must certify the unchanged code generation."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.reset(max_turns=30)
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Preserve parser compatibility",
        "requirements": [
            {
                "id": "R1",
                "description": "Preserve parser compatibility",
                "kind": "compatibility",
            },
            {
                "id": "R2",
                "description": "Pass parser acceptance",
                "kind": "verification",
            },
        ],
        "acceptance_commands": ["pytest -q tests/test_parser.py"],
    }))
    state.observe_tool("edit_file", {"path": "parser.py"}, "Done")
    state.observe_requirement_verification(
        "pytest -q tests/test_parser.py",
        passed=True,
        acceptance=True,
    )
    state.observe_tool("edit_file", {"path": "docs/parser.md"}, "Done")

    state.observe_requirement_semantic_review(
        accepted=True,
        fingerprint="accepted-after-docs",
    )

    assert state.requirement_ledger_snapshot().status("R1") == "satisfied"


def test_old_runtime_snapshot_migrates_acceptance_generation_conservatively():
    """Snapshots predating scoped generations must not reuse uncertain evidence."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    assert state.restore({
        "active": True,
        "mutation_generation": 5,
        "source_mutation_generation": 3,
    }) is True
    assert state.acceptance_mutation_generation == 5


def test_pathless_write_conservatively_invalidates_requirement_evidence():
    """An unattributed mutation cannot preserve code evidence as docs-only."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.reset(max_turns=30)
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Fix parser behavior",
        "requirements": [
            {
                "id": "R1",
                "description": "Fix parser behavior",
                "kind": "behavior",
                "expected_artifacts": ["parser.py"],
            },
            {
                "id": "R2",
                "description": "Pass parser acceptance",
                "kind": "verification",
            },
        ],
    }))
    state.observe_tool("edit_file", {"path": "parser.py"}, "Done")
    state.observe_requirement_verification(
        "pytest -q tests/test_parser.py",
        passed=True,
        acceptance=True,
    )

    state.observe_tool("scaffold_project", {}, "Done")

    assert state.acceptance_mutation_generation == 2
    assert state.requirement_ledger_snapshot().status("R1") == "candidate"
    assert state.requirement_ledger_snapshot().status("R2") == "candidate"


def test_current_generation_acceptance_pass_prompts_immediate_finalization():
    state = _state_with_current_verification_contract()
    state.observe_tool(
        "bash",
        {"command": "python -m pytest -q cron_engine/tests"},
        "59 passed",
        succeeded=True,
    )

    block = state.build_prompt_block()

    assert "DECLARED ACCEPTANCE PASSED" in block
    assert "Do not call more tools; give the final summary now." in block


def test_repeated_model_contract_command_is_idempotent_within_generation():
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()
    for _ in range(2):
        state.observe_tool(
            "bash",
            {"command": "python -m pytest -q cron_engine/tests"},
            "59 passed",
            succeeded=True,
        )

    contract = VerificationContract.from_dict(state.verification_contract)
    assert contract.attempts == 1


def test_runtime_state_tracks_open_todo_items_and_contract_readiness():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.reset(max_turns=20)
    state.observe_tool(
        "todo",
        {"items": []},
        "[x] inspect parser\n[>] add tests\n[ ] update README\n\n(1/3 completed)",
    )

    assert state.open_todo_items == 2
    assert state.verification_contract_ready("yellow") is False
    assert state.verification_contract_ready("red") is False
    assert state.verification_contract_ready("completion") is True

    state.observe_tool(
        "todo",
        {"items": []},
        "[x] inspect parser\n[x] add tests\n[x] update README\n\n(3/3 completed)",
    )

    assert state.open_todo_items == 0
    assert state.verification_contract_ready("yellow") is True


def test_intermediate_acceptance_pass_does_not_prompt_finalization_with_open_todos():
    state = _state_with_current_verification_contract()
    state.observe_tool(
        "todo",
        {"items": []},
        "[>] add tests\n[ ] update README\n\n(0/2 completed)",
    )
    state.observe_tool(
        "bash",
        {"command": "python -m pytest -q cron_engine/tests"},
        "59 passed",
        succeeded=True,
    )

    block = state.build_prompt_block()

    assert "INTERMEDIATE ACCEPTANCE PASSED" in block
    assert "2 Todo item(s) remain open" in block
    assert "Do not call more tools; give the final summary now." not in block


def test_budget_zone_runtime_acceptance_pass_is_intermediate_without_todos():
    from nz_coder.runtime.verification.verification_contract import VerificationContract

    state = _state_with_current_verification_contract()
    contract = VerificationContract.from_dict(state.verification_contract)
    contract.record_attempt(
        state.mutation_generation,
        passed=True,
        output="85 passed",
        source="runtime",
        zone="orange",
    )
    state.verification_contract = contract.to_dict()
    state.verification_generation = state.mutation_generation
    state.py_compile_ok = True
    state.changed_files_verified = True

    block = state.build_prompt_block()

    assert "INTERMEDIATE ACCEPTANCE PASSED" in block
    assert "budget-zone check, not a completion boundary" in block
    assert "Do not call more tools; give the final summary now." not in block


def test_runtime_state_aggregates_provider_usage_by_purpose_and_round_trips(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.observe_provider_call(
        "planning",
        provider_id="openai-responses",
        model_id="gpt-planner",
        usage={
            "input": 100,
            "output": 20,
            "total": 120,
            "reasoning": 5,
            "cache_read": 10,
            "cache_write": 0,
        },
        attempts=1,
        duration_ms=12.5,
        cost=0.012,
        cost_source="registry",
    )
    state.observe_provider_call(
        "coding",
        provider_id="anthropic",
        model_id="claude-coder",
        usage={
            "input": 200,
            "output": 30,
            "total": 230,
            "reasoning": 7,
            "cache_read": 0,
            "cache_write": 0,
        },
        attempts=2,
        duration_ms=20.0,
        cost=None,
        cost_source=None,
    )

    assert state.provider_calls == 2
    assert state.provider_attempts == 3
    assert state.provider_calls_by_purpose == {"planning": 1, "coding": 1}
    assert state.provider_usage["total"] == 350
    assert state.provider_usage_by_purpose["planning"]["input"] == 100
    assert state.provider_calls_by_model == {
        "openai-responses/gpt-planner": 1,
        "anthropic/claude-coder": 1,
    }
    assert state.provider_usage_by_model[
        "openai-responses/gpt-planner"
    ]["total"] == 120
    assert state.provider_duration_ms_by_purpose == {
        "planning": 12.5,
        "coding": 20.0,
    }
    assert state.provider_cost_usd == 0.012
    assert state.provider_cost_usd_by_purpose == {"planning": 0.012}
    assert state.provider_cost_usd_by_model == {
        "openai-responses/gpt-planner": 0.012,
    }
    assert state.provider_cost_unknown_calls == 1
    assert state.provider_cost_sources == {"registry": 1, "unknown": 1}

    path = tmp_path / "runtime-state.json"
    state.save(path)
    restored = RuntimeState()
    assert restored.load(path) is True
    assert restored.provider_usage_by_purpose == state.provider_usage_by_purpose
    assert restored.provider_usage_by_model == state.provider_usage_by_model
    assert restored.provider_cost_usd == state.provider_cost_usd
    assert restored.provider_cost_sources == state.provider_cost_sources


def test_runtime_state_sanitizes_corrupt_persisted_provider_costs():
    """A damaged/legacy state file must not poison later cost aggregation."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    assert state.restore({
        "active": True,
        "provider_calls": 4,
        "provider_cost_usd": float("nan"),
        "provider_cost_usd_by_purpose": {
            "coding": 0.125,
            "planning": float("inf"),
            "": 1.0,
            "review": -0.5,
            "bad": "not-a-number",
        },
        "provider_cost_usd_by_model": {
            "openai/gpt": 0.125,
            "anthropic/claude": float("nan"),
        },
        "provider_cost_unknown_calls": -3,
        "provider_cost_sources": {
            "registry": 1,
            "unknown": "2",
            "broken": -1,
            "": 7,
        },
    }) is True

    assert state.provider_cost_usd == 0.125
    assert state.provider_cost_usd_by_purpose == {"coding": 0.125}
    assert state.provider_cost_usd_by_model == {"openai/gpt": 0.125}
    assert state.provider_cost_unknown_calls == 0
    assert state.provider_cost_sources == {"registry": 1, "unknown": 2}

    state.observe_provider_call(
        "coding",
        provider_id="openai",
        model_id="gpt",
        usage={"input": 1, "output": 1, "total": 2},
        attempts=1,
        duration_ms=1.0,
        cost=0.025,
        cost_source="registry",
    )
    assert state.provider_cost_usd == 0.15


def test_runtime_state_sanitizes_corrupt_persisted_control_fields():
    """One damaged resume snapshot cannot poison prompt or policy arithmetic."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    assert state.restore({
        "active": True,
        "turn_count": float("nan"),
        "max_turns": "broken",
        "started_at": float("inf"),
        "mutation_generation": float("nan"),
        "source_mutation_generation": {},
        "acceptance_mutation_generation": float("inf"),
        "verification_generation": [],
        "changed_files": "src/app.py",
        "requested_paths": {"src/app.py": True},
        "task_contract": [],
        "requirement_ledger": "broken",
        "verification_contract": ["broken"],
        "patch_risk": {
            "requires_replan": True,
            "risk_signals": {"not": "a list"},
        },
        "scheduled_verification_generations": {
            "static": float("nan"),
            "targeted": "3",
        },
        "workspace_git_available": "yes",
    }) is True

    assert state.turn_count == 0
    assert state.max_turns == 80
    assert state.started_at == 0.0
    assert state.mutation_generation == 0
    assert state.source_mutation_generation == 0
    assert state.acceptance_mutation_generation == 0
    assert state.verification_generation == -1
    assert state.changed_files == []
    assert state.requested_paths == []
    assert state.task_contract == {}
    assert state.requirement_ledger == {}
    assert state.verification_contract == {}
    assert state.patch_risk["risk_signals"] == []
    assert state.scheduled_verification_generations == {"targeted": 3}
    assert state.workspace_git_available is None
    assert isinstance(state.build_prompt_block(strict=True), str)


def test_runtime_state_normalizes_nested_contract_counters_on_restore():
    """Valid-shaped nested state with corrupt numbers remains resumable."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    assert state.restore({
        "active": True,
        "task_contract": {
            "objective": "repair parser",
            "requirements": [],
            "contract_version": float("nan"),
        },
        "requirement_ledger": {
            "items": [],
            "latest_generation": float("nan"),
            "latest_verification_generation": float("inf"),
        },
        "verification_contract": {
            "command": "pytest -q tests/test_parser.py",
            "attempted_generation": float("nan"),
            "attempts": float("inf"),
            "passed": False,
        },
    }) is True

    assert state.task_contract["contract_version"] == 1
    assert state.requirement_ledger["latest_generation"] == 0
    assert state.requirement_ledger["latest_verification_generation"] == -1
    assert state.verification_contract["attempted_generation"] == -1
    assert state.verification_contract["attempts"] == 0
    assert isinstance(state.requirement_ledger_snapshot().to_dict(), dict)


def test_runtime_state_sanitizes_provider_ledger_and_rejects_private_restore():
    """Untrusted persisted JSON must not replace locks or crash accounting."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    assert state.restore({
        "active": True,
        "_provider_accounting_lock": "not-a-lock",
        "provider_calls": "2",
        "provider_attempts": -4,
        "provider_calls_by_purpose": {"coding": "2", "bad": -1},
        "provider_calls_by_model": "not-a-map",
        "provider_usage": {
            "input": "10",
            "output": 2,
            "total": float("nan"),
            "unknown": 999,
        },
        "provider_usage_by_purpose": {
            "coding": {"input": 10, "output": "2", "total": 12},
            "broken": "not-usage",
        },
        "provider_usage_by_model": [],
        "provider_duration_ms_by_purpose": {
            "coding": 12.5,
            "bad": float("inf"),
        },
        "provider_duration_ms_by_model": {"openai/gpt": -1},
        "provider_turn_records": ["bad", {"turn": "3", "reason": "coding"}],
        "provider_turns_by_reason": {"coding": "1", "bad": -1},
        "provider_turns_by_outcome": None,
    }) is True

    assert state.provider_calls == 2
    assert state.provider_attempts == 2
    assert state.provider_calls_by_purpose == {"coding": 2}
    assert state.provider_calls_by_model == {}
    assert state.provider_usage == {
        "input": 10,
        "output": 2,
        "total": 12,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    assert state.provider_usage_by_purpose == {
        "coding": {
            "input": 10,
            "output": 2,
            "total": 12,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        },
    }
    assert state.provider_usage_by_model == {}
    assert state.provider_duration_ms_by_purpose == {"coding": 12.5}
    assert state.provider_duration_ms_by_model == {}
    assert state.provider_turn_records == [{
        "turn": 3,
        "reason": "coding",
        "outcome": "unknown",
        "tool_names": [],
        "finish_reason": "",
        "mutation_generation_before": 0,
        "mutation_generation_after": 0,
        "mutation_delta": 0,
        "verification_generation_after": -1,
    }]
    assert state.provider_turns_by_reason == {"coding": 1}
    assert state.provider_turns_by_outcome == {}

    # This enters the run-local lock and proves the private JSON key was ignored.
    state.observe_provider_call(
        "coding",
        provider_id="openai",
        model_id="gpt",
        usage={"input": 1, "output": 1, "total": 2},
        attempts=1,
        duration_ms=1.0,
    )
    assert state.provider_calls == 3


def test_provider_observer_never_crashes_on_malformed_telemetry():
    """Best-effort telemetry cannot mask an otherwise settled Provider call."""
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()

    state.observe_provider_call(
        "coding",
        provider_id="third-party",
        model_id="compatible-model",
        usage={
            "input": float("nan"),
            "output": "bad",
            "total": float("inf"),
            "reasoning": True,
        },
        attempts=float("nan"),
        duration_ms=float("inf"),
        cost=float("nan"),
        cost_source="provider",
    )

    assert state.provider_calls == 1
    assert state.provider_attempts == 1
    assert state.provider_usage == {
        "input": 0,
        "output": 0,
        "total": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    assert state.provider_duration_ms_by_purpose == {"coding": 0.0}
    assert state.provider_cost_usd == 0.0
    assert state.provider_cost_unknown_calls == 1


def test_runtime_state_serializes_concurrent_provider_observers():
    """Parallel Sidecar completions must not lose usage increments."""
    import threading

    from nz_coder.runtime.execution.runtime_state import RuntimeState

    first_get = threading.Event()
    second_get = threading.Event()

    class RacingDict(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == "stall_sidecar":
                if not first_get.is_set():
                    first_get.set()
                    second_get.wait(timeout=0.2)
                else:
                    second_get.set()
            return value

    state = RuntimeState()
    state.provider_calls_by_purpose = RacingDict()

    def observe():
        state.observe_provider_call(
            "stall_sidecar",
            provider_id="deepseek",
            model_id="v4-flash",
            usage={"input": 10, "output": 2, "total": 12},
            attempts=1,
            duration_ms=5.0,
        )

    threads = [threading.Thread(target=observe) for _index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert state.provider_calls == 2
    assert state.provider_calls_by_purpose["stall_sidecar"] == 2
    assert state.provider_usage["total"] == 24


def test_runtime_state_records_bounded_provider_turn_ledger_and_round_trips(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    for turn in range(1, 206):
        state.observe_provider_turn({
            "turn": turn,
            "reason": "investigation" if turn < 3 else "implementation",
            "outcome": "investigation_batch" if turn < 3 else "mutation_batch",
            "tool_names": ["read_file"] if turn < 3 else ["apply_patch"],
        })

    assert len(state.provider_turn_records) == 200
    assert state.provider_turn_records[0]["turn"] == 6
    assert state.provider_turn_records[-1]["turn"] == 205
    assert state.provider_turns_by_reason == {
        "investigation": 2,
        "implementation": 203,
    }
    assert state.provider_turns_by_outcome == {
        "investigation_batch": 2,
        "mutation_batch": 203,
    }

    path = tmp_path / "runtime-state.json"
    state.save(path)
    restored = RuntimeState()
    assert restored.load(path) is True
    assert restored.provider_turn_records == state.provider_turn_records
    assert restored.provider_turns_by_reason == state.provider_turns_by_reason
    assert restored.provider_turns_by_outcome == state.provider_turns_by_outcome


def test_legacy_bounded_emergency_state_is_advisory_only():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.work_phase = "bounded_emergency"
    state.has_diff = True
    state.changed_files = ["cron_engine/parser.py"]
    state.requested_paths = ["cron_engine/tests/test_parser.py"]
    state.verification_contract = {
        "command": "python -m pytest -q cron_engine/tests",
    }

    assert state.closure_phase_action(
        "read_file", {"path": "cron_engine/parser.py"},
    ) == "allow"
    assert state.closure_phase_action(
        "edit_file", {"path": "cron_engine/parser.py"},
    ) == "allow"
    assert state.closure_phase_action(
        "write_file", {"path": "unrelated.py"},
    ) == "allow"
    assert state.closure_phase_action("list_directory", {"path": "."}) == "allow"
    assert state.closure_phase_action(
        "grep_search", {"pattern": "Cron", "path": "."},
    ) == "allow"
    assert state.closure_phase_action("task", {"prompt": "explore"}) == "allow"
    assert state.closure_phase_action(
        "bash", {"command": "python -m pip install pytest"},
    ) == "allow"
    assert state.package_install_attempts == 0
    assert state.emergency_broad_exploration == 0
    assert state.closure_phase_action(
        "bash", {"command": "python -m pytest -q cron_engine/tests"},
    ) == "allow"
    assert state.closure_phase_action(
        "bash",
        {
            "command": "python -c \"import cron_engine; print(cron_engine.__file__)\"",
            "_nz_runtime_contract": True,
        },
    ) == "allow"
    assert state.closure_phase_action(
        "bash",
        {
            "command": "python -c \"print('probe')\"",
            "_nz_runtime_verification_stage": "static",
        },
    ) == "allow"


def test_runtime_emergency_eligibility_requires_failure_and_known_target():
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.has_diff = True
    state.changed_files = ["cron_engine/parser.py"]
    state.verification_failures = 1

    assert state.emergency_eligibility().eligible is True

    state.changed_files = []
    assert state.emergency_eligibility().eligible is False
