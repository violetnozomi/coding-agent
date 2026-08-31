"""Trusted data-only built-in workflows and bounded pattern generation."""
from __future__ import annotations

import copy
from typing import Callable

from nz_coder.runtime.workflows.workflow_capsule import create_workflow_capsule
from nz_coder.runtime.workflows.workflow_manifest import WORKFLOW_PATTERN_IDS


_FINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["finding"],
    "properties": {"finding": {"type": "string"}},
}
_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "unverified_requirements"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "severity", "location", "summary", "disposition"],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "location": {"type": "string"},
                    "summary": {"type": "string"},
                    "disposition": {"type": "string", "enum": ["confirmed", "refuted", "unresolved"]},
                },
            },
        },
        "unverified_requirements": {"type": "array", "items": {"type": "string"}},
    },
}


def _manifest(
    name: str,
    description: str,
    phases: list[str],
    agents: int,
    concurrency: int,
    patterns: list[str],
) -> dict:
    return {
        "name": name,
        "description": description,
        "phases": phases,
        "read_only": True,
        "planned_agents": agents,
        "max_agents": agents,
        "max_concurrency": min(concurrency, agents),
        "patterns": patterns,
    }


def parallel_investigation(args: dict) -> dict:
    question = str(args.get("question") or "").strip()
    if not question:
        raise ValueError("parallel-investigation requires question")
    maximum = max(2, min(int(args.get("max_agents") or 8), 20))
    targets = args.get("targets")
    if targets is None:
        targets = [
            "structure, entry points, and control flow",
            "edge cases, error handling, and failure modes",
            "tests, validation, and existing coverage",
        ]
    if (
        not isinstance(targets, list)
        or not targets
        or not all(isinstance(item, str) and item.strip() for item in targets)
    ):
        raise ValueError("parallel-investigation targets must be non-empty strings")
    targets = targets[:maximum - 1]
    tasks = [{
        "name": f"investigate-{index + 1}",
        "prompt": (
            "Investigate READ-ONLY and cite file:line evidence.\n"
            f"Question: {question}\nFocus: {target}"
        ),
        "read_only": True,
        "model_hint": "balanced",
        "output_schema": copy.deepcopy(_FINDING_SCHEMA),
    } for index, target in enumerate(targets)]
    manifest = _manifest(
        "parallel-investigation",
        "Fan out read-only investigators and synthesize evidence.",
        ["investigate", "synthesize"],
        len(tasks) + 1,
        min(4, len(tasks) or 1),
        ["fan-out-and-synthesize"],
    )
    plan = {
        "manifest": manifest,
        "phases": [
            {"name": "investigate", "mode": "parallel", "tasks": tasks, "concurrency": manifest["max_concurrency"]},
            {
                "name": "synthesize",
                "mode": "synthesize",
                "from_phases": ["investigate"],
                "rubric": str(args.get("rubric") or (
                    "Deduplicate, retain file:line evidence, rank by relevance, "
                    "and state gaps from failed investigations."
                )),
            },
        ],
    }
    return create_workflow_capsule(manifest=manifest, plan=plan)


def scoped_review(args: dict) -> dict:
    packets = args.get("packets")
    if not isinstance(packets, list) or not packets:
        raise ValueError("scoped-review requires packets")
    if len(packets) > 9:
        raise ValueError("scoped-review supports at most 9 packets")
    for packet in packets:
        if not isinstance(packet, dict) or not packet.get("packet_path"):
            raise ValueError("scoped-review packet metadata is invalid")
    stages = [
        {
            "name": "primary-review",
            "prompt": (
                "Review immutable packet {item}. Read packet_path and every evidence chunk. "
                "Return only actionable defects; uncertain requirements belong in "
                "unverified_requirements."
            ),
            "read_only": True,
            "model_hint": "balanced",
            "output_schema": copy.deepcopy(_REVIEW_SCHEMA),
        },
        {
            "name": "finding-verifier",
            "prompt": (
                "Independently refute candidate findings from {previous} against packet {item}. "
                "Preserve confirmed/unresolved findings and mark refuted findings explicitly."
            ),
            "read_only": True,
            "model_hint": "deep",
            "output_schema": copy.deepcopy(_REVIEW_SCHEMA),
        },
    ]
    agents = len(packets) * 2 + 1
    manifest = _manifest(
        "scoped-review",
        "Review immutable packets, verify findings, and synthesize without silent approval.",
        ["review", "quality-gate", "final-synthesis"],
        agents,
        min(4, len(packets)),
        ["adversarial-verification", "fan-out-and-synthesize"],
    )
    plan = {
        "manifest": manifest,
        "phases": [
            {"name": "review", "mode": "pipeline", "items": packets, "stages": stages},
            {"name": "quality-gate", "mode": "quality_gate", "from_phases": ["review"], "artifact": "scoped-review-audit"},
            {
                "name": "final-synthesis",
                "mode": "synthesize",
                "from_phases": ["quality-gate"],
                "rubric": (
                    "Lead with confirmed findings, preserve unresolved uncertainty and severity, "
                    "omit refuted findings, and never infer approval from missing evidence."
                ),
            },
        ],
    }
    return create_workflow_capsule(manifest=manifest, plan=plan)


_BUILTINS: dict[str, Callable[[dict], dict]] = {
    "parallel-investigation": parallel_investigation,
    "scoped-review": scoped_review,
}


def list_builtin_workflows() -> list[str]:
    return sorted(_BUILTINS)


def get_builtin_workflow(name: str, args: dict | None = None) -> dict | None:
    builder = _BUILTINS.get(str(name))
    return builder(dict(args or {})) if builder is not None else None


def generate_pattern_workflow(pattern: str, request: str, options: dict | None = None) -> dict:
    """Generate one bounded data plan, never provider-authored executable source."""
    if pattern not in WORKFLOW_PATTERN_IDS:
        raise ValueError(f"unsupported workflow pattern: {pattern}")
    text = str(request or "").strip()
    if not text:
        raise ValueError("workflow generator requires request")
    opts = dict(options or {})
    count = max(1, min(int(opts.get("agents") or 3), 8))
    if pattern == "fan-out-and-synthesize":
        return parallel_investigation({
            "question": text,
            "targets": opts.get("targets") or [f"independent angle {index + 1}" for index in range(count)],
            "max_agents": count + 1,
        })
    stages = 2 if pattern in {"adversarial-verification", "generate-and-filter"} else count
    tasks = [{
        "prompt": f"{text}\nBounded workflow step {index + 1} of {stages}.",
        "read_only": True,
        "model_hint": "balanced" if index == 0 else "deep",
    } for index in range(stages)]
    phase_name = "bounded-loop" if pattern == "loop-until-done" else "candidates"
    if pattern in {"adversarial-verification", "generate-and-filter", "loop-until-done"}:
        phase = {"name": phase_name, "mode": "pipeline", "items": [text], "stages": tasks}
        agents = stages + 1
    else:
        phase = {"name": phase_name, "mode": "parallel", "tasks": tasks}
        agents = len(tasks) + 1
    manifest = _manifest(
        f"generated-{pattern}",
        f"Bounded generated {pattern} workflow.",
        [phase_name, "synthesize"],
        agents,
        min(4, count),
        [pattern],
    )
    plan = {
        "manifest": manifest,
        "phases": [
            phase,
            {
                "name": "synthesize",
                "mode": "synthesize",
                "from_phases": [phase_name],
                "rubric": "Return the strongest supported result and preserve uncertainty.",
            },
        ],
    }
    return create_workflow_capsule(manifest=manifest, plan=plan)
