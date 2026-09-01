"""Durable per-step and per-tool lifecycle state for the Agent loop.

The processor translates the provider/tool execution stream into additive
message parts.  Provider-facing messages keep their legacy Chat Completions
shape; Session consumers receive InfCode-style pending/running/completed/error
state that survives save and resume.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from nz_coder.protocol.message_schema import (
    INTERACTION_RUN_ID_KEY,
    ASSISTANT_CHILD_COST_KEY,
    ASSISTANT_COST_KEY,
    ASSISTANT_FINISH_KEY,
    ASSISTANT_TIME_KEY,
    MESSAGE_ID_KEY,
    PARTS_KEY,
    assistant_error_from_exception,
    publish_assistant_state,
    remove_message_part,
    upsert_message_part,
)


OUTPUT_LENGTH_WARNING = (
    "The model hit its output limit, so this response may be incomplete."
)
REASONING_LENGTH_WARNING = (
    "The model hit its output limit while reasoning and produced no actionable "
    "output. Try disabling reasoning or increasing the output limit."
)


def _finite_number(value: object) -> float | None:
    """Return one finite persisted number without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _finite_nonnegative_int(value: object) -> int:
    """Normalize one untrusted usage bucket for durable Session metadata."""
    normalized = _finite_number(value)
    if normalized is None or normalized < 0 or not normalized.is_integer():
        return 0
    return int(normalized)


