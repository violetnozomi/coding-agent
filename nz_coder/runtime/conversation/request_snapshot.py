"""Frozen prompt layers used to measure and install compaction candidates."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

from nz_coder.state.context import estimate_request_tokens
from nz_coder.runtime.verification.recovery import RepeatNotice


def message_version(messages: list) -> str:
    """Detect changes while an asynchronous summary is being generated."""
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrozenRequestSnapshot:
    """Keep request instructions and schemas fixed while replacing history."""

    system: str
    dynamic: str
    instruction_reminder: str
    source_version: str
    project_history: Callable[[list], list] = field(repr=False)
    _tools: tuple[dict, ...] = field(repr=False)
    suffix: tuple[dict, ...] = ()
    retained: tuple[dict, ...] = field(default=(), repr=False)
    working_memory_revision: int | None = None
    memory_scope_id: str = ""
    memory_generation: tuple | None = None
    repeat_notice: RepeatNotice | None = None

    @property
    def tools(self) -> list:
        return copy.deepcopy(list(self._tools))

    def matches_source(self, messages: list) -> bool:
        return message_version(messages) == self.source_version

    def project(self, messages: list) -> list:
        from nz_coder.runtime.conversation.prompt_builder import (
            inject_dynamic_context,
            inject_instruction_reminder,
        )

        history = self.project_history(copy.deepcopy(messages))
        has_user = any(message.get("role") == "user" for message in history)
        dynamic = "\n".join(part for part in (
            self.dynamic, self.repeat_notice.text if self.repeat_notice else "",
        ) if part)
        history = inject_dynamic_context(history, dynamic)
        system = self.system
        if self.instruction_reminder:
            if has_user:
                history = inject_instruction_reminder(history, self.instruction_reminder)
            else:
                system = "\n\n".join(part for part in (system, self.instruction_reminder) if part)
        return [{"role": "system", "content": system}, *history, *copy.deepcopy(self.suffix)]

    def restore_retained(self, messages: list) -> list:
        """Keep the in-flight processor attached when summary tails are copied."""
        owners = {message["_nz_message_id"]: message for message in self.retained}
        restored = []
        seen = set()
        for message in messages:
            identity = message.get("_nz_message_id")
            restored.append(owners.get(identity, message))
            seen.add(identity)
        restored.extend(owner for identity, owner in owners.items() if identity not in seen)
        return restored

    def estimate(self, messages: list) -> int:
        return estimate_request_tokens(self.project(messages), self.tools)


def acknowledge_usable_repeat_notice(snapshot, result, recovery, state, run, active_agent_name, messages):
    """正式提交之后确认；流式工具后的尾流失败不撤回已经送达的事实。"""
    notice = snapshot.repeat_notice
    if notice is None or result.finish_reason in {"error", "length"}:
        return
    if not str(result.content or "").strip() and not result.tool_calls:
        return
    for call in result.tool_calls:
        function = call.get("function", {})
        if not function.get("name") or not call.get("id"):
            return
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return
        if not isinstance(arguments, dict):
            return
    with run._state_lock:
        if recovery.repeat_source_current(notice.source, state, run, active_agent_name, messages):
            recovery.acknowledge_repeat_notice(notice.notice_id, notice.source)
