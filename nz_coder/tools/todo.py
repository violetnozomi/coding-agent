"""Tool: todo - Session planning with task checklist."""
from __future__ import annotations

import json

from nz_coder.tools import register

_items_by_session: dict[tuple[str, str], list[dict]] = {}
_loaded_sessions: set[tuple[str, str]] = set()


def _session_key() -> tuple[str, str]:
    from nz_coder.runtime.workdir import current_workdir
    from nz_coder.sessions import active_session_id

    return str(current_workdir()), active_session_id() or "default"


def _items() -> list[dict]:
    key = _session_key()
    if key not in _loaded_sessions:
        _items_by_session[key] = _load_items(key[1])
        _loaded_sessions.add(key)
    return _items_by_session.setdefault(key, [])


def _load_items(session_id: str) -> list[dict]:
    from nz_coder.sessions import session_todo_path

    path = session_todo_path(session_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    loaded: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content", "")).strip()
        status = str(raw.get("status", "pending")).lower()
        priority = str(raw.get("priority", "medium")).lower()
        if not content or status not in ("pending", "in_progress", "completed", "cancelled"):
            continue
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        loaded.append({"content": content, "status": status, "priority": priority})
    return loaded[:20]


def _persist_items(session_id: str, items: list[dict]) -> str | None:
    from nz_coder.sessions import (
        session_todo_path,
        write_session_runtime_json,
    )

    path = session_todo_path(session_id)
    try:
        write_session_runtime_json(path, {"version": 1, "items": items})
    except Exception as exc:
        return str(exc)
    return None


def todo_update(items: list) -> str:
    validated = []
    ip_count = 0
    for i, raw in enumerate(items):
        content = str(raw.get("content", "")).strip()
        status = str(raw.get("status", "pending")).lower()
        priority = str(raw.get("priority", "medium")).lower()
        if not content:
            return f"Error: Item {i} has no content"
        if status not in ("pending", "in_progress", "completed", "cancelled"):
            return f"Error: Item {i} has invalid status '{status}'"
        if priority not in ("high", "medium", "low"):
            return f"Error: Item {i} has invalid priority '{priority}'"
        if status == "in_progress":
            ip_count += 1
        validated.append({"content": content, "status": status, "priority": priority})
    if ip_count > 1:
        return "Error: Only one item can be in_progress at a time"
    if len(validated) > 20:
        return "Error: Max 20 todo items"
    key = _session_key()
    error = _persist_items(key[1], validated)
    if error:
        return f"Error: failed to persist todo: {error}"
    _items_by_session[key] = validated
    return render()


def render() -> str:
    items = _items()
    if not items:
        return "No todos."
    lines = []
    for item in items:
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
            "cancelled": "[~]",
        }[item["status"]]
        lines.append(f"{marker} {item['content']}")
    done = sum(1 for t in items if t["status"] == "completed")
    lines.append(f"\n({done}/{len(items)} completed)")
    return "\n".join(lines)


def has_open_items() -> bool:
    return any(item["status"] not in ("completed", "cancelled") for item in _items())


def clear() -> str:
    """Clear the durable todo list for the active session."""
    key = _session_key()
    count = len(_items())
    error = _persist_items(key[1], [])
    if error:
        return f"Error: failed to persist todo: {error}"
    _items_by_session[key] = []
    _loaded_sessions.add(key)
    return f"Todo cleared ({count} items removed)."


def get_reminder(rounds_since: int):
    if not has_open_items():
        return None
    if rounds_since < 3:
        return None
    return "<reminder>Please update your todo list before continuing.</reminder>"


register(
    name="todo",
    description="Update the session task list. Keep one item in_progress. Use for multi-step work.",
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Complete list of todo items (replaces any previous list).",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Task description."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Optional priority; defaults to medium.",
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["items"],
    },
    handler=todo_update,
)
