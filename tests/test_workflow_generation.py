"""Strict data-only workflow generation protocol tests."""
from __future__ import annotations

import json

import pytest


def test_generation_extracts_fenced_or_surrounded_json():
    from nz_coder.runtime.workflows.workflow_generation import extract_generation_json

    assert json.loads(extract_generation_json('```json\n{"action":"decline","reason":"simple"}\n```'))["action"] == "decline"
    assert json.loads(extract_generation_json('prefix {"action":"decline","reason":"simple"} suffix'))["reason"] == "simple"


def test_generation_decline_requires_reason():
    from nz_coder.runtime.workflows.workflow_generation import parse_workflow_generation

    assert parse_workflow_generation(
        '{"action":"decline","reason":"one Agent is sufficient"}'
    ) == {"kind": "declined", "reason": "one Agent is sufficient"}
    with pytest.raises(ValueError, match="requires reason"):
        parse_workflow_generation('{"action":"decline"}')


def test_generation_builds_and_validates_inert_capsule():
    from nz_coder.runtime.workflows.workflow_generation import parse_workflow_generation

    result = parse_workflow_generation(json.dumps({
        "action": "generate",
        "pattern": "fan-out-and-synthesize",
        "request": "Inspect routing",
        "options": {"agents": 2},
        "approval_summary": "Two investigators and one synthesis.",
    }))

    assert result["kind"] == "generated"
    assert result["capsule"]["format"] == "nzcoder.workflow"
    assert "source" not in result["capsule"]
    assert result["approval_summary"].startswith("Two investigators")


def test_generation_rejects_unknown_action_and_executable_source_shape():
    from nz_coder.runtime.workflows.workflow_generation import parse_workflow_generation

    with pytest.raises(ValueError, match="action must"):
        parse_workflow_generation('{"action":"guess"}')
    with pytest.raises(ValueError, match="requires approval_summary"):
        parse_workflow_generation(json.dumps({
            "action": "generate",
            "pattern": "tournament",
            "request": "compare",
            "source": "import os",
        }))


def test_generation_timeout_precedence_default_and_cap():
    from nz_coder.runtime.workflows.workflow_generation import (
        DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS,
        resolve_workflow_generation_timeout_ms,
    )

    assert resolve_workflow_generation_timeout_ms({}) == DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS
    assert resolve_workflow_generation_timeout_ms({
        "NZ_WORKFLOW_GENERATION_TIMEOUT_SEC": "2",
        "NZ_WORKFLOW_GENERATION_TIMEOUT_MS": "9000",
    }) == 2000
    assert resolve_workflow_generation_timeout_ms({}, timeout_seconds=9999) == 600_000
    assert resolve_workflow_generation_timeout_ms({
        "NZ_WORKFLOW_GENERATION_TIMEOUT_SEC": "invalid",
    }) == DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS


def test_generation_repair_prompt_is_bounded_and_forbids_source():
    from nz_coder.runtime.workflows.workflow_generation import (
        next_workflow_generation_repair,
        workflow_generation_repair_prompt,
    )

    prompt = workflow_generation_repair_prompt("bad schema", "x" * 10_000)

    assert "Return JSON only" in prompt
    assert "Do not return executable source" in prompt
    assert len(prompt) < 7000
    assert next_workflow_generation_repair(0, "bad", "raw")["attempt"] == 1
    assert next_workflow_generation_repair(1, "bad", "raw")["attempt"] == 2
    assert next_workflow_generation_repair(2, "bad", "raw")["allowed"] is False


def test_generation_tool_projects_validated_capsule():
    from nz_coder.runtime.workflows.workflow_features import workflow_generation

    raw = json.dumps({
        "action": "generate",
        "pattern": "classify-and-act",
        "request": "Classify changes",
        "approval_summary": "Bounded classifier.",
    })
    result = workflow_generation("parse", raw_text=raw)

    assert result.metadata["workflow_generation"]["kind"] == "generated"
    assert result.metadata["workflow_generation"]["capsule"]["format"] == "nzcoder.workflow"
