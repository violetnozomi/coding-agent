"""Source-parity contracts derived from InfCodeX workflow runtime semantics."""
from __future__ import annotations

import pytest

from nz_coder.runtime.workflows.workflow_contracts import workflow_contract
from nz_coder.runtime.workflows.workflow_runtime import lint_workflow_plan


def test_contract_is_defensive_and_declares_terminal_semantics():
    first = workflow_contract()
    first["phase_modes"].append("unsafe")
    second = workflow_contract()

    assert second["version"] == "1.6"
    assert "unsafe" not in second["phase_modes"]
    assert second["terminal_events"] == [
        "workflow_run_completed",
        "workflow_run_failed",
        "workflow_run_stopped",
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("failure_semantics", "ordinary_child_failure"), "null-result"),
        (("failure_semantics", "structural_failure"), "stop-workflow"),
        (("failure_semantics", "verifier_transport_failure"), "fail-open"),
        (("cache_semantics", "synthesis_replayed"), False),
        (("resource_semantics", "agent_cap_includes_synthesis"), True),
        (("outcome_semantics", "raw_child_output_persisted"), False),
    ],
)
def test_contract_exposes_source_parity_decisions(path, expected):
    value = workflow_contract()
    for key in path:
        value = value[key]
    assert value == expected


def test_process_isolation_is_preflighted():
    plan = {
        "phases": [{
            "name": "inspect",
            "mode": "parallel",
            "tasks": [{
                "prompt": "inspect",
                "read_only": True,
                "isolation": "container",
            }],
        }],
    }

    findings = lint_workflow_plan(plan, remaining_agents=2)

    assert any(item.code == "invalid-isolation" for item in findings)


def test_contract_declares_nested_review_sweep_and_generator_semantics():
    contract = workflow_contract()

    assert {"workflow", "quality_gate"} <= set(contract["phase_modes"])
    assert contract["resource_semantics"]["nested_workflow_depth"] == 1
    assert contract["resource_semantics"]["nested_runtime_resources"] == "shared-with-parent"
    assert contract["capsule_semantics"]["resolution_precedence"][0] == "builtin"
    assert contract["review_semantics"]["silent_approval"] is False
    assert contract["worktree_semantics"]["changed_worktree_retained"] is True
    assert len(contract["generator_semantics"]["patterns"]) == 6


def test_contract_declares_host_launch_and_identity_semantics():
    host = workflow_contract()["host_semantics"]

    assert host["invocation"] == {
        "command": "suggest",
        "natural-language": "none",
    }
    assert host["turn_consumed_by"] == ["started", "cancelled"]
    assert host["limit_precedence"] == "minimum-of-manifest-host-system"
    assert host["display_alias_ambiguity"] == "fail-closed"
    assert host["resume_target"] == "run-id-or-unique-display-name"


def test_contract_declares_approval_lifecycle_generation_and_resilience():
    contract = workflow_contract()

    assert contract["host_semantics"]["stale_approval"] == "fail-closed"
    assert contract["lifecycle_semantics"]["saved_delete"] == "recoverable-trash"
    assert contract["lifecycle_semantics"]["retention_preview"] is True
    assert contract["generation_semantics"]["executable_source_allowed"] is False
    assert contract["generation_semantics"]["repair_attempts"] == 2
    assert contract["resilience_semantics"]["tool_name_fuzzy_matching"] is False
    assert contract["resilience_semantics"]["terminal_signals"] == [
        "COMPLETE", "BLOCKED", "DECIDE",
    ]
