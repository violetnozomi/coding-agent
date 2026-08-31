"""Append-only, session-scoped Agent lineage persistence."""
from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path

from nz_coder.foundation.json_safety import (
    json_safe_value,
    reject_nonstandard_json_constant,
)


_ENTRY_TYPES = frozenset({
    "run_started", "handoff", "terminal", "child_outcome", "run_finished",
    "memory_outcome_digest", "memory_review_receipt", "client_notice",
    "artifact_ledger", "invariant_violation",
})


class SessionLineage:
    """Durable ordered facts for handoffs and child outcomes in one Session."""

    def __init__(self, path: Path, session_id: str):
        self.path = Path(path)
        self.session_id = str(session_id)
        self._lock = threading.RLock()
        self._entries = self._load()

    def append(self, entry_type: str, payload: dict | None = None) -> dict:
        if entry_type not in _ENTRY_TYPES:
            raise ValueError(f"Unsupported lineage entry type: {entry_type}")
        with self._lock:
            previous = self._entries[-1]["id"] if self._entries else ""
            entry = {
                "id": f"lineage-{uuid.uuid4().hex}",
                "session_id": self.session_id,
                "sequence": len(self._entries) + 1,
                "timestamp": time.time(),
                "type": entry_type,
                "parent_id": previous,
                "payload": _bounded_payload(payload or {}),
            }
            encoded = (
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            if len(encoded) > 64 * 1024:
                raise ValueError("Lineage entry exceeds 64 KiB")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._entries.append(entry)
            return json.loads(json.dumps(entry))

    def entries(self) -> list[dict]:
        with self._lock:
            return json.loads(json.dumps(self._entries))

    def append_unique(
        self,
        entry_type: str,
        unique_key: str,
        payload: dict | None = None,
    ) -> dict | None:
        """Append one idempotent outcome entry keyed within its type."""
        key = str(unique_key or "").strip()
        if not key:
            raise ValueError("Lineage unique key cannot be empty")
        with self._lock:
            existing = next(
                (
                    entry for entry in self._entries
                    if entry["type"] == entry_type
                    and entry["payload"].get("unique_key") == key
                ),
                None,
            )
            if existing is not None:
                return None
            return self.append(entry_type, {**dict(payload or {}), "unique_key": key})

    def recover_active_agent(self, default: str) -> str:
        """Resume only an interrupted run; settled runs start from the graph root."""
        active, _depth = self.recover_open_agent_state(default)
        return active

    def recover_open_agent_state(self, default: str) -> tuple[str, int]:
        """Return active role and durable as-tool depth for an interrupted run."""
        active = str(default)
        call_depth = 0
        open_run = False
        for entry in self._entries:
            if entry["type"] == "run_started":
                open_run = True
                active = str(entry["payload"].get("agent") or default)
                if not entry["payload"].get("resumed"):
                    call_depth = 0
            elif entry["type"] == "handoff" and open_run:
                active = str(entry["payload"].get("to") or active)
                kind = entry["payload"].get("kind")
                if kind == "as-tool":
                    call_depth += 1
                elif kind == "as-tool-return":
                    call_depth = max(0, call_depth - 1)
            elif entry["type"] == "run_finished":
                open_run = False
                active = str(default)
                call_depth = 0
        return (
            (active, call_depth)
            if open_run
            else (str(default), 0)
        )

    def _load(self) -> list[dict]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Lineage journal exceeds 16 MiB")
        lines = raw.splitlines(keepends=True)
        entries: list[dict] = []
        for index, line in enumerate(lines):
            if not line.endswith(b"\n") and index == len(lines) - 1:
                break
            try:
                entry = json.loads(
                    line,
                    parse_constant=reject_nonstandard_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"Invalid lineage entry at line {index + 1}") from exc
            expected_parent = entries[-1]["id"] if entries else ""
            if not _valid_entry(
                entry,
                session_id=self.session_id,
                sequence=index + 1,
                parent_id=expected_parent,
            ):
                raise ValueError(f"Invalid lineage chain at line {index + 1}")
            entries.append(entry)
            if len(entries) > 100_000:
                raise ValueError("Lineage journal contains more than 100000 entries")
        return entries


class AgentCallStackStore:
    """Atomic durable storage for bounded `as-tool` caller frames."""

    def __init__(self, path: Path, session_id: str):
        self.path = Path(path)
        self.session_id = str(session_id)
        self._lock = threading.RLock()

    def save(self, frames: list[dict]) -> None:
        with self._lock:
            if len(frames) > 8:
                raise ValueError("Agent call stack exceeds 8 frames")
            payload = {
                "version": 1,
                "session_id": self.session_id,
                "frames": frames,
            }
            encoded = json.dumps(
                json_safe_value(payload),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > 16 * 1024 * 1024:
                raise ValueError("Agent call stack exceeds 16 MiB")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.replace(temporary, self.path)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def load(self) -> list[dict]:
        with self._lock:
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                return []
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("Agent call stack exceeds 16 MiB")
            try:
                payload = json.loads(
                    raw,
                    parse_constant=reject_nonstandard_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError("Invalid Agent call stack") from exc
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("session_id") != self.session_id
                or not isinstance(payload.get("frames"), list)
                or len(payload["frames"]) > 8
            ):
                raise ValueError("Invalid Agent call stack envelope")
            frames = payload["frames"]
            for frame in frames:
                if (
                    not isinstance(frame, dict)
                    or not isinstance(frame.get("agent"), str)
                    or not isinstance(frame.get("target"), str)
                    or not isinstance(frame.get("messages"), list)
                    or not all(isinstance(message, dict) for message in frame["messages"])
                ):
                    raise ValueError("Invalid Agent call stack frame")
            return json.loads(json.dumps(frames))


def _bounded_payload(payload: dict) -> dict:
    decoded = json_safe_value(payload)
    if not isinstance(decoded, dict):
        raise ValueError("Lineage payload must be an object")
    return decoded


def _valid_entry(
    entry: object,
    *,
    session_id: str,
    sequence: int,
    parent_id: str,
) -> bool:
    return bool(
        isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and entry["id"].startswith("lineage-")
        and entry.get("session_id") == session_id
        and entry.get("sequence") == sequence
        and entry.get("parent_id") == parent_id
        and entry.get("type") in _ENTRY_TYPES
        and isinstance(entry.get("payload"), dict)
        and isinstance(entry.get("timestamp"), (int, float))
        and not isinstance(entry.get("timestamp"), bool)
        and math.isfinite(float(entry["timestamp"]))
    )
