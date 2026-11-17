"""调查停滞后，动作交接要求应留在后续请求中，直到实际行动改变状态。"""
from __future__ import annotations

import pytest

from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
from nz_coder.runtime.execution.runtime_state import RuntimeState
from tests.runtime.test_request_snapshot import _host


@pytest.mark.parametrize("strict", [False, True])
def test_convergence_guidance_names_existing_handoff_without_blocking_reads(strict):
    state = RuntimeState(initial_task_text="Repair parser behavior")
    state.reset(max_turns=80)
    for index in range(12):
        state.observe_tool("grep_search", {"pattern": str(index)}, "source evidence")

    first = state.build_prompt_block(strict=strict)
    assert "update_scratchpad" in first and "next_action" in first
    assert "category='plan'" in first and "tool call IDs" in first
    assert "verification_command" in first and "separate" in first
    assert "next_action" in state.build_prompt_block(strict=strict)
    assert state.strict_progress_nudges == 1
    for index in range(12):
        state.observe_tool("grep_search", {"pattern": f"more-{index}"}, "missing evidence")
    assert state.strict_progress_action("grep_search") == "allow"
    assert state.strict_progress_action("read_file") == "allow"
    assert not state.execution_handoff


def test_ignored_handoff_guidance_survives_the_next_request(monkeypatch):
    host, _tools, _hooks = _host(monkeypatch)
    state = RuntimeState(initial_task_text="Repair parser behavior", max_turns=80)
    host.runtime_state = state
    host._implementation_bundle_block = lambda _query, **_kwargs: ""
    builder = ProductionPromptBuilder()
    messages = [{"role": "user", "content": state.initial_task_text}]
    for index in range(12):
        state.observe_tool("grep_search", {"pattern": str(index)}, "source evidence")
    first = builder.freeze(host, messages)
    assert "next_action" in str(first.project(messages))
    state.observe_tool("read_file", {"path": "parser.py"}, "more source evidence", succeeded=True)
    messages.append({"role": "assistant", "content": "Continue investigating parser details."})
    following = builder.freeze(host, messages)
    assert "next_action" in str(following.project(messages))
    assert state.strict_progress_nudges == 1
    assert state.strict_progress_action("read_file") == "allow"
    state.observe_tool("edit_file", {"path": "parser.py"}, "Updated parser", succeeded=True)
    assert "next_action" not in str(builder.freeze(host, messages).project(messages))
    assert state.investigation_calls_since_edit == 0


def test_handoff_guidance_reaches_actual_request_at_existing_threshold(monkeypatch):
    from nz_coder.state.context import PromptBudget
    from nz_coder.tools import get_specs
    from nz_coder.tools.scratchpad import Scratchpad
    import nz_coder.tools.scratchpad  # noqa: F401

    host, _tools, _hooks = _host(monkeypatch)
    host._sp = Scratchpad(persistent=False)
    host._prompt_budget = lambda: PromptBudget(100000, 1000, 99000, 80000, 0, 0, 0, False)
    state = RuntimeState(initial_task_text="Repair parser behavior", max_turns=80)
    host.runtime_state = state
    host._implementation_bundle_block = lambda _query, **_kwargs: ""
    host._active_tool_specs = lambda: [spec for spec in get_specs()
                                     if spec["function"]["name"] in {"read_file", "update_scratchpad"}]
    builder = ProductionPromptBuilder()
    messages = [{"role": "user", "content": state.initial_task_text}]
    for index in range(11):
        state.observe_tool("grep_search", {"pattern": str(index)}, "source evidence")
    before = builder.build(host, messages)
    assert "next_action" not in str(before)
    state.observe_tool("grep_search", {"pattern": "last-clue"}, "source evidence")
    snapshot = builder.freeze(host, messages)
    wire = snapshot.project(messages)
    assert "next_action" in str(wire) and "update_scratchpad" in str(wire)
    assert any("next_action" in spec["function"]["parameters"]["properties"]
               for spec in snapshot.tools if spec["function"]["name"] == "update_scratchpad")
    assert state.strict_progress_nudges == 1
    assert not state.execution_handoff
