"""Real bash progress and result payloads cannot bypass output admission."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import nz_coder.tools.bash  # noqa: F401 -- register the real shell tool

from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime
from nz_coder.runtime.agent.guardrails import ToolGuardrail
from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec
from nz_coder.runtime.agent.agent_transition_runtime import (
    ProductionAgentTransitionRuntime,
)
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.session.tool_progress import ToolSessionBoundary
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.state.tool_ledger import ToolLedger

from nz_coder.runtime.tool_runtime.result_projection import (
    ProductionToolResultProjector,
)
from nz_coder.tool_platform.execution import ToolExecutionResult

from .support import Dependencies, call
from .test_component_contract import components


@pytest.mark.parametrize("action", ["allow", "rewrite", "block"])
def test_real_bash_progress_waits_for_output_admission(tmp_path, action):
    # Keep the marker out of argv/authorized input, so it can only come from output.
    (tmp_path / "payload.txt").write_text("UNADMITTED_SENTINEL", encoding="utf-8")
    (tmp_path / "emit.py").write_text(
        "from pathlib import Path\nprint(Path('payload.txt').read_text())\n",
        encoding="utf-8",
    )
    with components(tmp_path, True) as (dependencies, context):
        events, saves, admitted = [], [], []
        dependencies.processor = SessionProcessor(
            dependencies.messages[-1],
            publish=lambda event, data: events.append((event, copy.deepcopy(data))),
        )
        selected = call("bash", "call-bash", command="python emit.py")
        dependencies.prepare([selected])
        original_checkpoint = context.lifecycle.checkpoint

        async def persist(messages, status):
            saves.append(copy.deepcopy(messages))
            await original_checkpoint(messages, status)

        async def verdict(selected, result, agent_context):
            assert "UNADMITTED_SENTINEL" in result["content"]
            assert "UNADMITTED_SENTINEL" not in json.dumps(events + saves)
            admitted.append(True)
            if action == "rewrite":
                return {"action": action, "payload": {"content": "ADMITTED"}}
            return {"action": action, "reason": "contract policy"}

        graph = AgentGraph(
            [
                AgentSpec(
                    "coder",
                    "Contract",
                    guardrails=(ToolGuardrail("contract", after_tool=verdict),),
                ),
            ],
            "coder",
        )
        guardrails = ProductionGuardrailRuntime()
        # Existing guardrail API takes an outer policy host, never passed to tools.
        host = SimpleNamespace(
            agent_graph=graph,
            current_agent_name="coder",
            tracer=SimpleNamespace(log=dependencies.trace),
        )

        async def after(selected, result, messages):
            return await guardrails.after_tool(host, selected, result, messages)

        async def run():
            boundary = ToolSessionBoundary(persist, None, lambda event, data: None)
            selected_context = replace(
                context,
                lifecycle=replace(
                    context.lifecycle,
                    checkpoint=boundary.checkpoint,
                    drain_progress=boundary.drain_progress,
                    metadata_reporter=boundary.metadata_reporter,
                    after_tool=after,
                ),
            )
            result = await ProductionToolRuntime().execute_batch_async(
                selected_context,
                [selected],
                dependencies.messages,
            )
            assert boundary._pending == []
            return result

        assert asyncio.run(run()) == "continue"
        assert admitted == [True]
        durable = asyncio.run(
            LegacyJsonSessionStore().load(context.run.session.identity, tmp_path)
        )
        assert durable is not None
        ledger = ToolLedger(tmp_path).executions("session-contract")
        assert len(ledger) == 1 and ledger[0]["execution_state"] == "succeeded"
        visible = json.dumps(events + saves + durable.transcript + ledger)
        assert ("UNADMITTED_SENTINEL" in visible) == (action == "allow")
        if action == "rewrite":
            assert "ADMITTED" in visible
        parts = [
            p
            for p in dependencies.processor.message["_nz_parts"]
            if p["type"] == "tool"
        ]
        assert parts[0]["state"]["status"] == (
            "error" if action == "block" else "completed"
        )


@pytest.mark.parametrize("kind", ["plan", "handoff", "terminal", "child_cost"])
@pytest.mark.parametrize("action", ["rewrite", "block", "rewrite_error"])
def test_content_verdict_preserves_only_valid_success_control_facts(kind, action):
    dependencies = Dependencies()
    edges = (HandoffSpec("worker"),) if kind == "handoff" else ()
    guard = ToolGuardrail(
        "contract",
        after_tool=lambda *_: (
            {"action": "block", "reason": "contract"}
            if action == "block"
            else {
                "action": "rewrite",
                "payload": {
                    "content": "ADMITTED",
                    "is_error": action == "rewrite_error",
                },
            }
        ),
    )
    graph = AgentGraph(
        [
            AgentSpec("coder", "Contract", handoffs=edges, guardrails=(guard,)),
            AgentSpec("worker", "Worker"),
        ],
        "coder",
    )
    host = SimpleNamespace(
        agent_graph=graph,
        current_agent_name="coder",
        tracer=SimpleNamespace(log=dependencies.trace),
    )
    selected = call(
        {"plan": "plan_exit", "child_cost": "task"}.get(kind, "emit_handoff")
    )
    metadata = {
        "plan_exit_terminal": True,
        "plan_exit_approved": True,
        "handoffTarget": "worker" if kind == "handoff" else "",
        "isTerminal": kind == "terminal",
        "child_cost_delta": 1.25,
        "output": "UNADMITTED_SENTINEL",
        "handoffInput": "UNADMITTED_SENTINEL",
        "terminalSummary": "UNADMITTED_SENTINEL",
        "custom": {"payload": "UNADMITTED_SENTINEL"},
    }
    result = ToolExecutionResult(
        selected["function"]["name"],
        {},
        "UNADMITTED_SENTINEL",
        True,
        False,
        False,
        False,
        title="UNADMITTED_SENTINEL",
        metadata=metadata,
        attachments=[{"url": "UNADMITTED_SENTINEL"}],
    )
    result = asyncio.run(
        ProductionGuardrailRuntime().after_tool(
            host, selected, result, dependencies.messages
        )
    )
    assert result.executed and (result.dispatch_failed == (action != "rewrite"))
    assert "UNADMITTED_SENTINEL" not in repr(result)
    assert result.title == "" and result.attachments == []
    dependencies.prepare([selected])
    projection = replace(
        dependencies.context().projection,
        signal_from_metadata=lambda metadata: (
            ProductionAgentTransitionRuntime().signal_from_metadata(host, metadata)
        ),
    )
    state = ProductionToolResultProjector().consume(
        projection,
        [(0, selected, result)],
        dependencies.messages,
        processor=dependencies.processor,
    )
    if kind == "plan":
        assert state["terminal"] == (action == "rewrite")
    elif kind == "child_cost":
        assert result.metadata.get("child_cost_delta") == (
            1.25 if action == "rewrite" else None
        )
    else:
        signal = state["handoff_signal"]
        assert (signal is not None) == (action == "rewrite")
        if signal is not None:
            assert signal.summary == "ADMITTED"
            assert signal.terminal == (kind == "terminal")
            assert signal.target == ("worker" if kind == "handoff" else "")
