"""Tool: todo - Session planning with task checklist."""
from __future__ import annotations

from nz_coder.tools import register

_items: list[dict] = []


def todo_update(items: list) -> str:
    global _items
    validated = []
    ip_count = 0
    for i, raw in enumerate(items):
        content = str(raw.get("content", "")).strip()
        status = str(raw.get("status", "pending")).lower()
        if not content:
            return f"Error: Item {i} has no content"
        if status not in ("pending", "in_progress", "completed"):
            return f"Error: Item {i} has invalid status '{status}'"
        if status == "in_progress":
            ip_count += 1
        validated.append({"content": content, "status": status})
    if ip_count > 1:
        return "Error: Only one item can be in_progress at a time"
    if len(validated) > 20:
        return "Error: Max 20 todo items"
    _items = validated
    return render()


def render() -> str:
    if not _items:
        return "No todos."
    lines = []
    for item in _items:
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
        lines.append(f"{marker} {item['content']}")
    done = sum(1 for t in _items if t["status"] == "completed")
    lines.append(f"\n({done}/{len(_items)} completed)")
    return "\n".join(lines)


def has_open_items() -> bool:
    return any(item["status"] != "completed" for item in _items)


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
                            "enum": ["pending", "in_progress", "completed"],
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