class SessionProcessor:
    """Mutate one assistant message through a deterministic step lifecycle."""

    def __init__(
        self,
        assistant_message: dict,
        *,
        publish: Callable[[str, dict], None] | None = None,
        on_message_updated: Callable[[dict], None] | None = None,
    ) -> None:
        message_id = assistant_message.get(MESSAGE_ID_KEY)
        if not isinstance(message_id, str) or not message_id.startswith("msg-"):
            raise ValueError("assistant message must have a durable identity")
        self.message = assistant_message
        self.message_id = message_id
        self.publish = publish
        self.on_message_updated = on_message_updated
        self._lock = threading.RLock()
        self._blocked = False
        now = time.time()
        self._step_started_at = now
        parts = assistant_message.get(PARTS_KEY)
        for part in parts if isinstance(parts, list) else ():
            if not isinstance(part, dict) or part.get("type") != "step-start":
                continue
            timing = part.get("time")
            raw_start = timing.get("start") if isinstance(timing, dict) else None
            start = _finite_number(raw_start)
            if start is None:
                part["time"] = {
                    **(timing if isinstance(timing, dict) else {}),
                    "start": now,
                }
                start = now
            self._step_started_at = start
            break
        timing = assistant_message.get(ASSISTANT_TIME_KEY)
        created = (
            _finite_number(timing.get("created"))
            if isinstance(timing, dict)
            else None
        )
        if created is None:
            assistant_message[ASSISTANT_TIME_KEY] = {"created": now}

    def start_step(
        self,
        snapshot: str | None = None,
        *,
        started_at: float | None = None,
    ) -> dict:
        """Persist the pre-step boundary before any tool begins."""
        self._step_started_at = (
            float(started_at)
            if isinstance(started_at, (int, float)) and math.isfinite(started_at)
            else time.time()
        )
        part = {
            "id": self._part_id("step-start"),
            "message_id": self.message_id,
            "type": "step-start",
            "time": {"start": self._step_started_at},
        }
        if snapshot:
            part["snapshot"] = snapshot
        return self._update(part)

    def set_step_snapshot(self, snapshot: str) -> dict | None:
        """Attach a late pre-tool snapshot without resetting the step clock."""
        if not isinstance(snapshot, str) or not snapshot:
            return None
        for part in self.message.get(PARTS_KEY, []):
            if isinstance(part, dict) and part.get("type") == "step-start":
                return self._update({**part, "snapshot": snapshot})
        return self.start_step(snapshot)

    @property
    def step_snapshot(self) -> str | None:
        """Return the current step-start snapshot, if capture succeeded."""
        for part in self.message.get(PARTS_KEY, []):
            if isinstance(part, dict) and part.get("type") == "step-start":
                snapshot = part.get("snapshot")
                return snapshot if isinstance(snapshot, str) and snapshot else None
        return None

    def register_tool_calls(self, tool_calls: list[dict]) -> None:
        """Create pending parts as soon as the assistant response is accepted."""
        for index, tool_call in enumerate(tool_calls):
            call_id = str(tool_call.get("id") or tool_call.get("tool_call_id") or "")
            fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            name = str(fn.get("name") or "unknown")
            if not call_id:
                continue
            raw = fn.get("arguments", {})
            existing = self._tool_part(call_id) or self._tool_part_by_index(index)
            self._update({
                "id": (existing or {}).get("id") or self._part_id("tool", str(index)),
                "message_id": self.message_id,
                "type": "tool",
                "tool": name,
                "call_id": call_id,
                "index": index,
                "state": {
                    "status": "pending",
                    "input": _arguments(raw),
                    "raw": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
                },
                **(
                    {"metadata": dict(tool_call["provider_extra"])}
                    if isinstance(tool_call.get("provider_extra"), dict)
                    and tool_call["provider_extra"]
                    else (
                        {"metadata": dict(existing["metadata"])}
                        if isinstance((existing or {}).get("metadata"), dict)
                        else {}
                    )
                ),
            })

    def stream_tool_delta(
        self,
        index: int,
        *,
        call_id: str = "",
        name: str = "",
        arguments: str = "",
        metadata: dict | None = None,
    ) -> dict:
        """Persist a provider tool-input start/delta before the call is complete."""
        existing = self._tool_part(call_id) if call_id else None
        existing = existing or self._tool_part_by_index(index)
        previous_state = (existing or {}).get("state") or {}
        selected_call_id = call_id or str((existing or {}).get("call_id") or f"pending-{index}")
        selected_name = name or str((existing or {}).get("tool") or "unknown")
        parsed = _arguments(arguments)
        if not parsed and isinstance(previous_state.get("input"), dict):
            parsed = dict(previous_state["input"])
        return self._update({
            "id": (existing or {}).get("id") or self._part_id("tool", str(index)),
            "message_id": self.message_id,
            "type": "tool",
            "tool": selected_name,
            "call_id": selected_call_id,
            "index": max(0, int(index)),
            "state": {
                "status": "pending",
                "input": parsed,
                "raw": str(arguments),
            },
            **(
                {"metadata": dict(metadata)}
                if isinstance(metadata, dict) and metadata
                else (
                    {"metadata": dict(existing["metadata"])}
                    if isinstance((existing or {}).get("metadata"), dict)
                    else {}
                )
            ),
        })

    def fail_unsettled(self, error: str, *, interrupted: bool = False) -> int:
        """Settle all pending/running tools after a failed provider/tool attempt."""
        count = 0
        for part in list(self.message.get(PARTS_KEY, [])):
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            if isinstance(state, dict) and state.get("status") in {"pending", "running"}:
                self.fail_tool(str(part.get("call_id") or ""), error, interrupted=interrupted)
                count += 1
        return count

    def add_reasoning(self, text: str) -> dict | None:
        """Persist provider reasoning separately from user-visible text."""
        if not isinstance(text, str) or not text:
            return None
        return self._update({
            "id": self._part_id("reasoning"),
            "message_id": self.message_id,
            "type": "reasoning",
            "text": text,
            "time": {"start": self._step_started_at, "end": time.time()},
        })

    def add_handoff(
        self,
        source: str,
        target: str,
        *,
        kind: str = "continuation",
        description: str = "",
    ) -> dict:
        """Persist one point-in-time Agent ownership transition."""
        return self._update({
            "id": self._part_id("handoff", f"{source}-{target}"),
            "message_id": self.message_id,
            "type": "handoff",
            "from": str(source),
            "to": str(target),
            "kind": str(kind),
            "description": str(description),
            "time": {"start": time.time(), "end": time.time()},
        })

    def add_length_warning(
        self,
        *,
        has_text: bool,
        has_reasoning: bool,
        has_tools: bool,
    ) -> str:
        """Persist the ignored user warning for one output-limit finish."""
        warning = (
            REASONING_LENGTH_WARNING
            if has_reasoning and not has_text and not has_tools
            else OUTPUT_LENGTH_WARNING
        )
        self._update({
            "id": self._part_id("text", "length-warning"),
            "message_id": self.message_id,
            "type": "text",
            "text": warning,
            "ignored": True,
            "time": {"start": time.time(), "end": time.time()},
        })
        return warning

    def stream_text(
        self,
        text: str,
        *,
        part_id: str,
        run_id: str = "",
        attempt_id: str = "",
        generation_id: str = "",
        generation: int = 0,
        version: int = 0,
    ) -> dict:
        """Persist accumulated visible text while delta events remain incremental."""
        self.message["content"] = str(text)
        return self._update({
            "id": part_id,
            "message_id": self.message_id,
            "type": "text",
            "text": str(text),
            "time": {"start": self._step_started_at},
            **({"run_id": run_id} if run_id else {}),
            **({"attempt_id": attempt_id} if attempt_id else {}),
            **({"generation_id": generation_id} if generation_id else {}),
            "generation": max(0, int(generation)),
            "version": max(0, int(version)),
        }, publish=False)

    def remove_part(self, part_id: str, reason: str) -> dict | None:
        """Remove a failed attempt from durable state and publish its tombstone."""
        with self._lock:
            removed = remove_message_part(self.message, part_id)
            if removed is None:
                return None
            if self.publish is not None:
                self.publish("message.part.removed", {
                    "message_id": self.message_id,
                    "part_id": part_id,
                    "reason": str(reason),
                    **{
                        key: removed[key]
                        for key in (
                            "run_id",
                            "attempt_id",
                            "generation_id",
                            "generation",
                            "version",
                        )
                        if key in removed
                    },
                })
            self._notify_message_updated()
            return removed

    def start_tools(self, tool_calls: list[dict]) -> None:
        """Move registered tool calls to running immediately before dispatch."""
        now = time.time()
        for tool_call in tool_calls:
            call_id = str(tool_call.get("id") or tool_call.get("tool_call_id") or "")
            if not call_id:
                continue
            part = self._tool_part(call_id)
            if part is None:
                continue
            state = dict(part["state"])
            state.update({"status": "running", "time": {"start": now}})
            state.pop("raw", None)
            self._update({**part, "state": state})

    def complete_tool(
        self,
        call_id: str,
        output: str,
        *,
        title: str = "",
        metadata: dict | None = None,
        attachments: list[dict] | None = None,
    ) -> None:
        """Settle a running tool part with its persisted result."""
        part = self._tool_part(call_id)
        if part is None:
            return
        start = part.get("state", {}).get("time", {}).get("start", time.time())
        self._update({
            **part,
            "state": {
                "status": "completed",
                "input": dict(part.get("state", {}).get("input") or {}),
                "output": str(output),
                "title": title,
                "metadata": dict(metadata or {}),
                "time": {"start": start, "end": time.time()},
                **({"attachments": list(attachments)} if attachments else {}),
            },
        })

    def update_tool_metadata(
        self,
        call_id: str,
        *,
        title: str = "",
        metadata: dict | None = None,
    ) -> dict | None:
        """Persist a pending/running tool's title and structured progress."""
        with self._lock:
            part = self._tool_part(call_id)
            if part is None:
                return None
            previous = dict(part.get("state") or {})
            if previous.get("status") not in {"pending", "running"}:
                return None
            start = previous.get("time", {}).get("start", time.time())
            state = {
                "status": "running",
                "input": dict(previous.get("input") or {}),
                "time": {"start": start},
            }
            selected_title = str(title or previous.get("title") or "")
            if selected_title:
                state["title"] = selected_title
            selected_metadata = (
                dict(metadata)
                if isinstance(metadata, dict)
                else dict(previous.get("metadata") or {})
            )
            if selected_metadata:
                state["metadata"] = selected_metadata
            return self._update({**part, "state": state})

    def start_question(
        self,
        call_id: str,
        request_id: str,
        questions: list[dict],
    ) -> dict | None:
        """Create the durable display part before the interaction blocks."""
        if not call_id or not request_id:
            return None
        display = [
            {**item, "custom": True}
            for item in questions
            if isinstance(item, dict)
        ]
        return self._update({
            "id": self._part_id("question", call_id),
            "message_id": self.message_id,
            "type": "question",
            "request_id": request_id,
            "tool_call_id": call_id,
            "questions": display,
            "status": "pending",
        })

    def complete_question(
        self,
        call_id: str,
        answers: list[list[str]],
    ) -> dict | None:
        """Complete the display card and append its durable answer summary."""
        with self._lock:
            part = self._question_part(call_id)
            if part is None or part.get("status") != "pending":
                return None
            reply = [list(answer) for answer in answers]
            completed = self._update({
                **part,
                "status": "completed",
                "response": {"answers": reply},
            })
            self._update({
                "id": self._part_id("question-summary", call_id),
                "message_id": self.message_id,
                "type": "question-summary",
                "tool_call_id": call_id,
                "questions": list(part["questions"]),
                "answers": reply,
            })
            return completed

    def terminate_question(self, call_id: str) -> dict | None:
        """Mark a dismissed, timed-out, or cancelled question as terminated."""
        return self._settle_question_display(call_id, status="terminated")

    def fail_question(self, call_id: str, error: str) -> dict | None:
        """Persist an interaction-service or answer-shape failure."""
        return self._settle_question_display(
            call_id,
            status="error",
            error=str(error),
        )

    def fail_tool(
        self,
        call_id: str,
        error: str,
        *,
        interrupted: bool = False,
    ) -> None:
        """Settle a tool as error, discarding partial interrupted output."""
        part = self._tool_part(call_id)
        if part is None:
            return
        start = part.get("state", {}).get("time", {}).get("start", time.time())
        self._update({
            **part,
            "state": {
                "status": "error",
                "input": dict(part.get("state", {}).get("input") or {}),
                "error": str(error),
                "interrupted": bool(interrupted),
                "time": {"start": start, "end": time.time()},
            },
        })

    def settle_tool(
        self,
        call_id: str,
        output: str,
        *,
        failed: bool,
        denied: bool = False,
        title: str = "",
        metadata: dict | None = None,
        attachments: list[dict] | None = None,
        continue_on_deny: bool = False,
    ) -> None:
        """Consume one tool-result/error event and update processor control state."""
        if failed:
            self.fail_tool(call_id, output, interrupted=False)
        else:
            self.complete_tool(
                call_id,
                output,
                title=title,
                metadata=metadata,
                attachments=attachments,
            )
        if denied and not continue_on_deny:
            self._blocked = True

    def process_result(self, *, needs_compaction: bool = False, fatal: bool = False) -> str:
        """Return InfCode-style compact/stop/continue after stream cleanup."""
        if needs_compaction:
            return "compact"
        if fatal or self._blocked:
            return "stop"
        return "continue"

    def interrupt_unsettled(self) -> int:
        """Turn every pending/running tool into a terminal interrupted error."""
        count = self.fail_unsettled("Tool execution aborted", interrupted=True)
        for part in list(self.message.get(PARTS_KEY, [])):
            if (
                isinstance(part, dict)
                and part.get("type") == "question"
                and part.get("status") == "pending"
            ):
                if self.terminate_question(str(part.get("tool_call_id") or "")) is not None:
                    count += 1
        return count

    def add_retry(
        self,
        attempt: int,
        error: Exception | str,
        next_at: float | None = None,
        *,
        provider_id: str = "",
    ) -> dict:
        """Persist a provider retry decision on the current assistant step."""
        detail = str(error) or type(error).__name__
        typed_error = (
            assistant_error_from_exception(
                error,
                provider_id=provider_id,
                is_retryable=True,
            )
            if isinstance(error, Exception)
            else {"name": "UnknownError", "data": {"message": detail[:4000]}}
        )
        part = {
            "id": self._part_id("retry", str(attempt)),
            "message_id": self.message_id,
            "type": "retry",
            "attempt": max(1, int(attempt)),
            "error": typed_error,
            "time": {"created": time.time()},
            # Compatibility/status fields retained for existing terminal and
            # HTTP clients while the durable shape follows InfCode RetryPart.
            "message": detail,
        }
        if next_at is not None:
            part["next"] = float(next_at)
        return self._update(part)

    def add_patch(self, snapshot: str, files: list[str]) -> dict | None:
        """Persist the files changed since this step's start snapshot."""
        clean = [str(path) for path in files if isinstance(path, str) and path]
        if not isinstance(snapshot, str) or not snapshot or not clean:
            return None
        return self._update({
            "id": self._part_id("patch"),
            "message_id": self.message_id,
            "type": "patch",
            "hash": snapshot,
            "files": clean,
        })

    def finish_step(
        self,
        reason: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        reasoning_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost: float | None = None,
        snapshot: str | None = None,
    ) -> dict:
        """Persist the terminal step boundary and normalized usage."""
        normalized_reason = str(reason or "stop")
        if normalized_reason in {"tool-calls", "tool_calls"} and not any(
            isinstance(part, dict) and part.get("type") == "tool"
            for part in self.message.get(PARTS_KEY, [])
        ):
            normalized_reason = "stop"
        self.message[ASSISTANT_FINISH_KEY] = normalized_reason
        timing = self.message.get(ASSISTANT_TIME_KEY)
        stored_created = (
            _finite_number(timing.get("created"))
            if isinstance(timing, dict)
            else None
        )
        created = self._step_started_at if stored_created is None else stored_created
        normalized_input = _finite_nonnegative_int(input_tokens)
        normalized_output = _finite_nonnegative_int(output_tokens)
        normalized_total = _finite_nonnegative_int(total_tokens)
        normalized_reasoning = _finite_nonnegative_int(reasoning_tokens)
        normalized_cache_read = _finite_nonnegative_int(cache_read_tokens)
        normalized_cache_write = _finite_nonnegative_int(cache_write_tokens)
        self.message[ASSISTANT_TIME_KEY] = {
            "created": created,
            "completed": time.time(),
        }
        part = {
            "id": self._part_id("step-finish"),
            "message_id": self.message_id,
            "type": "step-finish",
            "reason": normalized_reason,
            "tokens": {
                "input": normalized_input,
                "output": normalized_output,
                "total": normalized_total,
                **(
                    {"reasoning": normalized_reasoning}
                    if normalized_reasoning else {}
                ),
                **(
                    {
                        "cache": {
                            "read": normalized_cache_read,
                            "write": normalized_cache_write,
                        }
                    }
                    if normalized_cache_read or normalized_cache_write else {}
                ),
            },
            "time": {"start": self._step_started_at, "end": time.time()},
        }
        valid_cost = (
            isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(float(cost))
            and cost >= 0
        )
        if valid_cost:
            part["cost"] = float(cost)
        child_cost = self._child_cost()
        if valid_cost or child_cost > 0:
            self.message[ASSISTANT_COST_KEY] = (
                (float(cost) if valid_cost else 0.0) + child_cost
            )
        if snapshot:
            part["snapshot"] = snapshot
        updated = self._update(part)
        publish_assistant_state(self.message, self.publish)
        return updated

    def add_child_cost(self, amount: float) -> float:
        """Merge one settled child-Agent cost delta into the assistant owner."""
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or amount <= 0
        ):
            return self._child_cost()
        with self._lock:
            previous = self._child_cost()
            current_total = self.message.get(ASSISTANT_COST_KEY)
            model_cost = (
                max(0.0, float(current_total) - previous)
                if isinstance(current_total, (int, float))
                and not isinstance(current_total, bool)
                and math.isfinite(float(current_total))
                else 0.0
            )
            child_total = previous + float(amount)
            self.message[ASSISTANT_CHILD_COST_KEY] = child_total
            self.message[ASSISTANT_COST_KEY] = model_cost + child_total
            publish_assistant_state(self.message, self.publish)
            self._notify_message_updated()
            return child_total

    def _child_cost(self) -> float:
        value = self.message.get(ASSISTANT_CHILD_COST_KEY)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value > 0
        ):
            return float(value)
        return 0.0

    def _tool_part(self, call_id: str) -> dict | None:
        for part in self.message.get(PARTS_KEY, []):
            if (
                isinstance(part, dict)
                and part.get("type") == "tool"
                and part.get("call_id") == call_id
            ):
                return dict(part)
        return None

    def _tool_part_by_index(self, index: int) -> dict | None:
        for part in self.message.get(PARTS_KEY, []):
            if (
                isinstance(part, dict)
                and part.get("type") == "tool"
                and part.get("index") == index
            ):
                return dict(part)
        return None

    def _question_part(self, call_id: str) -> dict | None:
        for part in self.message.get(PARTS_KEY, []):
            if (
                isinstance(part, dict)
                and part.get("type") == "question"
                and part.get("tool_call_id") == call_id
            ):
                return dict(part)
        return None

    def _settle_question_display(
        self,
        call_id: str,
        *,
        status: str,
        error: str = "",
    ) -> dict | None:
        with self._lock:
            part = self._question_part(call_id)
            if part is None or part.get("status") != "pending":
                return None
            updated = {**part, "status": status}
            updated.pop("response", None)
            if status == "error":
                updated["error"] = str(error or "Question failed")
            return self._update(updated)

    def _part_id(self, kind: str, discriminator: str = "") -> str:
        seed = f"nz-coder-session-part:{self.message_id}:{kind}:{discriminator}"
        return f"part-{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex}"

    def _update(self, part: dict, *, publish: bool = True) -> dict:
        with self._lock:
            interaction_run_id = self.message.get(INTERACTION_RUN_ID_KEY)
            if isinstance(interaction_run_id, str) and interaction_run_id:
                part.setdefault("interaction_run_id", interaction_run_id)
            internal = self.message.get("_nz_internal") is True
            part.setdefault("visible", not internal)
            part.setdefault("internal", internal)
            part.setdefault("authoritative", True)
            normalized = upsert_message_part(self.message, part)
            if publish and self.publish is not None and not internal:
                self.publish("message.part.updated", {
                    "message_id": self.message_id,
                    "part": normalized,
                })
            self._notify_message_updated()
            return normalized

    def _notify_message_updated(self) -> None:
        """Report one committed stable mutation to the Session owner."""
        if self.on_message_updated is not None:
            self.on_message_updated(self.message)


def _arguments(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
