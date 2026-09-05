"""Tests for the model-callable Plan/Build mode workflow."""
from __future__ import annotations

import json

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.agent.subagent import _subagent_tools
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import dispatch, get_execution_mode, get_specs
from nz_coder.tools.plan_mode import (
    PlanModeController,
    scoped_plan_mode_controller,
)


def _answering(*labels: str):
    answers = iter(labels)

    def ask(_questions):
        return [[next(answers)]]

    return ask


def _controller(tmp_path, *, mode="auto", answers=()):
    permissions = PermissionManager(mode)
    controller = PlanModeController(
        permissions,
        session_id="test-plan",
        question_asker=_answering(*answers) if answers else None,
    )
    return permissions, controller


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_plan_tools_are_registered_serial_and_hidden_from_subagents():
    names = {item["function"]["name"] for item in get_specs()}
    child_names = {
        item["function"]["name"]
        for item in _subagent_tools("general-purpose")
    }

    assert {"plan_enter", "write_plan", "plan_exit"}.issubset(names)
    assert all(
        get_execution_mode(name) == "serial"
        for name in ("plan_enter", "write_plan", "plan_exit")
    )
    assert not {"plan_enter", "write_plan", "plan_exit"} & child_names
    assert all(
        PermissionManager("acceptEdits").check(name, {})["behavior"] == "allow"
        for name in ("plan_enter", "write_plan", "plan_exit")
    )


def test_plan_tools_without_bound_controller_fail_without_blocking():
    assert dispatch("plan_enter", {"reason": "complex change"}).startswith(
        "Error: Plan mode service unavailable"
    )
    assert dispatch("write_plan", {"content": "# Plan"}).startswith(
        "Error: Plan mode service unavailable"
    )
    assert dispatch("plan_exit", {"title": "Plan", "summary": "- Step"}).startswith(
        "Error: Plan mode service unavailable"
    )


def test_plan_enter_decline_keeps_build_mode_and_does_not_create_plan(tmp_path):
    with scoped_workdir(tmp_path):
        permissions, controller = _controller(
            tmp_path,
            answers=("Continue Build",),
        )

        result = controller.enter("Research a multi-file change")

        assert result.startswith("Plan mode was not entered")
        assert permissions.mode == "auto"
        assert not controller.plan_path.exists()


def test_plan_enter_write_and_approve_are_a_deferred_transition(tmp_path):
    with scoped_workdir(tmp_path):
        permissions, controller = _controller(
            tmp_path,
            answers=(
                "Switch to Plan (Recommended)",
                "Approve Plan (Recommended)",
            ),
        )

        entered = controller.enter("Research a multi-file change")
        assert "Plan mode is active" in entered
        assert permissions.mode == "plan"
        assert controller.plan_path.read_text(encoding="utf-8") == ""
        assert permissions.check(
            "write_file", {"path": "app.py", "content": "x"}
        )["behavior"] == "deny"

        written = controller.write("# Implementation plan\n\n1. Change parser")
        assert "Plan updated" in written
        assert controller.plan_path.read_text(encoding="utf-8").endswith("\n")

        approved = controller.exit("Parser update", "- Change parser\n- Run tests")
        assert "Plan approved" in approved
        assert "sha256:" in approved
        assert approved.metadata["plan_exit_approved"] is True
        assert approved.metadata["title"] == "Parser update"
        assert permissions.mode == "plan"

        assert controller.apply_pending_mode() == ("plan", "auto")
        assert permissions.mode == "auto"
        assert controller.apply_pending_mode() is None


def test_plan_exit_requires_content_and_rejection_stays_read_only(tmp_path):
    with scoped_workdir(tmp_path):
        permissions, controller = _controller(
            tmp_path,
            mode="plan",
            answers=("Keep Planning",),
        )
        controller.plan_path.parent.mkdir(parents=True, exist_ok=True)
        controller.plan_path.write_text("", encoding="utf-8")

        empty = controller.exit("Plan", "- Step")
        assert empty == "Error: plan file is empty; call write_plan before plan_exit"

        controller.write("# Plan\n\n1. Step")
        rejected = controller.exit("Plan", "- Step")

        assert rejected.startswith("Plan was not approved")
        assert permissions.mode == "plan"
        assert controller.apply_pending_mode() is None


