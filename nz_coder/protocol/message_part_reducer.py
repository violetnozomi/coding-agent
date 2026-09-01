"""Deterministic projection of Message/Part snapshots and stream events."""
from __future__ import annotations

import copy
from collections import deque
from typing import Any


class MessagePartReducer:
    """Reduce local or remote Part events into one authoritative text view.

    The reducer deliberately has no terminal dependencies.  Event identities,
    attempt generations, part versions, and delta sequence numbers provide
    independent idempotency/fencing layers at reconnect and cancellation edges.
    """

    def __init__(self, *, identity_capacity: int = 8192) -> None:
        self._identity_capacity = max(128, int(identity_capacity))
        self._messages: dict[str, list[dict]] = {}
        self._message_order: list[str] = []
        self._generation: dict[str, int] = {}
        self._last_sequence = 0
        self._seen_event_ids: set[str] = set()
        self._seen_event_order: deque[str] = deque()
        self._seen_deltas: set[tuple[str, str, str, int]] = set()
        self._seen_delta_order: deque[tuple[str, str, str, int]] = deque()

    @property
    def visible_text(self) -> str:
        """Return visible assistant text in stable message/part order."""
        return "".join(
            str(part.get("text") or "")
            for message_id in self._message_order
            for part in self._messages.get(message_id, ())
            if part.get("type") == "text"
            and part.get("ignored") is not True
            and part.get("visible") is not False
        )

    def parts(self, message_id: str) -> list[dict]:
        """Return a defensive snapshot of one message's ordered parts."""
        return copy.deepcopy(self._messages.get(str(message_id), []))

    def clear(self) -> None:
        """Discard authoritative state and all replay identities."""
        self._messages.clear()
        self._message_order.clear()
        self._generation.clear()
        self._last_sequence = 0
        self._seen_event_ids.clear()
        self._seen_event_order.clear()
        self._seen_deltas.clear()
        self._seen_delta_order.clear()

    def replace_snapshot(self, records: list[dict] | None) -> None:
        """Atomically replace state from an authoritative message snapshot."""
        messages: dict[str, list[dict]] = {}
        order: list[str] = []
        generations: dict[str, int] = {}
        for record in records if isinstance(records, list) else ():
            if not isinstance(record, dict):
                continue
            info = record.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            message_id = str(info.get("id") or "")
            if not message_id or message_id in messages:
                continue
            selected = [
                self._snapshot_part(part)
                for part in record.get("parts", ())
                if isinstance(part, dict)
                and str(part.get("message_id") or message_id) == message_id
            ]
            if not any(part.get("type") == "text" for part in selected):
                content = info.get("content")
                if isinstance(content, str) and content:
                    selected.insert(0, {
                        "id": f"snapshot-{message_id}",
                        "message_id": message_id,
                        "type": "text",
                        "text": content,
                        "generation": 0,
                        "version": 0,
                        "status": "completed",
                        "visible": True,
                        "authoritative": True,
                    })
            messages[message_id] = selected
            order.append(message_id)
            generations[message_id] = max(
                (self._integer(part.get("generation")) for part in selected),
                default=0,
            )
        self._messages = messages
        self._message_order = order
        self._generation = generations
        self._seen_deltas.clear()
        self._seen_delta_order.clear()

    def apply_event(self, event: Any) -> bool:
        """Apply one Session event and report whether logical state changed."""
        event_type, properties, event_id, sequence = self._event_fields(event)
        if event_id and not self._remember_event(event_id):
            return False
        if sequence and sequence <= self._last_sequence:
            return False
        if sequence:
            self._last_sequence = sequence
        if event_type in {"message.part.created", "message.part.updated"}:
            part = properties.get("part")
            if not isinstance(part, dict):
                return False
            message_id = str(
                properties.get("message_id") or part.get("message_id") or ""
            )
            status = (
                "created"
                if event_type == "message.part.created"
                else self._updated_status(part)
            )
            return self._upsert(message_id, part, default_status=status)
        if event_type == "message.part.completed":
            return self._complete(properties)
        if event_type == "message.part.delta":
            return self._apply_delta(properties)
        if event_type == "message.part.removed":
            return self._remove(properties)
        return False

    def _upsert(
        self,
        message_id: str,
        value: dict,
        *,
        default_status: str = "streaming",
    ) -> bool:
        part_id = str(value.get("id") or "")
        if not message_id or not part_id:
            return False
        incoming = copy.deepcopy(value)
        incoming["message_id"] = message_id
        incoming.setdefault("status", default_status)
        incoming.setdefault("visible", incoming.get("ignored") is not True)
        incoming.setdefault("authoritative", True)
        generation = self._integer(incoming.get("generation"))
        current_generation = self._generation.get(message_id, 0)
        if generation < current_generation:
            return False
        if generation > current_generation:
            self._advance_generation(message_id, generation)
        parts = self._message_parts(message_id)
        for index, existing in enumerate(parts):
            if existing.get("id") != part_id:
                continue
            existing_version = self._integer(existing.get("version"))
            incoming_version = self._integer(incoming.get("version"))
            if incoming_version < existing_version:
                return False
            if incoming_version and incoming_version == existing_version:
                return False
            if incoming_version == existing_version and existing == incoming:
                return False
            parts[index] = incoming
            return True
        if incoming.get("type") == "text":
            attempt_id = str(incoming.get("attempt_id") or "")
            if attempt_id:
                parts[:] = [
                    part for part in parts
                    if part.get("type") != "text"
                    or self._integer(part.get("generation")) != generation
                    or str(part.get("attempt_id") or "") == attempt_id
                ]
        parts.append(incoming)
        return True

    def _complete(self, properties: dict) -> bool:
        message_id = str(properties.get("message_id") or "")
        part = properties.get("part")
        if isinstance(part, dict):
            completed = copy.deepcopy(part)
            completed["status"] = "completed"
            selected_message_id = (
                message_id or str(completed.get("message_id") or "")
            )
            if self._upsert(
                selected_message_id,
                completed,
                default_status="completed",
            ):
                return True
            return self._complete({
                "message_id": selected_message_id,
                "part_id": completed.get("id"),
                "generation": completed.get("generation"),
                "version": completed.get("version"),
            })
        part_id = str(properties.get("part_id") or "")
        if not message_id or not part_id:
            return False
        generation = self._integer(properties.get("generation"))
        current_generation = self._generation.get(message_id, 0)
        if generation < current_generation:
            return False
        if generation > current_generation:
            self._advance_generation(message_id, generation)
        for existing in self._messages.get(message_id, ()):
            if existing.get("id") != part_id:
                continue
            incoming_version = self._integer(properties.get("version"))
            existing_version = self._integer(existing.get("version"))
            if incoming_version and incoming_version < existing_version:
                return False
            if existing.get("status") == "completed" and (
                not incoming_version or incoming_version == existing_version
            ):
                return False
            existing["status"] = "completed"
            existing["version"] = max(incoming_version, existing_version)
            return True
        return False

    def _apply_delta(self, properties: dict) -> bool:
        if properties.get("field") not in {None, "text"}:
            return False
        delta = properties.get("delta")
        message_id = str(properties.get("message_id") or "msg-legacy-stream")
        part_id = str(properties.get("part_id") or "part-legacy-stream")
        if not isinstance(delta, str) or not delta:
            return False
        generation = self._integer(properties.get("generation"))
        current_generation = self._generation.get(message_id, 0)
        if generation < current_generation:
            return False
        if generation > current_generation:
            self._advance_generation(message_id, generation)
        attempt_id = str(properties.get("attempt_id") or "")
        generation_id = str(properties.get("generation_id") or "")
        delta_sequence = self._integer(properties.get("delta_sequence"))
        delta_key = (attempt_id, generation_id, part_id, delta_sequence)
        if delta_sequence and not self._remember_delta(delta_key):
            return False
        parts = self._message_parts(message_id)
        part = next((item for item in parts if item.get("id") == part_id), None)
        if part is None:
            part = {
                "id": part_id,
                "message_id": message_id,
                "type": "text",
                "text": "",
                "attempt_id": attempt_id,
                "generation_id": generation_id,
                "generation": generation,
                "version": 0,
                "status": "streaming",
                "visible": True,
                "authoritative": True,
            }
            parts.append(part)
        if self._integer(part.get("generation")) != generation:
            return False
        if attempt_id and str(part.get("attempt_id") or "") not in {"", attempt_id}:
            return False
        incoming_version = self._integer(properties.get("version"))
        if incoming_version and incoming_version <= self._integer(part.get("version")):
            return False
        part["text"] = str(part.get("text") or "") + delta
        if attempt_id:
            part["attempt_id"] = attempt_id
        if generation_id:
            part["generation_id"] = generation_id
        part["generation"] = generation
        part["version"] = max(incoming_version, self._integer(part.get("version")) + 1)
        part["status"] = "streaming"
        part["visible"] = True
        part["authoritative"] = True
        return True

    def _remove(self, properties: dict) -> bool:
        message_id = str(properties.get("message_id") or "")
        part_id = str(properties.get("part_id") or "")
        if not message_id or not part_id:
            return False
        parts = self._messages.get(message_id)
        if not parts:
            return False
        retained = [part for part in parts if part.get("id") != part_id]
        if len(retained) == len(parts):
            return False
        self._messages[message_id] = retained
        generation = self._integer(properties.get("generation"))
        if generation > self._generation.get(message_id, 0):
            self._generation[message_id] = generation
        return True

    def _advance_generation(self, message_id: str, generation: int) -> None:
        self._generation[message_id] = generation
        parts = self._message_parts(message_id)
        parts[:] = [
            part for part in parts
            if part.get("type") != "text"
            or self._integer(part.get("generation")) >= generation
        ]

    def _message_parts(self, message_id: str) -> list[dict]:
        if message_id not in self._messages:
            self._messages[message_id] = []
            self._message_order.append(message_id)
        return self._messages[message_id]

    def _remember_event(self, event_id: str) -> bool:
        if event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(event_id)
        self._seen_event_order.append(event_id)
        while len(self._seen_event_order) > self._identity_capacity:
            self._seen_event_ids.discard(self._seen_event_order.popleft())
        return True

    def _remember_delta(self, identity: tuple[str, str, str, int]) -> bool:
        if identity in self._seen_deltas:
            return False
        self._seen_deltas.add(identity)
        self._seen_delta_order.append(identity)
        while len(self._seen_delta_order) > self._identity_capacity:
            self._seen_deltas.discard(self._seen_delta_order.popleft())
        return True

    @staticmethod
    def _event_fields(event: Any) -> tuple[str, dict, str, int]:
        if isinstance(event, dict):
            meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
            properties = (
                event.get("properties")
                if isinstance(event.get("properties"), dict)
                else {}
            )
            return (
                str(event.get("type") or ""),
                properties,
                str(meta.get("event_id") or event.get("event_id") or ""),
                MessagePartReducer._integer(
                    meta.get("sequence", event.get("sequence"))
                ),
            )
        return (
            str(getattr(event, "type", "") or ""),
            getattr(event, "properties", {})
            if isinstance(getattr(event, "properties", {}), dict)
            else {},
            str(getattr(event, "event_id", "") or ""),
            MessagePartReducer._integer(getattr(event, "sequence", 0)),
        )

    @staticmethod
    def _snapshot_part(part: dict) -> dict:
        selected = copy.deepcopy(part)
        selected.setdefault(
            "status",
            MessagePartReducer._updated_status(selected, snapshot=True),
        )
        selected.setdefault("visible", selected.get("ignored") is not True)
        selected.setdefault("authoritative", True)
        return selected

    @staticmethod
    def _updated_status(part: dict, *, snapshot: bool = False) -> str:
        timing = part.get("time") if isinstance(part.get("time"), dict) else {}
        if "end" in timing or snapshot:
            return "completed"
        return "streaming"

    @staticmethod
    def _integer(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
