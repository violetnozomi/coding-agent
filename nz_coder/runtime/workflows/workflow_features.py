"""Model-facing tools for review packets and trusted data-plan generation."""
from __future__ import annotations

import json

from nz_coder.runtime.workflows.workflow_builtins import (
    generate_pattern_workflow,
    get_builtin_workflow,
    list_builtin_workflows,
)
from nz_coder.runtime.workflows.workflow_review import write_review_packets
from nz_coder.runtime.workflows.workflow_generation import (
    parse_workflow_generation,
    next_workflow_generation_repair,
    resolve_workflow_generation_timeout_ms,
)
from nz_coder.runtime.workflows.workflow_host import (
    build_scout_then_author_prompt,
    build_workflow_approval_summary,
    evaluate_workflow_approval,
    resolve_workflow_identity,
    workflow_invocation_decision,
    workflow_start_outcome_consumes_turn,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.sessions import active_session_id
from nz_coder.tools import ToolOutput, register


def workflow_review_packet(
    label: str,
    diff: str,
    requirements: list[str] | None = None,
    test_evidence: list[str] | None = None,
    routing_risk: str = "low",
) -> str:
    """Persist immutable supplied diff packets; never invokes Git implicitly."""
    try:
        packets = write_review_packets(
            workspace=current_workdir(),
            session_id=active_session_id() or "workflow",
            label=label,
            diff=diff,
            requirements=requirements,
            test_evidence=test_evidence,
            routing_risk=routing_risk,
        )
        return ToolOutput(
            f"Created {len(packets)} immutable review packet(s).",
            title="Workflow review packets",
            metadata={"review_packets": packets},
        )
    except Exception as exc:
        return f"Error: {exc}"


def workflow_generate(
    pattern: str,
    request: str,
    options: dict | None = None,
) -> str:
    """Generate an inspectable JSON Capsule, not executable model-authored code."""
    try:
        capsule = generate_pattern_workflow(pattern, request, options)
        return ToolOutput(
            json.dumps(capsule, ensure_ascii=False, indent=2),
            title=f"Generated workflow: {pattern}",
            metadata={"workflow_capsule": capsule},
        )
    except Exception as exc:
        return f"Error: {exc}"


def workflow_builtin(
    action: str,
    name: str = "",
    args: dict | None = None,
) -> str:
    """List or materialize trusted built-in data plans."""
    try:
        normalized = str(action or "").strip().lower()
        if normalized == "list":
            names = list_builtin_workflows()
            return ToolOutput(
                f"Built-in workflows: {len(names)}.",
                title="Built-in workflows",
                metadata={"builtin_workflows": names},
            )
        if normalized == "show":
            capsule = get_builtin_workflow(name, args)
            if capsule is None:
                return f"Error: unknown built-in workflow: {name}"
            return ToolOutput(
                json.dumps(capsule, ensure_ascii=False, indent=2),
                title=f"Built-in workflow: {name}",
                metadata={"workflow_capsule": capsule},
            )
        return "Error: action must be list or show"
    except Exception as exc:
        return f"Error: {exc}"


def workflow_host(
    action: str,
    target: str = "",
    source: str = "command",
    outcome: str = "started",
    request: str = "",
    manifest: dict | None = None,
    host_policy: dict | None = None,
    decision: str = "",
    approval_digest: str = "",
    headless: bool = False,
) -> str:
    """Inspect host-owned workflow launch decisions without starting a run."""
    try:
        normalized = str(action or "").strip().lower()
        if normalized == "resolve":
            from nz_coder.runtime.agent.agent_manager import _current_manager

            manager = _current_manager()
            value = resolve_workflow_identity(
                target,
                workspace=manager.workspace,
                runs_root=manager._workflow.root / "runs",
            )
        elif normalized == "invocation":
            value = workflow_invocation_decision(source)
        elif normalized == "turn-consumption":
            value = {
                "outcome": outcome,
                "consumes_turn": workflow_start_outcome_consumes_turn(outcome),
            }
        elif normalized == "approval-summary":
            value = build_workflow_approval_summary(manifest, host_policy)
        elif normalized == "approval-decision":
            summary = build_workflow_approval_summary(manifest, host_policy)
            value = evaluate_workflow_approval(
                summary,
                decision=decision,
                expected_digest=approval_digest,
                headless=headless,
            )
        elif normalized == "author-prompt":
            value = {"prompt": build_scout_then_author_prompt(request)}
        else:
            return (
                "Error: action must be resolve, invocation, turn-consumption, "
                "approval-summary, approval-decision, or author-prompt"
            )
        return ToolOutput(
            json.dumps(value, ensure_ascii=False, indent=2),
            title=f"Workflow host: {normalized}",
            metadata={"workflow_host": value},
        )
    except Exception as exc:
        return f"Error: {exc}"


def workflow_generation(
    action: str,
    raw_text: str = "",
    error: str = "",
    timeout_seconds: float | None = None,
    attempt: int = 0,
) -> str:
    """Parse or diagnose a strict JSON-only generated workflow envelope."""
    try:
        normalized = str(action or "").strip().lower()
        if normalized == "parse":
            value = parse_workflow_generation(raw_text)
        elif normalized == "timeout":
            value = {
                "timeout_ms": resolve_workflow_generation_timeout_ms(
                    timeout_seconds=timeout_seconds
                )
            }
        elif normalized == "repair-prompt":
            value = next_workflow_generation_repair(attempt, error, raw_text)
        else:
            return "Error: action must be parse, timeout, or repair-prompt"
        return ToolOutput(
            json.dumps(value, ensure_ascii=False, indent=2),
            title=f"Workflow generation: {normalized}",
            metadata={"workflow_generation": value},
        )
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="workflow_review_packet",
    description="Create immutable bounded review packets from supplied diff bytes.",
    parameters={
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "diff": {"type": "string"},
            "requirements": {"type": "array", "items": {"type": "string"}},
            "test_evidence": {"type": "array", "items": {"type": "string"}},
            "routing_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["label", "diff"],
    },
    handler=workflow_review_packet,
    execution="write",
    side_effect="mutates-state",
)

