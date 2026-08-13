"""Strict JSON-only workflow generation envelopes and repair diagnostics."""
from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
from typing import Any, Callable

from nz_coder.runtime.workflow_builtins import generate_pattern_workflow
from nz_coder.runtime.workflow_capsule import validate_workflow_capsule


DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS = 120_000
MAX_WORKFLOW_GENERATION_TIMEOUT_MS = 600_000
WORKFLOW_GENERATION_REPAIR_ATTEMPTS = 2

WORKFLOW_GENERATION_SYSTEM_PROMPT = "\n".join((
    "You generate NZ-Coder declarative workflows.",
    "Return JSON only and never return executable source.",
    'For a simple task return {"action":"decline","reason":"..."}.',
    "For a complex task return action=generate with pattern, request, options, "
    "and approval_summary.",
    "Allowed patterns: classify-and-act, fan-out-and-synthesize, "
    "adversarial-verification, generate-and-filter, tournament, loop-until-done.",
    "Use multiple agents only when independent investigation, comparison, or "
    "verification materially improves the result.",
))

WorkflowGenerateText = Callable[[str, str], str]


def extract_generation_json(raw_text: str) -> str:
    """Extract one outer JSON object from plain or fenced model output."""
    text = str(raw_text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("workflow generation output did not contain a JSON object")
    return text[start:end + 1]


def parse_workflow_generation(raw_text: str) -> dict:
    """Parse decline/generate envelopes and validate the resulting inert Capsule."""
    try:
        value: Any = json.loads(extract_generation_json(raw_text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"workflow generation output is invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("workflow generation output must be an object")
    action = str(value.get("action") or "").strip().lower()
    if action == "decline":
        reason = str(value.get("reason") or "").strip()
        if not reason:
            raise ValueError("workflow generation decline requires reason")
        return {"kind": "declined", "reason": reason[:2000]}
    if action != "generate":
        raise ValueError("workflow generation action must be decline or generate")
    approval_summary = str(value.get("approval_summary") or "").strip()
    if not approval_summary:
        raise ValueError("workflow generation requires approval_summary")
    if isinstance(value.get("capsule"), dict):
        capsule = validate_workflow_capsule(value["capsule"])
    else:
        pattern = str(value.get("pattern") or "").strip()
        request = str(value.get("request") or "").strip()
        options = value.get("options")
        if options is not None and not isinstance(options, dict):
            raise ValueError("workflow generation options must be an object")
        capsule = generate_pattern_workflow(pattern, request, options)
    return {
        "kind": "generated",
        "capsule": capsule,
        "approval_summary": approval_summary[:4000],
    }


def resolve_workflow_generation_timeout_ms(
    env: dict[str, str] | None = None,
    *,
    timeout_seconds: float | None = None,
) -> int:
    """Resolve explicit seconds, seconds env, then legacy milliseconds env."""
    values = dict(os.environ if env is None else env)
    if timeout_seconds is not None:
        candidate = float(timeout_seconds) * 1000
    elif str(values.get("NZ_WORKFLOW_GENERATION_TIMEOUT_SEC") or "").strip():
        try:
            candidate = float(values["NZ_WORKFLOW_GENERATION_TIMEOUT_SEC"]) * 1000
        except ValueError:
            candidate = DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS
    elif str(values.get("NZ_WORKFLOW_GENERATION_TIMEOUT_MS") or "").strip():
        try:
            candidate = float(values["NZ_WORKFLOW_GENERATION_TIMEOUT_MS"])
        except ValueError:
            candidate = DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS
    else:
        candidate = DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS
    if not math.isfinite(candidate) or candidate <= 0:
        return DEFAULT_WORKFLOW_GENERATION_TIMEOUT_MS
    return min(int(candidate), MAX_WORKFLOW_GENERATION_TIMEOUT_MS)


def workflow_generation_repair_prompt(error: Exception | str, raw_text: str) -> str:
    """Return one bounded data-only correction request; callers own the retry budget."""
    return (
        "The workflow JSON envelope failed validation. Return JSON only with either "
        '{"action":"decline","reason":"..."} or '
        '{"action":"generate","pattern":"...","request":"...",'
        '"options":{},"approval_summary":"..."}. Do not return executable source.\n'
        f"Validation error: {str(error)[:2000]}\n"
        f"Previous output excerpt: {str(raw_text)[:4000]}"
    )


def next_workflow_generation_repair(
    attempt: int,
    error: Exception | str,
    raw_text: str,
) -> dict:
    """Issue at most two repair prompts for one generation operation."""
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise ValueError("workflow generation repair attempt must be a non-negative integer")
    if attempt >= WORKFLOW_GENERATION_REPAIR_ATTEMPTS:
        return {
            "allowed": False,
            "attempt": attempt,
            "max_attempts": WORKFLOW_GENERATION_REPAIR_ATTEMPTS,
        }
    return {
        "allowed": True,
        "attempt": attempt + 1,
        "max_attempts": WORKFLOW_GENERATION_REPAIR_ATTEMPTS,
        "prompt": workflow_generation_repair_prompt(error, raw_text),
    }


def _call_with_timeout(
    callback: WorkflowGenerateText,
    system: str,
    prompt: str,
    timeout_ms: int,
) -> str:
    """Bound one blocking Provider callback without owning its SDK transport."""
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, callback(system, prompt)), block=False)
        except BaseException as exc:
            result_queue.put((False, exc), block=False)

    worker = threading.Thread(
        target=invoke,
        name="nz-workflow-generator",
        daemon=True,
    )
    worker.start()
    try:
        ok, value = result_queue.get(timeout=max(0.001, timeout_ms / 1000))
    except queue.Empty as exc:
        raise TimeoutError(
            f"workflow generation timed out after {timeout_ms}ms"
        ) from exc
    if not ok:
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError(str(value))
    return str(value or "")


def generate_workflow_with_provider(
    request: str,
    generate_text: WorkflowGenerateText,
    *,
    timeout_seconds: float | None = None,
) -> dict:
    """Generate, validate, and repair one inert Capsule through a Provider.

    The same wall-clock budget covers the initial call and at most two repair
    calls.  No Capsule is saved or executed here; approval and runtime owners
    remain separate consumers.
    """
    task = str(request or "").strip()
    if not task:
        raise ValueError("workflow generation request is required")
    if not callable(generate_text):
        raise ValueError("workflow generation requires a Provider callback")
    timeout_ms = resolve_workflow_generation_timeout_ms(
        timeout_seconds=timeout_seconds
    )
    import time

    deadline = time.monotonic() + timeout_ms / 1000
    prompt = (
        "Decide whether this request benefits from a bounded multi-Agent "
        f"workflow and return the required JSON envelope.\nRequest: {task[:12000]}"
    )
    raw_text = ""
    repairs = 0
    while True:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError(
                f"workflow generation timed out after {timeout_ms}ms"
            )
        raw_text = _call_with_timeout(
            generate_text,
            WORKFLOW_GENERATION_SYSTEM_PROMPT,
            prompt,
            remaining_ms,
        )
        try:
            parsed = parse_workflow_generation(raw_text)
        except Exception as exc:
            repair = next_workflow_generation_repair(repairs, exc, raw_text)
            if not repair["allowed"]:
                raise ValueError(
                    "workflow generation remained invalid after "
                    f"{repairs} repair attempt(s): {exc}"
                ) from exc
            repairs = int(repair["attempt"])
            prompt = str(repair["prompt"])
            continue
        return {
            **parsed,
            "raw_text": raw_text[:20000],
            "attempts": repairs + 1,
            "repair_attempts": repairs,
            "timeout_ms": timeout_ms,
        }
