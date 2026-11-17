"""路线元数据不能取得交付证据或父子执行权限。"""
from __future__ import annotations

import pytest

from nz_coder.intelligence.reviewer import review_run_evidence
from nz_coder.runtime.agent.subagent import _subagent_tools
from nz_coder.runtime.verification.sidecar_verifier import (
    VerifierGateMetrics, compose_gate_decision, _transcript_has_tool_use,
)
from test_working_memory_reminders import _call
from test_working_plan_state import PLAN


def test_route_only_call_preserves_default_terminal_review_gate():
    transcript = ({"role": "user", "content": "Explain the issue."},
                  {"role": "assistant", "content": "", "tool_calls": [
                      _call(1, "update_working_plan", {"plan": PLAN, "expected_revision": 0})]})
    assert _transcript_has_tool_use(transcript) is False
    assert compose_gate_decision(transcript, VerifierGateMetrics(), env={}) == (True, "default-fire")


@pytest.mark.parametrize("real_read", [False, True])
def test_route_history_requires_real_business_evidence(real_read):
    calls = [_call(1, "update_working_plan", {"plan": PLAN, "expected_revision": 0})]
    if real_read:
        calls.append(_call(2, "read_file", {"path": "evidence.txt"}))
    transcript = ({"role": "user", "content": "Inspect."},
                  {"role": "assistant", "tool_calls": calls},
                  {"role": "user", "content": "Do not call tools and do not modify files. Report the verification result already obtained in the previous turn."})
    assert compose_gate_decision(transcript, VerifierGateMetrics(), env={}) == (
        (False, "grounded-history-report") if real_read else (True, "default-fire"))


@pytest.mark.parametrize("kind", ["explore", "plan", "reflection", "general-purpose"])
def test_child_tool_catalog_never_exposes_parent_route_update(kind):
    import nz_coder.tools.working_plan  # noqa: F401
    names = {spec["function"]["name"] for spec in _subagent_tools(kind)}
    assert "update_working_plan" not in names
    requested = _subagent_tools(kind, allowed_tools=["update_working_plan"])
    assert "update_working_plan" not in {spec["function"]["name"] for spec in requested}


@pytest.mark.parametrize("reported_review", [False, True])
def test_static_risk_is_visible_without_inventing_required_plan_rewrite(reported_review):
    review = review_run_evidence({
        "modified_files": ["pkg/api.py"],
        "verification_results": [{"status": "passed", "summary": "OK"}],
        "impact_review": {"risk": "high", "requires_replan": True,
                          "risk_signals": [{"category": "public_api_change"}]}},
        runtime={"has_diff": True, "patch_risk_reviewed": reported_review})
    assert review["review_status"] == "approved_with_limitations"
    assert any("public_api_change" in item for item in review["limitations"])
    assert review["required_next_steps"] == []