register(
    name="workflow_generate",
    description="Generate a bounded JSON-only Capsule from a supported workflow pattern.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "enum": [
                    "classify-and-act", "fan-out-and-synthesize",
                    "adversarial-verification", "generate-and-filter",
                    "tournament", "loop-until-done",
                ],
            },
            "request": {"type": "string"},
            "options": {"type": "object"},
        },
        "required": ["pattern", "request"],
    },
    handler=workflow_generate,
    execution="read",
)

register(
    name="workflow_generation",
    description="Parse, time-bound, or repair a strict JSON-only workflow generation envelope.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["parse", "timeout", "repair-prompt"]},
            "raw_text": {"type": "string"},
            "error": {"type": "string"},
            "timeout_seconds": {"type": "number"},
            "attempt": {"type": "integer"},
        },
        "required": ["action"],
    },
    handler=workflow_generation,
    execution="read",
)

register(
    name="workflow_builtin",
    description="List or inspect trusted built-in JSON-only workflows.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "show"]},
            "name": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["action"],
    },
    handler=workflow_builtin,
    execution="read",
)

register(
    name="workflow_host",
    description=(
        "Resolve workflow identity or inspect command invocation, approval, "
        "turn-consumption, and scout-then-author host contracts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "resolve", "invocation", "turn-consumption",
                    "approval-summary", "author-prompt",
                    "approval-decision",
                ],
            },
            "target": {"type": "string"},
            "source": {"type": "string", "enum": ["command", "natural-language"]},
            "outcome": {
                "type": "string",
                "enum": ["started", "declined", "cancelled", "failed"],
            },
            "request": {"type": "string"},
            "manifest": {"type": "object"},
            "host_policy": {"type": "object"},
            "decision": {
                "type": "string", "enum": ["approve", "deny", "cancel"],
            },
            "approval_digest": {"type": "string"},
            "headless": {"type": "boolean"},
        },
        "required": ["action"],
    },
    handler=workflow_host,
    execution="read",
)
