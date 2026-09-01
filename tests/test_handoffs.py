"""Tests for declarative InfCodeX-style Agent handoffs."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.test_loop_fake import FakeClient, FakeMessage, FakeResponse, FakeToolCall


def _graph(input_filter=None):
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec

    return AgentGraph(
        [
            AgentSpec(
                name="scout",
                instructions="SCOUT_ROLE",
                allowed_tools=("repo_map",),
                handoffs=(HandoffSpec(
                    target="worker",
                    description="Implement the selected fix.",
                    input_filter=input_filter,
                ),),
            ),
            AgentSpec(name="worker", instructions="WORKER_ROLE", allowed_tools=("write_file",)),
        ],
        start="scout",
    )


def test_agent_graph_rejects_unknown_duplicate_and_cyclic_edges():
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec

    with pytest.raises(ValueError, match="unknown handoff target"):
        AgentGraph([
            AgentSpec("a", "A", handoffs=(HandoffSpec("missing"),)),
        ], start="a")
    with pytest.raises(ValueError, match="duplicate target"):
        AgentGraph([
            AgentSpec("a", "A", handoffs=(HandoffSpec("b"), HandoffSpec("b"))),
            AgentSpec("b", "B"),
        ], start="a")
    with pytest.raises(ValueError, match="contains a cycle"):
        AgentGraph([
            AgentSpec("a", "A", handoffs=(HandoffSpec("b"),)),
            AgentSpec("b", "B", handoffs=(HandoffSpec("a"),)),
        ], start="a")


def test_handoff_tool_enforces_declared_edges_and_terminal_roles():
    graph = _graph()
    current = "scout"
    handler = graph.tool_definition(lambda: current)["handler"]

    invalid = handler(target="scout")
    handoff = handler(target="worker", summary="ready")
    premature = handler(terminal=True)

    assert str(invalid).startswith("Error: undeclared handoff")
    assert handoff.metadata["handoffTarget"] == "worker"
    assert premature.startswith("Error: only an Agent with no declared handoffs")

    current = "worker"
    terminal = handler(terminal=True, summary="implemented")
    assert terminal.metadata == {
        "isTerminal": True,
        "handoffSource": "worker",
        "terminalSummary": "implemented",
    }


def test_agent_loop_switches_prompt_persists_part_and_honors_terminal_signal(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall("emit_handoff", {"target": "worker", "summary": "scouting done"}),
        ])),
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall("emit_handoff", {"terminal": True, "summary": "work done"}, call_id="call_2"),
        ])),
    ])
    transitions = []
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop(
                "unused",
                permission_mode="auto",
                client=fake,
                trace_enabled=False,
                agent_graph=_graph(),
                on_agent_switched=lambda event: transitions.append(event),
            )
            messages = [{"role": "user", "content": "inspect then implement"}]
            visible = []
            result = asyncio.run(
                agent.run(messages, stream=False, on_text=visible.append)
            )
    finally:
        config.WORKDIR = old_workdir

    assert result["status"] == "completed"
    assert visible == ["work done"]
    assert fake.chat.completions.calls == 2
    assert fake.chat.completions.requests[0]["messages"][0]["content"].startswith("SCOUT_ROLE")
    assert fake.chat.completions.requests[1]["messages"][0]["content"].startswith("WORKER_ROLE")
    first_tools = {
        item["function"]["name"]
        for item in fake.chat.completions.requests[0]["tools"]
    }
    second_tools = {
        item["function"]["name"]
        for item in fake.chat.completions.requests[1]["tools"]
    }
    assert first_tools == {"repo_map", "emit_handoff"}
    assert second_tools == {"write_file", "emit_handoff"}
    assert transitions[0]["from"] == "scout"
    assert transitions[0]["to"] == "worker"
    first_assistant = next(item for item in messages if item.get("role") == "assistant")
    handoff_part = next(part for part in first_assistant["_nz_parts"] if part["type"] == "handoff")
    assert handoff_part["from"] == "scout"
    assert handoff_part["to"] == "worker"
    lineage_entries = agent.lineage.entries()
    assert [entry["type"] for entry in lineage_entries[:4]] == [
        "run_started", "handoff", "terminal", "run_finished",
    ]
    assert lineage_entries[2]["payload"]["summary"] == "work done"
    assert lineage_entries[3]["payload"]["status"] == "completed"


def test_handoff_input_filter_receives_copy_and_controls_next_visible_history(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.session.session_processor import SessionProcessor
    from nz_coder.runtime.process.workdir import scoped_workdir

    observed = []

    def keep_last(history):
        observed.extend(history)
        history[0]["content"] = "snapshot-only mutation"
        return history[-2:]

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop("unused", client=FakeClient([]), trace_enabled=False, agent_graph=_graph(keep_last))
            messages = [
                {"role": "user", "content": "original"},
                {"role": "assistant", "content": "handoff", "_nz_message_id": "msg-a", "_nz_parts": []},
                {"role": "tool", "tool_call_id": "call-1", "content": "ready"},
            ]
            processor = SessionProcessor(messages[1])
            signal = agent._tool_handoff_signal({"handoffTarget": "worker"})
            transition = agent._apply_handoff_signal(signal, messages, processor)
    finally:
        config.WORKDIR = old_workdir

    assert transition["to"] == "worker"
    assert len(messages) == 2
    assert all(item["content"] != "snapshot-only mutation" for item in messages)
    assert observed[0]["content"] == "snapshot-only mutation"


def test_session_lineage_is_append_only_and_recovers_interrupted_agent(tmp_path):
    from nz_coder.runtime.agent.lineage import SessionLineage

    path = tmp_path / "lineage.jsonl"
    lineage = SessionLineage(path, "session-1")
    started = lineage.append("run_started", {"agent": "scout"})
    switched = lineage.append("handoff", {"from": "scout", "to": "worker"})

    assert switched["parent_id"] == started["id"]
    assert lineage.recover_active_agent("scout") == "worker"
    assert path.stat().st_mode & 0o777 == 0o600

    restored = SessionLineage(path, "session-1")
    assert restored.entries() == lineage.entries()
    restored.append("run_finished", {"status": "interrupted"})
    assert restored.recover_active_agent("scout") == "scout"


def test_session_lineage_ignores_only_a_truncated_tail(tmp_path):
    from nz_coder.runtime.agent.lineage import SessionLineage

    path = tmp_path / "lineage.jsonl"
    lineage = SessionLineage(path, "session-1")
    lineage.append("run_started", {"agent": "worker"})
    with path.open("ab") as handle:
        handle.write(b'{"partial":')

    restored = SessionLineage(path, "session-1")
    assert len(restored.entries()) == 1

    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid lineage entry"):
        SessionLineage(path, "session-1")


def test_session_lineage_persists_strict_json_and_rejects_legacy_nan(tmp_path):
    from nz_coder.runtime.agent.lineage import SessionLineage

    path = tmp_path / "lineage.jsonl"
    lineage = SessionLineage(path, "session-1")
    entry = lineage.append("run_started", {
        "usage": [float("nan"), float("inf")],
    })

    assert entry["payload"] == {"usage": [None, None]}
    json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )

    path.write_text(
        path.read_text(encoding="utf-8").replace("null", "NaN", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid lineage entry"):
        SessionLineage(path, "session-1")


def test_agent_call_stack_store_is_atomic_private_and_validated(tmp_path):
    from nz_coder.runtime.agent.lineage import AgentCallStackStore

    path = tmp_path / "agent-call-stack.json"
    store = AgentCallStackStore(path, "session-1")
    frames = [{
        "agent": "caller",
        "target": "helper",
        "messages": [{"role": "user", "content": "inspect"}],
    }]

    store.save(frames)

    assert store.load() == frames
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".agent-call-stack.json.*.tmp"))

    path.write_text('{"version":1,"session_id":"other","frames":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid Agent call stack envelope"):
        store.load()


def test_agent_call_stack_store_normalizes_nonfinite_values(tmp_path):
    from nz_coder.runtime.agent.lineage import AgentCallStackStore

    path = tmp_path / "agent-call-stack.json"
    store = AgentCallStackStore(path, "session-1")
    store.save([{
        "agent": "caller",
        "target": "helper",
        "messages": [{"role": "user", "content": "inspect", "score": float("nan")}],
    }])

    assert store.load()[0]["messages"][0]["score"] is None
    json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def test_memory_outcome_lineage_entries_are_idempotent(tmp_path):
    from nz_coder.runtime.agent.lineage import SessionLineage

    lineage = SessionLineage(tmp_path / "lineage.jsonl", "session-1")
    first = lineage.append_unique(
        "memory_review_receipt",
        "review-1",
        {"status": "completed", "proposal_ids": ["memory-a"]},
    )
    duplicate = lineage.append_unique(
        "memory_review_receipt",
        "review-1",
        {"status": "completed", "proposal_ids": ["memory-b"]},
    )

    assert first is not None
    assert duplicate is None
    assert len(lineage.entries()) == 1
    assert lineage.entries()[0]["payload"]["proposal_ids"] == ["memory-a"]


def test_agent_tool_guardrail_rejects_hidden_tool_before_dispatch(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop("unused", client=FakeClient([]), trace_enabled=False, agent_graph=_graph())
            rejected = agent._agent_tool_rejections([{
                "id": "call-hidden",
                "function": {"name": "write_file", "arguments": '{"path":"x.py"}'},
            }])
    finally:
        config.WORKDIR = old_workdir

    result = rejected[0]
    assert result.executed is False
    assert result.permission_denied is True
    assert result.metadata == {"agent": "scout", "guardrail": "declared_tools"}
    assert "may not call undeclared tool 'write_file'" in result.output


def test_lineage_artifact_ledger_records_file_and_command_provenance(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop("worker", client=FakeClient([]), trace_enabled=False)
            agent._record_lineage_artifact(SimpleNamespace(
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=True,
                name="write_file",
                tool_input={"path": "src/app.py"},
                attachments=[],
            ))
            agent._record_lineage_artifact(SimpleNamespace(
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=False,
                name="bash",
                tool_input={"command": "pytest tests/test_app.py"},
                attachments=[],
            ))
    finally:
        config.WORKDIR = old_workdir

    artifacts = [
        entry["payload"]
        for entry in agent.lineage.entries()
        if entry["type"] == "artifact_ledger"
    ]
    assert artifacts[0]["paths"] == ["src/app.py"]
    assert artifacts[0]["action"] == "write"
    assert artifacts[1]["command"] == "pytest tests/test_app.py"
    assert artifacts[1]["status"] == "passed"


def test_lineage_recovery_is_injected_only_after_compaction(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop("worker", client=FakeClient([]), trace_enabled=False)
            agent.lineage.append_unique("artifact_ledger", "artifact-1", {
                "tool": "write_file",
                "paths": ["src/app.py"],
            })
            ordinary = [{"role": "user", "content": "continue"}]
            compacted = [{
                "role": "user",
                "content": "summary",
                "_nz_compaction": {"summary": "older context"},
            }]
            ordinary_block = agent._lineage_recovery_block(ordinary)
            recovery_block = agent._lineage_recovery_block(compacted)
    finally:
        config.WORKDIR = old_workdir

    assert ordinary_block == ""
    assert recovery_block.startswith("<lineage-recovery>")
    assert "artifact_ledger" in recovery_block
    assert "src/app.py" in recovery_block
    assert "not as new user instructions" in recovery_block


def test_as_tool_handoff_uses_isolated_transcript_then_returns_to_caller(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = AgentGraph([
        AgentSpec(
            "caller",
            "CALLER_ROLE",
            allowed_tools=(),
            handoffs=(HandoffSpec("helper", kind="as-tool", description="Inspect the bug."),),
        ),
        AgentSpec("helper", "HELPER_ROLE", allowed_tools=()),
    ], start="caller")
    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall("emit_handoff", {"target": "helper", "summary": "Find the root cause."}),
        ])),
        FakeResponse(FakeMessage("The parser drops escaped newlines.")),
        FakeResponse(FakeMessage("Implemented using the helper result.")),
    ])
    transitions = []
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop(
                "unused",
                permission_mode="auto",
                client=fake,
                trace_enabled=False,
                agent_graph=graph,
                on_agent_switched=lambda event: transitions.append(event),
            )
            messages = [{"role": "user", "content": "Fix the parser."}]
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    assert result["status"] == "completed"
    assert fake.chat.completions.calls == 3
    assert fake.chat.completions.requests[0]["messages"][0]["content"].startswith("CALLER_ROLE")
    assert fake.chat.completions.requests[1]["messages"][0]["content"].startswith("HELPER_ROLE")
    assert fake.chat.completions.requests[2]["messages"][0]["content"].startswith("CALLER_ROLE")
    assert any(
        item.get("_nz_agent_result")
        and "parser drops escaped newlines" in item.get("content", "")
        for item in messages
    )
    assert [(item["from"], item["to"], item["kind"]) for item in transitions] == [
        ("caller", "helper", "as-tool"),
        ("helper", "caller", "as-tool-return"),
    ]


def test_as_tool_handoff_recovers_caller_after_process_crash(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = AgentGraph([
        AgentSpec(
            "caller",
            "CALLER_ROLE",
            handoffs=(HandoffSpec("helper", kind="as-tool"),),
        ),
        AgentSpec("helper", "HELPER_ROLE"),
    ], start="caller")
    session_id = "session-crash-recovery"
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            before_crash = AgentLoop(
                "unused",
                client=FakeClient([]),
                trace_enabled=False,
                agent_graph=graph,
                session_id=session_id,
            )
            caller_messages = [{"role": "user", "content": "Fix the parser."}]
            before_crash._init_run(caller_messages, False)
            signal = before_crash._tool_handoff_signal({
                "handoffTarget": "helper",
                "handoffInput": "Inspect parser state.",
            })
            before_crash._apply_handoff_signal(signal, caller_messages, None)

            resumed = AgentLoop(
                "unused",
                client=FakeClient([]),
                trace_enabled=False,
                agent_graph=graph,
                session_id=session_id,
            )
            helper_messages = [{"role": "user", "content": "resume helper"}]
            resumed._init_run(helper_messages, False)

            assert resumed.current_agent_name == "helper"
            assert resumed._agent_call_stack[0]["agent"] == "caller"
            resumed._return_from_as_tool(helper_messages, "Root cause found.")
            assert resumed.current_agent_name == "caller"
            assert resumed.agent_call_stack_store.load() == []
            assert any(
                item.get("_nz_agent_result") and "Root cause found" in item["content"]
                for item in helper_messages
            )
            resumed._finish_lineage("completed", helper_messages)
    finally:
        config.WORKDIR = old_workdir


def test_agent_handoff_switches_declared_model_and_reasoning_effort(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = AgentGraph([
        AgentSpec(
            "caller",
            "CALLER_ROLE",
            model="gpt-4o",
            handoffs=(HandoffSpec("helper", kind="as-tool"),),
        ),
        AgentSpec(
            "helper",
            "HELPER_ROLE",
            model="gpt-5",
            effort="medium",
        ),
    ], start="caller")
    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall("emit_handoff", {"target": "helper", "summary": "reason deeply"}),
        ])),
        FakeResponse(FakeMessage("Found the cause.")),
        FakeResponse(FakeMessage("Done.")),
    ])
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop(
                "unused",
                client=fake,
                trace_enabled=False,
                agent_graph=graph,
            )
            result = asyncio.run(agent.run(
                [{"role": "user", "content": "Investigate."}],
                stream=False,
            ))
    finally:
        config.WORKDIR = old_workdir

    assert result["status"] == "completed"
    assert [request["model"] for request in fake.chat.completions.requests] == [
        "gpt-4o",
        "gpt-5",
        "gpt-4o",
    ]
    helper_request = fake.chat.completions.requests[1]
    assert helper_request["reasoning_effort"] == "medium"
    assert "HELPER_ROLE" in helper_request["messages"][0]["content"]


def test_agent_input_and_output_guardrails_rewrite_authoritative_transcript(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.guardrails import InputGuardrail, OutputGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    def rewrite_input(messages, _context):
        rewritten = list(messages)
        rewritten[-1] = {"role": "user", "content": "guarded input"}
        return {"action": "rewrite", "payload": rewritten}

    async def rewrite_output(message, _context):
        return {
            "action": "rewrite",
            "payload": {"role": "assistant", "content": message["content"].upper()},
        }

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER_ROLE",
            guardrails=(
                InputGuardrail("normalize-input", rewrite_input),
                OutputGuardrail("normalize-output", rewrite_output),
            ),
        ),
    ], start="worker")
    fake = FakeClient([FakeResponse(FakeMessage("guarded output"))])
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            messages = [{"role": "user", "content": "raw input"}]
            agent = AgentLoop(
                "unused", client=fake, trace_enabled=False, agent_graph=graph,
            )
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    assert result["status"] == "completed"
    assert messages[0]["content"] == "guarded input"
    assert messages[-1]["content"] == "GUARDED OUTPUT"
    assert fake.chat.completions.requests[0]["messages"][-1]["content"].endswith(
        "guarded input"
    )


def test_agent_tool_guardrail_blocks_call_and_model_can_recover(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.guardrails import ToolGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    seen_agents = []

    def block_tool(call, context):
        seen_agents.append(context["agent"].name)
        return {"action": "block", "reason": f'{call["function"]["name"]} denied'}

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER_ROLE",
            allowed_tools=("list_directory",),
            guardrails=(ToolGuardrail("read-policy", before_tool=block_tool),),
        ),
    ], start="worker")
    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall("list_directory", {"path": "."}),
        ])),
        FakeResponse(FakeMessage("Recovered from denial.")),
    ])
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            messages = [{"role": "user", "content": "Inspect."}]
            agent = AgentLoop(
                "unused",
                client=fake,
                trace_enabled=False,
                agent_graph=graph,
                permission_mode="auto",
            )
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    assert result["status"] == "completed"
    assert seen_agents == ["worker"]
    tool_message = next(message for message in messages if message.get("role") == "tool")
    assert tool_message["content"] == 'Tool blocked by guardrail "read-policy".'
    assert "list_directory denied" not in repr(messages)


def test_tool_guardrail_selects_current_agent_after_handoff() -> None:
    """Tool policy follows the active Agent while input/output stay start-owned."""
    from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime
    from nz_coder.runtime.agent.guardrails import ToolGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec

    seen = []

    def observe(label):
        def check(_call, context):
            seen.append((label, context["agent"].name))
            return {"action": "allow"}

        return check

    graph = AgentGraph([
        AgentSpec(
            "scout",
            "SCOUT_ROLE",
            guardrails=(ToolGuardrail("scout-tools", before_tool=observe("scout")),),
        ),
        AgentSpec(
            "worker",
            "WORKER_ROLE",
            guardrails=(ToolGuardrail("worker-tools", before_tool=observe("worker")),),
        ),
    ], start="scout")
    host = SimpleNamespace(
        agent_graph=graph,
        current_agent_name="worker",
        auto_mode_controller=None,
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
    )

    guarded, rejected = asyncio.run(ProductionGuardrailRuntime().before_tool(
        host,
        {
            "id": "call-current",
            "function": {"name": "read_file", "arguments": {"path": "app.py"}},
        },
        [],
    ))

    assert guarded["id"] == "call-current"
    assert rejected is None
    assert seen == [("worker", "worker")]


def test_agent_reasoning_profile_escalates_real_provider_effort_once(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentReasoningProfile, AgentSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER_ROLE",
            model="gpt-5",
            reasoning=AgentReasoningProfile(
                default="balanced",
                max="deep",
                escalate_on_revise=True,
            ),
        ),
    ], start="worker")
    fake = FakeClient([FakeResponse(FakeMessage("done"))])
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = AgentLoop(
                "unused", client=fake, trace_enabled=False, agent_graph=graph,
            )
            assert agent.model_variant == "medium"
            assert agent._escalate_agent_reasoning("replan") is True
            assert agent._escalate_agent_reasoning("replan-again") is False
            assert agent.model_variant == "high"
            agent._call_non_streaming([{"role": "user", "content": "continue"}])
    finally:
        config.WORKDIR = old_workdir

    assert fake.chat.completions.requests[0]["reasoning_effort"] == "high"
