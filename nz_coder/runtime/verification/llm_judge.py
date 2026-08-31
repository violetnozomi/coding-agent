"""Domain-neutral, fail-open LLM judge translated from InfCodeX FEATURE_215."""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar


VerdictT = TypeVar("VerdictT")


@dataclass(frozen=True)
class JudgeRequest:
    """One isolated structured judgement request."""

    system_prompt: str
    user_message: str
    report_tool: dict[str, Any]
    report_tool_name: str
    max_output_tokens: int = 1024


@dataclass(frozen=True)
class JudgeResponse:
    """Provider-neutral report blocks returned by a judgement adapter."""

    tool_blocks: tuple[dict[str, Any], ...] = ()
    text: str = ""


def edit_distance(left: str, right: str) -> int:
    """Return Levenshtein edit distance for short report-tool names."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    current = [0] * (len(right) + 1)
    for left_index, left_char in enumerate(left, start=1):
        current[0] = left_index
        for right_index, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current[right_index] = min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                previous[right_index - 1] + cost,
            )
        previous, current = current, previous
    return previous[-1]


def find_fuzzy_tool_match(
    tool_blocks: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    expected_tool_name: str,
) -> tuple[dict[str, Any], bool] | None:
    """Prefer an exact report call, otherwise the closest name within two edits."""
    for block in tool_blocks:
        if str(block.get("name") or "") == expected_tool_name:
            return block, True
    best: tuple[dict[str, Any], int] | None = None
    for block in tool_blocks:
        distance = edit_distance(str(block.get("name") or ""), expected_tool_name)
        if distance <= 2 and (best is None or distance < best[1]):
            best = block, distance
    return (best[0], False) if best is not None else None


def invoke_llm_judge(
    *,
    request: JudgeRequest,
    invoke: Callable[[JudgeRequest], JudgeResponse],
    parse_tool_call: Callable[[dict[str, Any], bool], VerdictT | None],
    default_verdict: Callable[[str], VerdictT],
    timeout_seconds: float = 15.0,
    cancel_event: threading.Event | None = None,
) -> VerdictT:
    """Run one bounded structured consult and never raise into the Main Agent."""
    if cancel_event is not None and cancel_event.is_set():
        return default_verdict("cancelled")
    outcomes: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            outcome: tuple[str, object] = ("response", invoke(request))
        except Exception as exc:
            outcome = ("error", exc)
        try:
            outcomes.put_nowait(outcome)
        except queue.Full:
            pass

    worker_thread = threading.Thread(
        target=worker,
        name="nz-llm-judge",
        daemon=True,
    )
    worker_thread.start()

    def cancelled_verdict() -> VerdictT:
        # Cooperative Provider adapters settle in one poll interval.  The
        # bounded join prevents their observer from leaking past run_end while
        # retaining fail-open behavior for third-party blocking callbacks.
        worker_thread.join(min(0.25, max(0.001, float(timeout_seconds))))
        return default_verdict("cancelled")

    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return cancelled_verdict()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return default_verdict("timeout")
        try:
            kind, value = outcomes.get(timeout=min(0.02, remaining))
        except queue.Empty:
            continue
        if cancel_event is not None and cancel_event.is_set():
            return cancelled_verdict()
        if kind == "error" or not isinstance(value, JudgeResponse):
            return default_verdict("provider_error")
        match = find_fuzzy_tool_match(value.tool_blocks, request.report_tool_name)
        if match is None:
            return default_verdict("no_tool_call")
        try:
            parsed = parse_tool_call(*match)
        except Exception:
            return default_verdict("parse_failure")
        if parsed is None:
            return default_verdict("parse_failure")
        return parsed


__all__ = [
    "JudgeRequest",
    "JudgeResponse",
    "edit_distance",
    "find_fuzzy_tool_match",
    "invoke_llm_judge",
]
