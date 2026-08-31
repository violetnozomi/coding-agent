"""Asynchronous L2 stall judgement with a bounded main-agent transcript."""
from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from typing import Any

from nz_coder.runtime.verification.llm_judge import (
    JudgeRequest,
    JudgeResponse,
    invoke_llm_judge,
)
from nz_coder.runtime.verification.stall_detector import StallDetector, StallSignal, stable_stringify


TRANSCRIPT_WINDOW = 16
STALL_SIDECAR_TIMEOUT_SECONDS = 5.0
REPORT_TOOL_NAME = "report_stall_judgment"

STALL_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": REPORT_TOOL_NAME,
        "description": (
            "Report whether the main coding agent is in a real tool-use stall."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "isStuck": {"type": "boolean"},
                "reason": {"type": "string"},
                "suggestedTool": {"type": "string"},
                "nudge": {"type": "string"},
            },
            "required": ["isStuck", "reason"],
            "additionalProperties": False,
        },
    },
}

SIDECAR_SYSTEM_PROMPT = """You are a stall-detector for an autonomous coding agent. A DIFFERENT
agent (the main agent) issued the same tool call repeatedly. Read the recent
transcript as a third-party observer. Classify `isStuck=true` only when no real
progress occurred between repeats. Legitimate iteration, a changed workspace,
new evidence, or a verification reread is not a stall. Call the
`report_stall_judgment` tool exactly once and do not narrate. When stuck, make
`nudge` one concrete next action; otherwise leave it empty."""


def _normalize_is_stuck(raw: Any) -> tuple[bool, bool] | None:
    """Normalize InfCodeX's observed boolean/string provider variants."""
    if isinstance(raw, bool):
        return raw, False
    if isinstance(raw, str) and raw.strip().casefold() in {"true", "false"}:
        return raw.strip().casefold() == "true", True
    return None


def _parse_stall_report(block: dict[str, Any], exact: bool) -> dict[str, Any] | None:
    """Parse one exact or fuzzy ``report_stall_judgment`` tool call."""
    payload = block.get("input")
    if not isinstance(payload, dict):
        return None
    normalized = _normalize_is_stuck(payload.get("isStuck", payload.get("is_stuck")))
    if normalized is None:
        return None
    is_stuck, coerced = normalized
    trace = "sidecar_ok"
    if not exact:
        trace = "fuzzy_tool_match"
    elif coerced:
        trace = "coerced_string_bool"
    verdict: dict[str, Any] = {
        "is_stuck": is_stuck,
        "reason": str(payload.get("reason") or "")[:500],
        "nudge": str(payload.get("nudge") or "")[:2000] if is_stuck else "",
        "trace": trace,
    }
    suggested = str(
        payload.get("suggestedTool", payload.get("suggested_tool", "")) or ""
    ).strip()
    if suggested:
        verdict["suggested_tool"] = suggested
    return verdict


def _default_stall_verdict(reason: str) -> dict[str, Any]:
    trace = reason if reason in {
        "provider_error",
        "timeout",
        "cancelled",
        "no_tool_call",
    } else "no_tool_call"
    return {"is_stuck": False, "trace": trace}


