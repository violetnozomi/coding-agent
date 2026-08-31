"""Contracts for the canonical child-Agent terminal result envelope."""
from __future__ import annotations

import json


def test_child_result_round_trip_preserves_explicit_null_structured_value():
    from nz_coder.runtime.agent.child_result import ChildAgentResult

    result = ChildAgentResult.from_dict({
        "task_id": "task-1",
        "name": "reviewer",
        "status": "completed",
        "final_text": "review complete",
        "structured": None,
        "usage": {"input": 10, "output": 3},
    })

    assert result.structured_present is True
    assert result.to_dict()["structured"] is None
    assert result.to_dict()["usage"] == {"input": 10, "output": 3}


def test_child_result_bounds_persisted_text_and_marks_truncation():
    from nz_coder.runtime.agent.child_result import ChildAgentResult

    result = ChildAgentResult.from_dict({
        "task_id": "task-1",
        "name": "worker",
        "status": "completed",
        "final_text": "x" * 20_000,
    })

    assert len(result.final_text) == 16_000
    assert result.final_text_truncated is True


def test_child_result_ignores_nonfinite_usage_and_non_boolean_flags():
    """Corrupt persisted metrics must not prevent a child from settling."""
    from nz_coder.runtime.agent.child_result import ChildAgentResult

    result = ChildAgentResult.from_dict({
        "task_id": "task",
        "name": "worker",
        "status": "completed",
        "final_text": "done",
        "usage": {
            "input": float("nan"),
            "output": float("inf"),
            "total": 12.7,
            "cached": -2,
            "flag": True,
        },
        "limit_reached": "false",
        "interrupted": 1,
    })

    assert result.usage == {"total": 12, "cached": 0}
    assert result.limit_reached is False
    assert result.interrupted is False


def test_child_result_repairs_nonfinite_nested_payloads_for_workflow_storage():
    """Structured child evidence must remain strict JSON across cache/resume."""
    from nz_coder.runtime.agent.child_result import ChildAgentResult

    result = ChildAgentResult.from_dict({
        "task_id": "task",
        "name": "worker",
        "status": "completed",
        "final_text": "done",
        "structured": {"score": float("nan")},
        "verification": {"duration": float("inf")},
        "route_facts": {"latency": -float("inf")},
        "conflicts": [{"weight": float("nan")}],
    })

    payload = result.to_dict()
    assert payload["structured"] == {"score": None}
    assert payload["verification"] == {"duration": None}
    assert payload["route_facts"] == {"latency": None}
    assert payload["conflicts"] == [{"weight": None}]
    json.dumps(payload, allow_nan=False)


def test_child_result_metadata_keeps_legacy_projection():
    from nz_coder.runtime.agent.child_result import CHILD_RESULT_KEY, ChildAgentResult

    metadata = ChildAgentResult.from_dict({
        "task_id": "child-1",
        "name": "worker",
        "status": "completed",
        "final_text": "done",
        "session_id": "child-1",
        "agent_id": "agent-1",
        "changed_files": ["app.py"],
        "verification": {"status": "passed", "summary": "tests passed"},
    }).to_metadata()

    assert metadata[CHILD_RESULT_KEY]["final_text"] == "done"
    assert metadata["child_session_id"] == "child-1"
    assert metadata["child_changed_files"] == ["app.py"]
    assert metadata["child_verification"] == "tests passed"


def test_child_result_adapts_legacy_tool_metadata():
    from nz_coder.runtime.agent.child_result import ChildAgentResult

    result = ChildAgentResult.from_metadata(
        {
            "child_session_id": "legacy-1",
            "child_agent_id": "agent-1",
            "child_status": "completed",
            "child_changed_files": ["old.py"],
        },
        final_text="legacy result",
        name="explore",
    )

    assert result is not None
    assert result.task_id == "legacy-1"
    assert result.final_text == "legacy result"
    assert result.changed_files == ("old.py",)


def test_child_result_from_state_projects_runtime_owners():
    from nz_coder.runtime.agent.child_result import child_result_from_state

    result = child_result_from_state(
        {
            "session_id": "child-2",
            "agent_id": "agent-2",
            "agent_type": "general-purpose",
            "parent_session_id": "parent",
            "provider_id": "openai-compatible",
            "model_id": "model-x",
            "structured_output": {"fixed": True},
            "changed_files": ["src/app.py"],
            "tokens": {"total": 12},
            "cost_known": True,
            "cost": 0.25,
            "messages": [{"role": "assistant", "content": "fixed"}],
            "route_facts": {
                "requested_tier": "deep",
                "tier_outcome": "applied",
            },
        },
        final_text="fixed",
        status="completed",
        verification="pytest passed",
    )

    payload = result.to_dict()
    assert payload["structured"] == {"fixed": True}
    assert payload["provider"] == "openai-compatible"
    assert payload["model"] == "model-x"
    assert payload["verification"]["status"] == "passed"
    assert payload["cost"] == 0.25
    assert payload["route_facts"]["iterations"] == 1
    assert payload["route_facts"]["requested_tier"] == "deep"
