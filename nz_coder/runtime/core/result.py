"""Terminal result contracts shared by every Agent runtime host."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal
from nz_coder.protocol.public_error import PublicError


class RunStatus(str, Enum):
    """Normalized terminal states produced by the shared Runner."""

    COMPLETED = "completed"
    COMPLETED_UNVERIFIED = "completed_unverified"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    ERROR = "error"
    MAX_TURNS = "max_turns"


def normalize_run_status(value: object) -> RunStatus:
    """所有宿主使用同一终态转换；未知值明确归为错误。"""
    if isinstance(value, RunStatus):
        return value
    try:
        return RunStatus(str(value or "error"))
    except ValueError:
        return RunStatus.ERROR


@dataclass(frozen=True)
class TokenUsage:
    """Provider-independent token accounting for one call or complete run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"TokenUsage {name} must be a non-negative integer")

    @property
    def total_tokens(self) -> int:
        """Return the sum of the runtime's mutually exclusive token buckets."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cached_read_tokens
            + self.cached_write_tokens
            + self.reasoning_tokens
        )

    def add(self, other: TokenUsage) -> TokenUsage:
        """Return an immutable field-wise aggregate."""
        if not isinstance(other, TokenUsage):
            raise TypeError("TokenUsage.add requires TokenUsage")
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_read_tokens=self.cached_read_tokens + other.cached_read_tokens,
            cached_write_tokens=self.cached_write_tokens + other.cached_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass(frozen=True)
class SessionSettlementResult:
    """一次原提交重试的投影；idle不代表历史任务已完成。"""
    session_id: str
    interaction_run_id: str | None
    state: Literal["idle", "committed", "pending", "conflict"]
    error: PublicError | None = None


@dataclass(frozen=True)
class RunResult:
    """Stable terminal envelope returned to CLI, HTTP, SDK, and evaluation hosts."""

    status: RunStatus
    final_text: str
    messages: tuple[dict, ...] | list[dict]
    usage: TokenUsage
    session_id: str
    active_agent: str
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise TypeError("RunResult status must be a RunStatus")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("RunResult session_id must be non-empty")
        if not isinstance(self.active_agent, str) or not self.active_agent.strip():
            raise ValueError("RunResult active_agent must be non-empty")
        object.__setattr__(self, "messages", copy.deepcopy(tuple(self.messages)))
        object.__setattr__(self, "metadata", copy.deepcopy(self.metadata))


TERMINAL_RESULT_KEY = "_nz_terminal_result"


def _fallback_answer(context, verification: dict, snapshot, stop_reason: str) -> str:
    """提前取消或异常没有普通收尾结果，只恢复当前交互的可见内容。"""
    from nz_coder.protocol.message_schema import (
        DEFERRED_MODEL_TEXT_KEY, INTERACTION_RUN_ID_KEY, ensure_message_identities, replace_assistant_final_text, set_assistant_end_state,
    )
    from nz_coder.runtime.verification.completion_gate import host_execution_report, host_test_report

    messages = context.transcript
    current = [m for m in messages if m.get("role") == "assistant" and m.get(INTERACTION_RUN_ID_KEY) == context.interaction_run_id]
    draft = next((m for m in reversed(current) if m.get("_nz_internal") is not True and m.get("_nz_visible") is not False
                  and isinstance(m.get("content"), str) and m["content"].strip()), None)
    text = draft["content"] if draft is not None else ""
    if snapshot is not None and snapshot.latest_review is not None and not snapshot.latest_review.accepted:
        text = ""
    if context.request.agent.output_schema is not None:
        if current and (current[-1].get("_nz_visible") is False or current[-1].get(DEFERRED_MODEL_TEXT_KEY)):
            return ""
        return text
    if context.terminal_denied_commands.total:
        answer = host_execution_report(context.terminal_denied_commands, verification, snapshot, stop_reason=stop_reason)
    elif context.uses_receipt_based_delivery:
        answer = host_test_report(snapshot, status=context.terminal_status.value, stop_reason=stop_reason)
    else:
        return text

    # 原执行宿主已退出。把同一份正文写入会话，重开时仍能看到当时的执行记录。
    if draft is None and not answer:
        return ""
    if draft is None or draft is not current[-1] or draft.get("tool_calls"):
        draft = {"role": "assistant", "content": "", INTERACTION_RUN_ID_KEY: context.interaction_run_id}
        messages.append(draft)
    ensure_message_identities(messages, context.session.session_id)
    replace_assistant_final_text(draft, answer)
    set_assistant_end_state(draft, "interrupted" if context.terminal_status is RunStatus.CANCELLED else "errored")
    return answer


def freeze_terminal_result(context, raw_result: object = None) -> None:
    """在规范 Session 提交前冻结本次结果；不复制历史或嵌套终态载荷。"""
    from dataclasses import asdict
    from nz_coder.protocol.public_error import public_error_from_wire, to_public_error
    if TERMINAL_RESULT_KEY in context.session.metadata:
        return
    if raw_result is None:
        raw_result = context.prepared_terminal_result
    payload = raw_result if isinstance(raw_result, dict) else {}
    status = context.terminal_status
    metadata = copy.deepcopy(context.metadata)
    for key in (TERMINAL_RESULT_KEY, "_nz_http_terminal_result", "legacy_payload", "public_error", "structured"):
        metadata.pop(key, None)
    public = None
    if status is RunStatus.ERROR:
        public = public_error_from_wire(payload.get("public_error")) or to_public_error(context._terminal_error)
        metadata["public_error"] = public.to_dict()
    snapshot = context.verification.snapshot() if context.verification is not None else None
    verification = snapshot.to_dict() if snapshot is not None else copy.deepcopy(payload.get("verification") or {})
    context.terminal_denied_commands.add_to_verification(verification)
    if "structured" in payload:
        metadata["structured"] = copy.deepcopy(payload["structured"])
        verification["structured_output_source"] = "model"
    metadata.update(interaction_run_id=context.interaction_run_id,
                    runtime=copy.deepcopy(payload.get("runtime") or {}),
                    verification=verification,
                    authored_content=str(payload.get("authored_content") or ""),
                    raw_status=str(payload.get("status") or status.value))
    error = public.message if public is not None else ""
    answer = _fallback_answer(context, verification, snapshot, error) if raw_result is None else str(payload.get("content") or "")
    context.session.metadata[TERMINAL_RESULT_KEY] = {
        "session_id": context.session.session_id,
        "workspace": str(context.session.workspace),
        "interaction_run_id": context.interaction_run_id,
        "status": status.value, "last_error": error,
        "last_result": {"status": status.value, "answer": answer, "active_agent": context.active_agent,
                        "error": error, "usage": asdict(context.usage), "metadata": metadata},
    }
    context.prepared_terminal_result = None


def saved_terminal_result(payload: dict) -> dict | None:
    """所有宿主只读取身份、状态及规范提交 marker 一致的正式投影。"""
    from pathlib import Path
    from nz_coder.runtime.session.store import confirms_terminal_commit
    metadata = payload.get("metadata") or {}
    record = metadata.get(TERMINAL_RESULT_KEY)
    if not isinstance(record, dict):
        return None
    interaction = record.get("interaction_run_id")
    if (not isinstance(interaction, str) or not interaction
            or record.get("session_id") != payload.get("session_id")
            or record.get("workspace") != payload.get("workspace")
            or record.get("status") != payload.get("run_status")
            or not isinstance(record.get("last_error"), str)):
        return None
    result = record.get("last_result")
    if (not isinstance(result, dict) or result.get("status") != record["status"]
            or any(not isinstance(result.get(key), str) for key in ("answer", "active_agent", "error"))
            or not isinstance(result.get("metadata"), dict)
            or result["metadata"].get("interaction_run_id") != interaction):
        return None
    if not confirms_terminal_commit(payload, Path(record["workspace"]), record["session_id"], interaction):
        return None
    return copy.deepcopy(record)