def test_plan_exit_can_continue_implementation_in_the_same_session(tmp_path):
    """Approval and implementation continuation are separate user choices."""
    with scoped_workdir(tmp_path):
        permissions, controller = _controller(
            tmp_path,
            mode="plan",
            answers=("Implement in This Session",),
        )
        controller.write("# Plan\n\n1. Implement\n2. Verify")

        result = controller.exit("Ready", "- Implement\n- Verify")

        assert result.metadata["plan_exit_approved"] is True
        assert result.metadata["plan_exit_terminal"] is False
        assert controller.apply_pending_mode() == ("plan", "default")
        assert permissions.mode == "default"


def test_plan_edit_during_approval_requires_a_fresh_review(tmp_path):
    with scoped_workdir(tmp_path):
        permissions = PermissionManager("plan")
        controller = None

        def edit_then_approve(_questions):
            controller.plan_path.write_text("# Changed plan\n", encoding="utf-8")
            return [["Approve Plan (Recommended)"]]

        controller = PlanModeController(
            permissions,
            session_id="test-plan",
            question_asker=edit_then_approve,
        )
        controller.write("# Original plan")

        result = controller.exit("Plan", "- Step")

        assert result.startswith("Plan changed during approval")
        assert permissions.mode == "plan"
        assert controller.apply_pending_mode() is None


def test_plan_mode_prompt_exposes_only_the_dedicated_write_exception(tmp_path):
    with scoped_workdir(tmp_path):
        _permissions, controller = _controller(tmp_path, mode="plan")

        block = controller.prompt_block()

        assert "Plan mode is ACTIVE" in block
        assert "write_plan" in block
        assert "user-state://plans/test-plan.md" in block
        assert "Do not modify source files" in block


def test_active_plan_boundary_is_injected_as_a_system_message(tmp_path):
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "BASE SYSTEM",
            permission_mode="plan",
            client=object(),
            trace_enabled=False,
        )

        api_messages = agent._build_api_messages([
            {"role": "user", "content": "Create a plan"},
        ])

        assert api_messages[0]["role"] == "system"
        assert "BASE SYSTEM" in api_messages[0]["content"]
        assert "<plan-mode>" in api_messages[0]["content"]
        assert "write_plan" in api_messages[0]["content"]


def test_active_plan_tool_exposure_hides_non_planning_side_effects(tmp_path):
    """Blocked tools should not consume schema budget or tempt invalid calls."""
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "BASE SYSTEM",
            permission_mode="plan",
            client=object(),
            trace_enabled=False,
        )

        names = {
            spec["function"]["name"] for spec in agent._active_tool_specs()
        }

        assert {"read_file", "write_plan", "plan_exit", "question", "todo"} <= names
        assert not {"write_file", "task", "agent_manager", "workflow_save"} & names


def test_plan_exit_then_write_in_same_batch_keeps_write_blocked(tmp_path):
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test",
            permission_mode="plan",
            client=object(),
            trace_enabled=False,
            question_asker=_answering("Approve Plan (Recommended)"),
        )
        assert not agent.plan_mode.write("# Plan\n\n1. Implement").startswith("Error:")
        calls = [
            _tool_call(
                "exit",
                "plan_exit",
                {"title": "Ready", "summary": "- Implement"},
            ),
            _tool_call(
                "write",
                "write_file",
                {"path": "should-not-exist.py", "content": "unsafe"},
            ),
        ]
        messages = []

        with scoped_plan_mode_controller(agent.plan_mode):
            agent._execute_tools(calls, messages)

        outputs = [item["content"] for item in messages if item.get("role") == "tool"]
        assert outputs[0].startswith("Plan approved")
        assert outputs[1].startswith("Denied: Plan mode")
        assert not (tmp_path / "should-not-exist.py").exists()
        assert agent.permissions.mode == "default"
