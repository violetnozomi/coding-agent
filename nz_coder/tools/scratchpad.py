"""Working Memory（第一层）：session 内的轻量暂存区。

Scratchpad 只保留两类高价值信息：
  - plan: 当前计划、下一步动作、已确认的关键事实
  - failure: 失败尝试或验证失败摘要

旧的 hypothesis / attempt / finding 仍接受，但会折叠到 plan，避免对 agent
暴露过细的分类心智负担。
"""
from __future__ import annotations

import json
import time

from nz_coder.tools import register

CATEGORIES = ("plan", "failure")
LEGACY_CATEGORY_ALIASES = {
    "hypothesis": "plan",
    "attempt": "plan",
    "finding": "plan",
}
_MAX_CONTENT_CHARS = 500
_MAX_PLAN_CHARS = 2000
_MAX_ENTRIES = 20
_MAX_PROMPT_CHARS = 2000


def _normalize_category(category: str) -> str | None:
    raw = str(category or "").strip().lower()
    if raw in CATEGORIES:
        return raw
    return LEGACY_CATEGORY_ALIASES.get(raw)


class Scratchpad:
    """Session-scoped working memory for the agent's active plan and failures."""

    def __init__(self) -> None:
        self._entries_by_session: dict[tuple[str, str], list[dict]] = {}
        self._loaded_sessions: set[tuple[str, str]] = set()

    @property
    def entries(self) -> list[dict]:
        return self._session_entries()

    def _session_key(self) -> tuple[str, str]:
        from nz_coder.runtime.workdir import current_workdir
        from nz_coder.sessions import active_session_id

        return str(current_workdir()), active_session_id() or "default"

    def _session_entries(self) -> list[dict]:
        key = self._session_key()
        if key not in self._loaded_sessions:
            self._entries_by_session[key] = self._load_entries(key[1])
            self._loaded_sessions.add(key)
        return self._entries_by_session.setdefault(key, [])

    def _load_entries(self, session_id: str) -> list[dict]:
        from nz_coder.sessions import session_scratchpad_path

        path = session_scratchpad_path(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw_entries = payload.get("entries", []) if isinstance(payload, dict) else []
        entries: list[dict] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            category = _normalize_category(str(raw.get("category", "")))
            content = str(raw.get("content", "")).strip()
            if category is None or not content:
                continue
            try:
                timestamp = float(raw.get("ts", 0.0))
            except (TypeError, ValueError):
                timestamp = 0.0
            entries.append({"ts": timestamp, "category": category, "content": content})
        return entries[-_MAX_ENTRIES:]

    def _persist_entries(self, session_id: str, entries: list[dict]) -> str | None:
        from nz_coder.sessions import (
            session_scratchpad_path,
            write_session_runtime_json,
        )

        path = session_scratchpad_path(session_id)
        try:
            write_session_runtime_json(path, {"version": 1, "entries": entries})
        except Exception as exc:
            return str(exc)
        return None

    def update(self, category: str, content: str) -> str:
        """Record a concise plan or failure note for the current session."""
        normalized = _normalize_category(category)
        if normalized is None:
            return f"Error: category must be one of {CATEGORIES}"
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "... (truncated)"
        key = self._session_key()
        entries = list(self._session_entries())
        entries.append({"ts": time.time(), "category": normalized, "content": content})
        if len(entries) > _MAX_ENTRIES:
            entries = entries[-_MAX_ENTRIES:]
        error = self._persist_entries(key[1], entries)
        if error:
            return f"Error: failed to persist scratchpad: {error}"
        self._entries_by_session[key] = entries
        preview = content[:80] + ("..." if len(content) > 80 else "")
        return f"Scratchpad updated [{normalized}]: {preview}"

    def replace_category(self, category: str, content: str, max_chars: int = 0) -> str:
        """Replace all entries in a category with one new note."""
        normalized = _normalize_category(category)
        if normalized is None:
            return f"Error: category must be one of {CATEGORIES}"
        if max_chars <= 0:
            max_chars = _MAX_PLAN_CHARS if normalized == "plan" else _MAX_CONTENT_CHARS
        if len(content) > max_chars:
            content = content[:max_chars] + "... (truncated)"
        key = self._session_key()
        entries = [e for e in self._session_entries() if e["category"] != normalized]
        entries.append({"ts": time.time(), "category": normalized, "content": content})
        if len(entries) > _MAX_ENTRIES:
            entries = entries[-_MAX_ENTRIES:]
        error = self._persist_entries(key[1], entries)
        if error:
            return f"Error: failed to persist scratchpad: {error}"
        self._entries_by_session[key] = entries
        preview = content[:80] + ("..." if len(content) > 80 else "")
        return f"Scratchpad [{normalized}] replaced: {preview}"

    def read(self) -> str:
        """Return all scratchpad content for the current session."""
        entries = self._session_entries()
        if not entries:
            return "Scratchpad is empty."
        lines = [f"# Scratchpad ({len(entries)} entries)", ""]
        for entry in entries:
            ts_str = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
            lines.append(f"[{ts_str}] [{entry['category']}] {entry['content']}")
        return "\n".join(lines)

    def _format_entry(self, entry: dict) -> str:
        ts_str = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
        return f"- [{entry['category']}] ({ts_str}) {entry['content']}"

    def build_prompt_block(self) -> str:
        """Generate the prompt block injected each turn."""
        entries = self._session_entries()
        if not entries:
            return ""
        header = (
            "\n## Working Memory (Scratchpad)\n"
            "Keep the current plan and any important failure notes in mind.\n\n"
        )
        total_budget = max(0, _MAX_PROMPT_CHARS - len(header))
        if total_budget <= 0:
            return ""

        plan_entries = [entry for entry in entries if entry["category"] == "plan"]
        other_entries = [entry for entry in entries if entry["category"] != "plan"]
        plan_budget = min(1200, max(0, total_budget - 200))
        other_budget = total_budget
        lines: list[str] = []

        for entry in plan_entries:
            line = self._format_entry(entry)
            if len(line) + 1 > plan_budget:
                line = line[:max(0, plan_budget - 17)] + "... (truncated)"
            if line:
                lines.append(line)
                other_budget -= min(len(line) + 1, other_budget)
                break

        other_lines: list[str] = []
        for entry in reversed(other_entries):
            line = self._format_entry(entry)
            if len(line) + 1 > other_budget:
                break
            other_lines.append(line)
            other_budget -= len(line) + 1
        other_lines.reverse()
        lines.extend(other_lines)

        if not lines:
            return ""
        return header + "\n".join(lines) + "\n"

    def clear(self) -> str:
        """Clear all scratchpad content for the current session."""
        key = self._session_key()
        count = len(self._session_entries())
        error = self._persist_entries(key[1], [])
        if error:
            return f"Error: failed to persist scratchpad: {error}"
        self._entries_by_session[key] = []
        self._loaded_sessions.add(key)
        return f"Scratchpad cleared ({count} entries removed)."


scratchpad = Scratchpad()


def set_agent_loop(loop) -> None:  # noqa: ARG001
    pass


def _update_scratchpad(category: str, content: str) -> str:
    return scratchpad.update(category, content)


def _read_scratchpad() -> str:
    return scratchpad.read()


register(
    name="update_scratchpad",
    description=(
        "Record session working notes. Prefer category='plan' for the active plan or confirmed facts, "
        "and category='failure' for failed attempts or verification failures."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(CATEGORIES),
                "description": (
                    "plan: active plan or important confirmed fact; "
                    "failure: what failed and why"
                ),
            },
            "content": {
                "type": "string",
                "description": "Your note. Keep it concise (max 500 chars).",
            },
        },
        "required": ["category", "content"],
    },
    handler=_update_scratchpad,
)

register(
    name="read_scratchpad",
    description=(
        "Read all entries in your working memory scratchpad. "
        "The scratchpad is also shown automatically each turn; "
        "only call this explicitly if you need the full untruncated history."
    ),
    parameters={"type": "object", "properties": {}},
    handler=_read_scratchpad,
    execution="read",
)
