"""Contracts for provider-neutral Agent structured output."""
from __future__ import annotations

import asyncio

import pytest

from tests.test_loop_fake import FakeClient, FakeMessage, FakeResponse, FakeToolCall


FINDING_SCHEMA = {
    "type": "object",
    "required": ["lens", "findings"],
    "properties": {
        "lens": {"type": "string", "enum": ["correctness", "security"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _agent(schema=FINDING_SCHEMA):
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec

    return AgentGraph([
        AgentSpec(
            "reviewer",
            "Review the requested evidence.",
            allowed_tools=(),
            output_schema=schema,
        ),
    ], start="reviewer")


def test_extract_prefers_last_fenced_json_and_handles_nested_shapes():
    from nz_coder.runtime.conversation.structured_output import extract_json_candidate

    text = (
        "draft ```json\n{\"old\": true}\n```\n"
        "final ```json\n{\"items\": [{\"value\": \"} inside string\"}]}\n```"
    )

    assert extract_json_candidate(text) == (
        '{"items": [{"value": "} inside string"}]}'
    )


def test_schema_validator_reports_nested_required_and_extra_fields():
    from nz_coder.runtime.conversation.structured_output import validate_against_schema

    errors = validate_against_schema(
        {
            "lens": "correctness",
            "findings": [{"extra": 1}],
            "unexpected": True,
        },
        FINDING_SCHEMA,
    )

    assert "findings[0].path: required field is missing" in errors
    assert (
        "findings[0].extra: unexpected property "
        "(additionalProperties is false)"
    ) in errors
    assert (
        "unexpected: unexpected property (additionalProperties is false)"
    ) in errors


def test_schema_declaration_rejects_silently_unsupported_constraints():
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec

    with pytest.raises(ValueError, match="unsupported.*minimum"):
        AgentGraph([
            AgentSpec(
                "reviewer",
                "review",
                allowed_tools=(),
                output_schema={
                    "type": "object",
                    "properties": {"score": {"type": "number", "minimum": 0}},
                },
            ),
        ], start="reviewer")


def test_schema_declaration_rejects_nonstandard_json_numbers():
    from nz_coder.runtime.conversation.structured_output import assert_supported_output_schema

    with pytest.raises(ValueError, match="JSON serializable"):
        assert_supported_output_schema({"type": "number", "default": float("nan")})


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_structured_output_rejects_nonstandard_json_numbers(constant: str):
    """A permissive Python parser must not widen the wire JSON contract."""
    from nz_coder.runtime.conversation.structured_output import evaluate_structured_output

    evaluated = evaluate_structured_output(
        f'{{"value": {constant}}}',
        {"type": "object"},
    )

    assert evaluated.ok is False
    assert "valid JSON" in evaluated.errors[0]


def test_output_schema_is_owned_by_terminal_agent():
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec

    with pytest.raises(ValueError, match="terminal owner"):
        AgentGraph([
            AgentSpec(
                "reviewer",
                "review",
                allowed_tools=(),
                output_schema={"type": "object"},
                handoffs=(HandoffSpec("owner"),),
            ),
            AgentSpec("owner", "own", allowed_tools=()),
        ], start="reviewer")


def test_declared_agent_surfaces_valid_structured_output(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.composition import build_declared_agent
    from nz_coder.runtime.conversation.structured_output import STRUCTURED_OUTPUT_KEY
    from nz_coder.runtime.process.workdir import scoped_workdir

    fake = FakeClient([FakeResponse(FakeMessage(
        'Review complete.\n```json\n'
        '{"lens":"correctness","findings":[{"path":"app.py"}]}\n```'
    ))])
    messages = [{"role": "user", "content": "inspect"}]
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_declared_agent(
                _agent(),
                client=fake,
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    expected = {
        "lens": "correctness",
        "findings": [{"path": "app.py"}],
    }
    assert result["status"] == "completed"
    assert result["structured"] == expected
    assert result["runtime"]["structured_output"]["ok"] is True
    assistant = next(item for item in messages if item.get("role") == "assistant")
    assert assistant[STRUCTURED_OUTPUT_KEY] == expected
    assert "## Required Output Format" in fake.chat.completions.requests[0]["messages"][0]["content"]


def test_invalid_output_gets_one_seeded_no_tool_repair(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.composition import build_declared_agent
    from nz_coder.runtime.conversation.structured_output import (
        STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir

    fake = FakeClient([
        FakeResponse(FakeMessage("prose without json")),
        FakeResponse(FakeMessage(
            '```json\n{"lens":"security","findings":[]}\n```'
        )),
    ])
    messages = [{"role": "user", "content": "inspect"}]
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_declared_agent(
                _agent(),
                client=fake,
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    assert fake.chat.completions.calls == 2
    repair_request = fake.chat.completions.requests[1]
    assert repair_request["tools"] == []
    assert repair_request["messages"][0]["content"] == (
        STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT
    )
    assert any(
        item.get("_nz_structured_output_repair") is True
        for item in messages
    )
    assert result["structured"] == {"lens": "security", "findings": []}
    assert result["runtime"]["structured_output"] == {
        "ok": True,
        "errors": [],
        "repaired": True,
    }


def test_invalid_repair_does_not_publish_unvalidated_structured_value(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.composition import build_declared_agent
    from nz_coder.runtime.process.workdir import scoped_workdir

    fake = FakeClient([
        FakeResponse(FakeMessage("no json")),
        FakeResponse(FakeMessage('```json\n{"lens":"wrong"}\n```')),
    ])
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_declared_agent(
                _agent(),
                client=fake,
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(
                [{"role": "user", "content": "inspect"}],
                stream=False,
            ))
    finally:
        config.WORKDIR = old_workdir

    assert fake.chat.completions.calls == 2
    assert "structured" not in result
    assert result["runtime"]["structured_output"]["ok"] is False
    assert result["runtime"]["structured_output"]["repaired"] is True


def test_invalid_terminal_signal_repairs_before_settling(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.composition import build_declared_agent
    from nz_coder.runtime.process.workdir import scoped_workdir

    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
            "emit_handoff",
            {"terminal": True, "summary": "not json"},
        )])),
        FakeResponse(FakeMessage(
            '```json\n{"lens":"correctness","findings":[]}\n```'
        )),
    ])
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_declared_agent(
                _agent(),
                client=fake,
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(
                [{"role": "user", "content": "inspect"}],
                stream=False,
            ))
    finally:
        config.WORKDIR = old_workdir

    assert fake.chat.completions.calls == 2
    assert result["status"] == "completed"
    assert result["structured"] == {"lens": "correctness", "findings": []}
    assert not any(entry["type"] == "terminal" for entry in agent.lineage.entries())


def test_as_tool_result_carries_validated_structured_payload_to_caller(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.composition import build_declared_agent
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec
    from nz_coder.runtime.conversation.structured_output import STRUCTURED_OUTPUT_KEY
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = AgentGraph([
        AgentSpec(
            "caller",
            "delegate",
            allowed_tools=(),
            handoffs=(HandoffSpec("reviewer", kind="as-tool"),),
        ),
        AgentSpec(
            "reviewer",
            "review",
            allowed_tools=(),
            output_schema=FINDING_SCHEMA,
        ),
    ], start="caller")
    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
            "emit_handoff",
            {"target": "reviewer", "summary": "review app.py"},
        )])),
        FakeResponse(FakeMessage(
            '```json\n{"lens":"security","findings":[{"path":"app.py"}]}\n```'
        )),
        FakeResponse(FakeMessage("caller accepted the review")),
    ])
    messages = [{"role": "user", "content": "inspect"}]
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_declared_agent(
                graph,
                client=fake,
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    returned = next(
        item for item in messages if item.get("_nz_agent_result") is True
    )
    assert result["status"] == "completed"
    assert returned[STRUCTURED_OUTPUT_KEY] == {
        "lens": "security",
        "findings": [{"path": "app.py"}],
    }
    assert returned["_nz_child_result"]["name"] == "reviewer"
    assert returned["_nz_child_result"]["status"] == "completed"
    assert returned["_nz_child_result"]["summary_kind"] == "excerpt"
    assert returned["_nz_child_result"]["structured"] == {
        "lens": "security",
        "findings": [{"path": "app.py"}],
    }