def invoke_stall_sidecar(
    *,
    user_message: str,
    invoke: Callable[[JudgeRequest], JudgeResponse],
    timeout_seconds: float = STALL_SIDECAR_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Invoke the InfCodeX-style forced structured L2 judge, fail-open."""
    return invoke_llm_judge(
        request=JudgeRequest(
            system_prompt=SIDECAR_SYSTEM_PROMPT,
            user_message=user_message,
            report_tool=STALL_REPORT_TOOL,
            report_tool_name=REPORT_TOOL_NAME,
            max_output_tokens=300,
        ),
        invoke=invoke,
        parse_tool_call=_parse_stall_report,
        default_verdict=_default_stall_verdict,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def _render_transcript(messages: list[dict[str, str]]) -> str:
    lines = ["=== MAIN AGENT TRANSCRIPT (you are reading, not authoring) ==="]
    assistant_turn = 0
    for message in messages:
        if message["kind"] == "tool_use":
            assistant_turn += 1
            lines.extend((
                "",
                f"[MAIN AGENT — assistant turn {assistant_turn}]",
                (
                    f"tool_use: {message['name']}({message['input_json']}) "
                    f"[id={message['id']}]"
                ),
            ))
        else:
            lines.extend((
                "",
                f"[TOOL_RESULT for {message['id']}]",
                message["content"],
            ))
    lines.extend(("", "=== END TRANSCRIPT ==="))
    return "\n".join(lines)


def _build_user_message(signal: StallSignal, messages: list[dict[str, str]]) -> str:
    return "\n".join((
        signal.envelope,
        "",
        _render_transcript(messages),
        "",
        (
            "Judge whether the main agent above is in a real stall and return "
            "the required JSON object."
        ),
    ))


def _accepts_cancel_event(callback: Callable[..., object]) -> bool:
    """Detect the optional cooperative-cancellation evaluator contract."""
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return True
    positional = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }
    return sum(parameter.kind in positional for parameter in parameters) >= 2


class StallSidecarOrchestrator:
    """Own L1 history, non-awaited L2 calls, and one-shot pending nudges."""

    def __init__(
        self,
        *,
        evaluate: Callable[..., dict[str, Any]],
        detector: StallDetector | None = None,
        transcript_window: int = TRANSCRIPT_WINDOW,
        timeout_seconds: float = 5.0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._evaluate = evaluate
        self._detector = detector or StallDetector()
        self._window = max(1, int(transcript_window))
        self._timeout_seconds = max(0.001, float(timeout_seconds))
        self._on_event = on_event
        self._evaluate_accepts_cancel = _accepts_cancel_event(evaluate)
        self._transcript: list[dict[str, str]] = []
        self._pending_nudge: str | None = None
        self._threads: list[threading.Thread] = []
        self._cancel_events: list[threading.Event] = []
        self._epoch = 0
        self._lock = threading.Lock()

    @property
    def transcript_size(self) -> int:
        with self._lock:
            return len(self._transcript)

    def _push(self, message: dict[str, str]) -> None:
        with self._lock:
            self._transcript.append(message)
            if len(self._transcript) > self._window:
                del self._transcript[:-self._window]

    def record_tool_use(self, call: dict[str, Any], *, cache_hit: bool = False) -> bool:
        """Record a call and launch L2 without waiting when L1 fires."""
        tool_name = str(call.get("name") or "")
        call_id = str(call.get("id") or "")
        tool_input = call.get("input", {})
        self._push({
            "kind": "tool_use",
            "id": call_id,
            "name": tool_name,
            "input_json": stable_stringify(tool_input),
        })
        signal = self._detector.record_tool_use(tool_name, tool_input, cache_hit)
        if signal.kind != "stall":
            return False
        with self._lock:
            snapshot = [dict(item) for item in self._transcript]
            epoch = self._epoch
        user_message = _build_user_message(signal, snapshot)
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._run_evaluation,
            args=(epoch, signal, user_message, cancel_event),
            name="nz-stall-sidecar",
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
            self._cancel_events.append(cancel_event)
        thread.start()
        return True

    def _run_evaluation(
        self,
        epoch: int,
        signal: StallSignal,
        user_message: str,
        cancel_event: threading.Event,
    ) -> None:
        started = time.monotonic()
        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def invoke() -> None:
            try:
                if self._evaluate_accepts_cancel:
                    outcome["verdict"] = self._evaluate(
                        user_message,
                        cancel_event,
                    )
                else:
                    outcome["verdict"] = self._evaluate(user_message)
            except Exception as exc:
                outcome["error"] = exc
            finally:
                completed.set()

        threading.Thread(
            target=invoke,
            name="nz-stall-sidecar-provider",
            daemon=True,
        ).start()
        deadline = time.monotonic() + self._timeout_seconds
        while (
            not completed.is_set()
            and not cancel_event.is_set()
            and time.monotonic() < deadline
        ):
            completed.wait(min(0.02, max(0.0, deadline - time.monotonic())))
        if cancel_event.is_set():
            # A cancel-aware Gateway should settle in one poll interval.  Keep
            # this bounded for third-party one-argument evaluators.
            completed.wait(min(0.25, self._timeout_seconds))
            event = {"is_stuck": False, "trace": "cancelled"}
        elif not completed.is_set():
            event = {"is_stuck": False, "trace": "timeout"}
        elif "error" in outcome:
            exc = outcome["error"]
            event = {
                "is_stuck": False,
                "trace": "provider_error",
                "error": str(exc),
            }
        else:
            verdict = outcome.get("verdict")
            if not isinstance(verdict, dict) or not isinstance(verdict.get("is_stuck"), bool):
                event = {"is_stuck": False, "trace": "invalid_verdict"}
            else:
                event = dict(verdict)
                event.setdefault("trace", "sidecar_ok")
                nudge = str(event.get("nudge") or "").strip()[:2000]
                if event["is_stuck"] and nudge:
                    with self._lock:
                        if epoch == self._epoch:
                            self._pending_nudge = nudge
        event.update({
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "signal_envelope": signal.envelope,
        })
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass

    def record_tool_result(self, call_id: str, content: str) -> None:
        """Append the settled tool output to the sidecar-only transcript."""
        self._push({
            "kind": "tool_result",
            "id": str(call_id),
            "content": str(content),
        })

    def consume_pending_nudge(self) -> str | None:
        """Return a queued nudge exactly once."""
        with self._lock:
            nudge = self._pending_nudge
            self._pending_nudge = None
            return nudge

    def reset(self) -> None:
        """Clear context-local state and invalidate unfinished L2 verdicts."""
        with self._lock:
            self._epoch += 1
            self._transcript.clear()
            self._pending_nudge = None
            cancellations = tuple(self._cancel_events)
        for cancel_event in cancellations:
            cancel_event.set()
        self._detector.reset()

    def cancel_and_settle(self, timeout: float = 1.0) -> bool:
        """Cancel run-owned L2 work and close its observer boundary."""
        self.reset()
        return self.settle(timeout=max(0.0, float(timeout)))

    def settle(self, timeout: float = 0.0) -> bool:
        """Wait a bounded time for already-started sidecars to finish."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            threads = tuple(self._threads)
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        with self._lock:
            alive = [
                (thread, cancel_event)
                for thread, cancel_event in zip(
                    self._threads,
                    self._cancel_events,
                )
                if thread.is_alive()
            ]
            self._threads = [thread for thread, _cancel_event in alive]
            self._cancel_events = [cancel_event for _thread, cancel_event in alive]
            return not self._threads


__all__ = [
    "REPORT_TOOL_NAME",
    "SIDECAR_SYSTEM_PROMPT",
    "STALL_REPORT_TOOL",
    "STALL_SIDECAR_TIMEOUT_SECONDS",
    "StallSidecarOrchestrator",
    "TRANSCRIPT_WINDOW",
    "invoke_stall_sidecar",
]
