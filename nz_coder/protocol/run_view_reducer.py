"""Authoritative current-interaction reducer shared by local and remote UI."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from nz_coder.protocol.message_part_reducer import MessagePartReducer


@dataclass
class RunViewState:
    """Complete renderable state for one user submission."""

    interaction_run_id: str = ""
    status: str = "idle"
    tool_parts: dict[str, dict] = field(default_factory=dict)
    retries: dict[str, dict] = field(default_factory=dict)
    pending_question: dict | None = None
    pending_permission: dict | None = None
    assistant_errors: dict[str, dict] = field(default_factory=dict)
    assistant_messages: dict[str, dict] = field(default_factory=dict)
    active_agent: str = ""
    terminal: dict | None = None
    last_sequence: int = 0


class RunViewReducer:
    """Reduce snapshots and SessionEvents into one UI-independent run view."""

    def __init__(self) -> None:
        self.text = MessagePartReducer()
        self._state = RunViewState()

    @property
    def state(self) -> RunViewState:
        return copy.deepcopy(self._state)

    @property
    def visible_text(self) -> str:
        return self.text.visible_text

    def clear(self) -> None:
        self.text.clear()
        self._state = RunViewState()

    def replace_snapshot(self, snapshot: dict | None) -> None:
        """Replace from the snapshot ``run`` object, never its timeline."""
        run = snapshot if isinstance(snapshot, dict) else {}
        interaction = str(run.get("interaction_run_id") or "")
        snapshot_sequence = self._integer(run.get("snapshot_sequence"))
        messages = run.get("messages") if isinstance(run.get("messages"), list) else []
        self.text.replace_snapshot(
            messages,
            interaction_run_id=interaction,
            last_sequence=snapshot_sequence,
        )
        state = RunViewState(
            interaction_run_id=interaction,
            status=str(run.get("status") or "idle"),
            last_sequence=snapshot_sequence,
        )
        for record in messages:
            if not isinstance(record, dict):
                continue
            info = record.get("info") if isinstance(record.get("info"), dict) else {}
            message_id = str(info.get("id") or "")
            error = info.get("error")
            if message_id and isinstance(error, dict):
                state.assistant_errors[message_id] = copy.deepcopy(error)
            if message_id:
                state.assistant_messages[message_id] = copy.deepcopy(info)
            if isinstance(info.get("agent"), str):
                state.active_agent = info["agent"]
            for part in record.get("parts", []):
                self._restore_part(state, part)
        for part in run.get("parts", []) if isinstance(run.get("parts"), list) else []:
            self._restore_part(state, part)
        pending = run.get("pending") if isinstance(run.get("pending"), dict) else {}
        permissions = pending.get("permissions")
        questions = pending.get("questions")
        if isinstance(permissions, list) and permissions:
            state.pending_permission = copy.deepcopy(permissions[0])
        if isinstance(questions, list) and questions:
            state.pending_question = copy.deepcopy(questions[0])
        self._state = state

    def apply_event(self, event: Any) -> bool:
        event_type, properties, interaction, sequence = self._fields(event)
        if (
            self._state.interaction_run_id
            and interaction
            and interaction != self._state.interaction_run_id
        ):
            return False
        if sequence and sequence <= self._state.last_sequence:
            return False
        if sequence:
            self._state.last_sequence = sequence
        if not self._state.interaction_run_id and interaction:
            self._state.interaction_run_id = interaction
        changed = self.text.apply_event(event)
        if event_type == "session.run.started":
            self._state.status = "running"
            self._state.terminal = None
            return True
        if event_type in {"session.run.status", "session.run.settled"}:
            status = str(properties.get("status") or self._state.status)
            self._state.status = status
            if status in {"completed", "failed", "cancelled"}:
                self._state.terminal = {
                    "status": status,
                    **copy.deepcopy(properties),
                }
            return True
        if event_type in {
            "session.run.completed", "session.run.failed", "session.run.cancelled",
        }:
            status = {
                "session.run.completed": "completed",
                "session.run.failed": "failed",
                "session.run.cancelled": "cancelled",
            }[event_type]
            self._state.status = status
            self._state.terminal = {"status": status, **copy.deepcopy(properties)}
            return True
        if event_type == "message.updated":
            info = properties.get("info") if isinstance(properties.get("info"), dict) else {}
            message_id = str(info.get("id") or properties.get("message_id") or "")
            if message_id and isinstance(info.get("error"), dict):
                self._state.assistant_errors[message_id] = copy.deepcopy(info["error"])
            elif message_id:
                self._state.assistant_errors.pop(message_id, None)
            if message_id:
                self._state.assistant_messages[message_id] = copy.deepcopy(info)
            if isinstance(info.get("agent"), str):
                self._state.active_agent = info["agent"]
            return True
        if event_type in {"message.part.created", "message.part.updated", "message.part.completed"}:
            part = properties.get("part")
            if isinstance(part, dict):
                selected = copy.deepcopy(part)
                selected.setdefault(
                    "message_id",
                    str(properties.get("message_id") or ""),
                )
                if not selected.get("id") and selected.get("type") == "tool":
                    selected["id"] = str(
                        selected.get("call_id")
                        or f"index-{selected.get('index', 0)}"
                    )
                if selected.get("type") != "retry":
                    self._state.retries.clear()
                self._restore_part(self._state, selected)
                return True
        if event_type == "session.tool.started":
            call_id = str(
                properties.get("tool_call_id")
                or f"index-{properties.get('index', 0)}"
            )
            self._state.tool_parts[call_id] = {
                "id": call_id,
                "type": "tool",
                "tool": str(properties.get("name") or "tool"),
                "call_id": call_id,
                "index": properties.get("index", 0),
                "state": {
                    "status": "running",
                    "title": properties.get("summary") or properties.get("name") or "tool",
                    "time": {"start": properties.get("started_at")},
                },
                "event": copy.deepcopy(properties),
            }
            return True
        if event_type == "session.tool.completed":
            call_id = str(
                properties.get("tool_call_id")
                or f"index-{properties.get('index', 0)}"
            )
            selected_key, existing = next((
                (key, part)
                for key, part in self._state.tool_parts.items()
                if key == call_id or str(part.get("call_id") or "") == call_id
            ), (call_id, {}))
            state = (
                copy.deepcopy(existing.get("state"))
                if isinstance(existing.get("state"), dict)
                else {}
            )
            state.update({
                "status": str(properties.get("status") or "completed"),
                "output": str(properties.get("output") or ""),
            })
            self._state.tool_parts[selected_key] = {
                **copy.deepcopy(existing),
                "id": call_id,
                "type": "tool",
                "tool": str(
                    properties.get("name")
                    or existing.get("tool")
                    or "tool"
                ),
                "call_id": call_id,
                "state": state,
                "event": {
                    **copy.deepcopy(existing.get("event", {})),
                    **copy.deepcopy(properties),
                },
            }
            return True
        if event_type == "message.part.removed":
            part_id = str(properties.get("part_id") or "")
            self._state.tool_parts.pop(part_id, None)
            for key, part in tuple(self._state.tool_parts.items()):
                if str(part.get("id") or "") == part_id:
                    self._state.tool_parts.pop(key, None)
            self._state.retries.pop(part_id, None)
            return changed
        if event_type in {"permission.asked", "session.permission.asked"}:
            self._state.pending_permission = copy.deepcopy(properties)
            return True
        if event_type in {
            "permission.replied", "permission.resolved", "permission.rejected",
        }:
            self._state.pending_permission = None
            return True
        if event_type in {"question.asked", "session.question.asked"}:
            self._state.pending_question = copy.deepcopy(properties)
            return True
        if event_type in {
            "question.replied", "question.resolved", "question.rejected",
        }:
            self._state.pending_question = None
            return True
        if event_type in {
            "agent.switched", "session.agent.switched", "agent.handoff",
        }:
            self._state.active_agent = str(
                properties.get("to")
                or properties.get("target")
                or properties.get("agent")
                or ""
            )
            return True
        return changed

    @staticmethod
    def _restore_part(state: RunViewState, part: object) -> None:
        if not isinstance(part, dict):
            return
        if part.get("internal") is True or part.get("visible") is False:
            return
        part_id = str(part.get("id") or "")
        part_type = part.get("type")
        if part_type == "tool" and part_id:
            state.tool_parts[part_id] = copy.deepcopy(part)
        elif part_type == "retry" and part_id:
            state.retries[part_id] = copy.deepcopy(part)
        elif part_type == "question" and part.get("status") == "pending":
            state.pending_question = copy.deepcopy(part)

    @staticmethod
    def _fields(event: Any) -> tuple[str, dict, str, int]:
        if isinstance(event, dict):
            meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
            properties = event.get("properties") if isinstance(event.get("properties"), dict) else {}
            return (
                str(event.get("type") or ""),
                properties,
                str(meta.get("interaction_run_id") or meta.get("run_id") or ""),
                int(meta.get("sequence") or 0),
            )
        return (
            str(getattr(event, "type", "") or ""),
            getattr(event, "properties", {}) if isinstance(getattr(event, "properties", {}), dict) else {},
            str(getattr(event, "run_id", "") or ""),
            int(getattr(event, "sequence", 0) or 0),
        )

    @staticmethod
    def _integer(value: Any) -> int:
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )
