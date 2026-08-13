"""Asynchronous L2 stall judgement with a bounded main-agent transcript."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from nz_coder.runtime.stall_detector import StallDetector, StallSignal, stable_stringify


TRANSCRIPT_WINDOW = 16

SIDECAR_SYSTEM_PROMPT = """You are a stall-detector for an autonomous coding agent. A DIFFERENT
agent (the main agent) issued the same tool call repeatedly. Read the recent
transcript as a third-party observer. Classify is_stuck=true only when no real
progress occurred between repeats. Legitimate iteration, a changed workspace,
new evidence, or a verification reread is not a stall. Return JSON only with
is_stuck (boolean), reason (string), suggested_tool (string), and nudge
(string). When stuck, make nudge one concrete next action; otherwise leave it
empty."""


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


class StallSidecarOrchestrator:
    """Own L1 history, non-awaited L2 calls, and one-shot pending nudges."""

    def __init__(
        self,
        *,
        evaluate: Callable[[str], dict[str, Any]],
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
        self._transcript: list[dict[str, str]] = []
        self._pending_nudge: str | None = None
        self._threads: list[threading.Thread] = []
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
        thread = threading.Thread(
            target=self._run_evaluation,
            args=(epoch, signal, user_message),
            name="nz-stall-sidecar",
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return True

    def _run_evaluation(
        self,
        epoch: int,
        signal: StallSignal,
        user_message: str,
    ) -> None:
        started = time.monotonic()
        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def invoke() -> None:
            try:
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
        if not completed.wait(self._timeout_seconds):
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
        self._detector.reset()

    def settle(self, timeout: float = 0.0) -> bool:
        """Wait a bounded time for already-started sidecars to finish."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            threads = tuple(self._threads)
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            return not self._threads


__all__ = [
    "SIDECAR_SYSTEM_PROMPT",
    "StallSidecarOrchestrator",
    "TRANSCRIPT_WINDOW",
]
