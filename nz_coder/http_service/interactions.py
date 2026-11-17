"""Thread-safe pending permission and question requests for HTTP sessions."""
from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.protocol.message_schema import project_public_protocol_value
from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError


class InteractionNotFoundError(LookupError):
    """Raised when an interaction request is absent or already resolved."""


@dataclass
class _PendingInteraction:
    request_id: str
    kind: str
    payload: dict[str, Any]
    created_at: float
    expires_at: float
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.request_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            **project_public_protocol_value(self.payload),
        }


class InteractionBroker:
    """Bridge synchronous Agent callbacks to asynchronous HTTP replies."""

    def __init__(
        self,
        *,
        session_id: str,
        event_bus: SessionEventBus,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.session_id = session_id
        self.event_bus = event_bus
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("interaction timeout must be a positive finite number")
        self.timeout_seconds = max(0.05, timeout)
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingInteraction] = {}
        self._closed = False
        self._accepting = False
        self._publisher = None

    def begin_run(self, publisher=None) -> None:
        """Allow interaction requests for one newly accepted Agent run."""
        with self._lock:
            if self._closed:
                raise RuntimeError("interaction broker is closed")
            if publisher is not None:
                self._publisher = publisher
            self._accepting = True

    def ask_permission(self, tool_name: str, tool_input: dict) -> str:
        """Publish a permission request and block until reply, timeout, or cancel."""
        payload = {
            "session_id": self.session_id,
            "permission": str(tool_name),
            "tool_input": copy.deepcopy(tool_input),
            "replies": ["once", "always", "reject"],
        }
        result = self._wait_for_reply("permission", payload, default="reject")
        if isinstance(result, PermissionInteractionError):
            raise result
        return str(result) if result in {"once", "always"} else "reject"

    def ask_question(self, questions: list[dict]) -> list[list[str]] | None:
        """Publish structured questions and block until reply, reject, or timeout."""
        from nz_coder.tools.question import current_question_request_id

        payload = {
            "session_id": self.session_id,
            "questions": copy.deepcopy(questions),
        }
        result = self._wait_for_reply(
            "question",
            payload,
            default=None,
            request_id=current_question_request_id(),
        )
        return copy.deepcopy(result) if isinstance(result, list) else None

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Return a stable snapshot of unresolved requests."""
        if kind not in (None, "permission", "question"):
            raise ValueError(f"unsupported interaction kind: {kind}")
        with self._lock:
            values = [
                item.to_dict()
                for item in self._pending.values()
                if kind is None or item.kind == kind
            ]
        return sorted(values, key=lambda item: (item["created_at"], item["id"]))

    @contextmanager
    def pending_snapshot(self) -> Iterator[list[dict[str, Any]]]:
        """保持待处理清单不变，供持有会话锁的调用方继续取得事件游标。"""
        with self._lock:
            yield self.list()

    def reply_permission(
        self,
        request_id: str,
        reply: str,
        *,
        message: str = "",
    ) -> None:
        """Resolve one permission request with InfCode-style reply semantics."""
        if not isinstance(reply, str) or reply not in {"once", "always", "reject"}:
            raise ValueError("permission reply must be once, always, or reject")
        item = None
        try:
            with self._lock:
                item = self._resolve(request_id, "permission", reply)
                self._publish(
                    "permission.replied",
                    {
                        "request_id": item.request_id,
                        "reply": reply,
                        "message": str(message or "")[:2000],
                    },
                )
        finally:
            if item is not None:
                item.event.set()

    def reply_question(self, request_id: str, answers: list[list[str]]) -> None:
        """Validate and resolve one pending structured question request."""
        item = self._get(request_id, "question")
        normalized = self._validate_answers(item.payload["questions"], answers)
        item = None
        try:
            with self._lock:
                item = self._resolve(request_id, "question", normalized)
                self._publish(
                    "question.replied",
                    {"request_id": item.request_id, "answers": copy.deepcopy(normalized)},
                )
        finally:
            if item is not None:
                item.event.set()

    def reject_question(self, request_id: str, *, reason: str = "rejected") -> None:
        """Dismiss one pending question without manufacturing an answer."""
        item = None
        try:
            with self._lock:
                item = self._resolve(request_id, "question", None)
                self._publish(
                    "question.rejected",
                    {
                        "request_id": item.request_id,
                        "reason": str(reason or "rejected")[:200],
                    },
                )
        finally:
            if item is not None:
                item.event.set()

    def cancel_all(self, reason: str = "cancelled", *, block_new: bool = False) -> int:
        """Reject every pending wait so abort/shutdown cannot deadlock the Agent."""
        with self._lock:
            if block_new:
                self._accepting = False
            pending = list(self._pending.values())
            self._pending.clear()
            for item in pending:
                item.result = (PermissionInteractionError(
                    "cancelled" if reason in {"aborted", "cancelled"} else "unavailable"
                ) if item.kind == "permission" else None)
                event_type = (
                    "permission.replied"
                    if item.kind == "permission"
                    else "question.rejected"
                )
                properties = {
                    "request_id": item.request_id,
                    "reason": str(reason or "cancelled")[:200],
                }
                if item.kind == "permission":
                    properties["reply"] = "reject"
                self._publish(event_type, properties)
        for item in pending:
            item.event.set()
        return len(pending)

    def close(self) -> None:
        """Reject pending requests and prevent new Agent waits."""
        with self._lock:
            self._closed = True
        self.cancel_all("disposed", block_new=True)

    def _wait_for_reply(
        self,
        kind: str,
        payload: dict[str, Any],
        default: Any,
        *,
        request_id: str = "",
    ) -> Any:
        now = time.time()
        item = _PendingInteraction(
            request_id=(
                str(request_id)[:160]
                if isinstance(request_id, str) and request_id
                else uuid.uuid4().hex
            ),
            kind=kind,
            payload=payload,
            created_at=now,
            expires_at=now + self.timeout_seconds,
        )
        with self._lock:
            if self._closed or not self._accepting:
                return PermissionInteractionError("unavailable") if kind == "permission" else default
            if item.request_id in self._pending:
                return PermissionInteractionError("unavailable") if kind == "permission" else default
            self._pending[item.request_id] = item
            # Registration and asked publication are one broker transaction.
            # Otherwise list/reply/cancel could resolve the visible request
            # before its asked event reaches clients.
            if not self._publish(f"{kind}.asked", item.to_dict()):
                self._pending.pop(item.request_id, None)
                return PermissionInteractionError("unavailable") if kind == "permission" else default

        from nz_coder.tools import current_tool_cancel_event
        cancel = current_tool_cancel_event()
        deadline = time.monotonic() + self.timeout_seconds
        reason = "timeout"
        while True:
            if cancel is not None and cancel.is_set():
                reason = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if item.event.wait(min(0.02, remaining) if cancel is not None else remaining):
                return item.result

        resolved_elsewhere = False
        with self._lock:
            current = self._pending.get(item.request_id)
            if current is not item:
                resolved_elsewhere = True
            else:
                self._pending.pop(item.request_id, None)
                item.result = PermissionInteractionError("cancelled" if reason == "cancelled" else "timed_out") if kind == "permission" else default
                if kind == "permission":
                    self._publish(
                        "permission.replied",
                        {
                            "request_id": item.request_id,
                            "reply": "reject",
                            "reason": reason,
                        },
                    )
                else:
                    self._publish(
                        "question.rejected",
                        {"request_id": item.request_id, "reason": reason},
                    )
        if resolved_elsewhere:
            item.event.wait()
            return item.result
        return item.result

    def _get(self, request_id: str, kind: str) -> _PendingInteraction:
        with self._lock:
            item = self._pending.get(str(request_id))
            if item is None or item.kind != kind:
                raise InteractionNotFoundError(str(request_id))
            return item

    def _resolve(self, request_id: str, kind: str, result: Any) -> _PendingInteraction:
        with self._lock:
            item = self._pending.get(str(request_id))
            if item is None or item.kind != kind:
                raise InteractionNotFoundError(str(request_id))
            self._pending.pop(item.request_id, None)
            item.result = copy.deepcopy(result)
            return item

    @staticmethod
    def _validate_answers(
        questions: list[dict],
        answers: list[list[str]],
    ) -> list[list[str]]:
        if not isinstance(answers, list) or len(answers) != len(questions):
            raise ValueError("answers must contain one string array per question")
        normalized: list[list[str]] = []
        for index, (question, answer) in enumerate(zip(questions, answers), 1):
            if not isinstance(answer, list) or not all(
                isinstance(value, str) for value in answer
            ):
                raise ValueError(f"answer {index} must be an array of strings")
            values = [value.strip() for value in answer if value.strip()]
            if not question.get("multiple") and len(values) > 1:
                raise ValueError(f"answer {index} allows at most one selection")
            normalized.append(values)
        return normalized

    def _publish(self, event_type: str, properties: dict[str, Any]) -> bool:
        try:
            (self._publisher or self.event_bus).publish(event_type, properties)
        except RuntimeError:
            return False
        return True
