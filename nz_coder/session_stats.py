"""Read-only InfCode-style usage statistics over persisted NZ-Coder Sessions."""
from __future__ import annotations

import math
import statistics
import time
from pathlib import Path
from typing import Any

from nz_coder.message_schema import message_records
from nz_coder.sessions import list_sessions, load_session, session_subagent_dir

_MAX_SESSIONS = 10_000


def aggregate_session_stats(days: int | None = None) -> dict[str, Any]:
    """Aggregate persisted top-level and child usage without network access."""
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 0):
        raise ValueError("days must be a non-negative integer")
    effective_days = 1 if days == 0 else days
    cutoff = (
        time.time() - effective_days * 86_400
        if effective_days is not None
        else 0.0
    )
    stats = _empty_stats(effective_days)
    per_session_tokens: list[int] = []
    timestamps: list[float] = []

    for path in list_sessions(limit=_MAX_SESSIONS):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        payload = load_session(path.stem)
        if not isinstance(payload, dict):
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = []
        message_times = _message_timestamps(messages, path.stem)
        activity = max(message_times) if message_times else modified
        if cutoff and activity < cutoff:
            continue
        stats["top_level_sessions"] += 1
        stats["total_messages"] += len(messages)
        timestamps.extend(message_times or [modified])
        session_tokens = _consume_parent_messages(stats, messages, path.stem)

        child_root = session_subagent_dir(path.stem)
        if child_root.exists():
            for child_path in sorted(child_root.glob("*/state.json")):
                child = _read_child_state(child_path)
                if child is None:
                    continue
                updated = _timestamp(child.get("updated_at")) or modified
                if cutoff and updated < cutoff:
                    continue
                stats["child_sessions"] += 1
                child_messages = child.get("messages")
                if isinstance(child_messages, list):
                    stats["total_messages"] += len(child_messages)
                child_tokens = _consume_child_state(stats, child)
                per_session_tokens.append(child_tokens)
                timestamps.append(updated)
        per_session_tokens.append(session_tokens)

    stats["total_sessions"] = stats["top_level_sessions"] + stats["child_sessions"]
    total_token_count = sum(stats["total_tokens"].values())
    stats["tokens_per_session"] = (
        total_token_count / stats["total_sessions"] if stats["total_sessions"] else 0.0
    )
    stats["median_tokens_per_session"] = (
        float(statistics.median(per_session_tokens)) if per_session_tokens else 0.0
    )
    if timestamps:
        stats["date_range"] = {
            "earliest": min(timestamps),
            "latest": max(timestamps),
        }
        if effective_days is None:
            stats["days"] = max(1, math.ceil((max(timestamps) - min(timestamps)) / 86_400))
    stats["cost_per_day"] = stats["total_cost"] / max(1, stats["days"] or 1)
    stats["complete_cost"] = stats["unpriced_assistant_messages"] == 0
    return stats


def render_session_stats(stats: dict[str, Any], *, model_limit: int = 10, tool_limit: int = 10) -> str:
    """Render one compact terminal-safe statistics report."""
    tokens = stats["total_tokens"]
    cost_suffix = "" if stats.get("complete_cost") else " (known requests only)"
    lines = [
        "Session statistics",
        (
            f"Sessions: {stats['total_sessions']} "
            f"(top-level {stats['top_level_sessions']}, child {stats['child_sessions']})"
        ),
        f"Messages: {stats['total_messages']}",
        f"Cost: ${stats['total_cost']:.6f}{cost_suffix}",
        f"Average cost/day: ${stats['cost_per_day']:.6f}",
        (
            "Tokens: "
            f"input {tokens['input']}, output {tokens['output']}, "
            f"reasoning {tokens['reasoning']}, cache read {tokens['cache_read']}, "
            f"cache write {tokens['cache_write']}"
        ),
        (
            f"Average/median tokens per Session: "
            f"{stats['tokens_per_session']:.0f}/{stats['median_tokens_per_session']:.0f}"
        ),
    ]
    if stats.get("unattributed_background_cost", 0) > 0:
        lines.append(
            "Unattributed background cost: "
            f"${stats['unattributed_background_cost']:.6f}"
        )
    models = sorted(
        stats["model_usage"].items(),
        key=lambda item: (-item[1]["messages"], item[0]),
    )[: max(0, int(model_limit))]
    if models:
        lines.append("Models:")
        for name, usage in models:
            lines.append(
                f"- {name}: {usage['messages']} message(s), "
                f"{sum(usage['tokens'].values())} tokens, ${usage['cost']:.6f}"
            )
    tools = sorted(
        stats["tool_usage"].items(),
        key=lambda item: (-item[1], item[0]),
    )[: max(0, int(tool_limit))]
    if tools:
        lines.append("Tools: " + ", ".join(f"{name} {count}" for name, count in tools))
    return "\n".join(lines)


def _empty_stats(days: int | None) -> dict[str, Any]:
    return {
        "total_sessions": 0,
        "top_level_sessions": 0,
        "child_sessions": 0,
        "total_messages": 0,
        "total_cost": 0.0,
        "priced_assistant_messages": 0,
        "unpriced_assistant_messages": 0,
        "unattributed_background_cost": 0.0,
        "total_tokens": _empty_tokens(),
        "model_usage": {},
        "tool_usage": {},
        "date_range": {},
        "days": days or 0,
        "cost_per_day": 0.0,
        "tokens_per_session": 0.0,
        "median_tokens_per_session": 0.0,
        "complete_cost": True,
    }


def _consume_parent_messages(stats: dict[str, Any], messages: list[dict], session_id: str) -> int:
    session_total = 0
    for raw, record in zip(messages, message_records(messages, session_id)):
        info = record["info"]
        if info.get("role") == "assistant":
            cost = _number(info.get("cost"))
            if cost is None:
                stats["unpriced_assistant_messages"] += 1
            else:
                stats["priced_assistant_messages"] += 1
                stats["total_cost"] += cost
            tokens = _tokens(info.get("tokens"))
            _add_tokens(stats["total_tokens"], tokens)
            session_total += sum(tokens.values())
            model_key = _model_key(info)
            model = _model_usage(stats, model_key)
            model["messages"] += 1
            _add_tokens(model["tokens"], tokens)
            step_costs = [
                _number(part.get("cost"))
                for part in record["parts"]
                if isinstance(part, dict) and part.get("type") == "step-finish"
            ]
            known_steps = [item for item in step_costs if item is not None]
            if known_steps:
                model["cost"] += sum(known_steps)
            elif cost is not None and not _number(raw.get("_nz_child_cost")):
                model["cost"] += cost
        for part in record["parts"]:
            if isinstance(part, dict) and part.get("type") == "tool":
                name = str(part.get("tool") or "unknown")
                stats["tool_usage"][name] = stats["tool_usage"].get(name, 0) + 1
    return session_total


def _message_timestamps(messages: list[dict], session_id: str) -> list[float]:
    """Read authoritative message times, falling back at the caller boundary."""
    timestamps: list[float] = []
    for record in message_records(messages, session_id):
        timing = record.get("info", {}).get("time")
        if not isinstance(timing, dict):
            continue
        for key in ("created", "completed"):
            value = timing.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and value >= 0
            ):
                timestamps.append(float(value))
    return timestamps


def _consume_child_state(stats: dict[str, Any], child: dict[str, Any]) -> int:
    tokens = _tokens(child.get("tokens"))
    _add_tokens(stats["total_tokens"], tokens)
    model = _model_usage(stats, _model_key(child))
    model["messages"] += sum(
        1
        for message in child.get("messages", [])
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    _add_tokens(model["tokens"], tokens)
    cost = _number(child.get("cost"))
    if cost is not None:
        model["cost"] += cost
        if child.get("background"):
            stats["unattributed_background_cost"] += cost
    return sum(tokens.values())


def _model_usage(stats: dict[str, Any], key: str) -> dict[str, Any]:
    return stats["model_usage"].setdefault(
        key,
        {"messages": 0, "tokens": _empty_tokens(), "cost": 0.0},
    )


def _model_key(value: dict[str, Any]) -> str:
    provider = str(value.get("provider_id") or "unknown")[:200]
    model = str(value.get("model_id") or "unknown")[:500]
    return f"{provider}/{model}"


def _empty_tokens() -> dict[str, int]:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }


def _tokens(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return _empty_tokens()
    cache = value.get("cache") if isinstance(value.get("cache"), dict) else {}
    return {
        "input": _nonnegative_int(value.get("input")),
        "output": _nonnegative_int(value.get("output")),
        "reasoning": _nonnegative_int(value.get("reasoning")),
        "cache_read": _nonnegative_int(value.get("cache_read", cache.get("read"))),
        "cache_write": _nonnegative_int(value.get("cache_write", cache.get("write"))),
    }


def _add_tokens(target: dict[str, int], source: dict[str, int]) -> None:
    for key in target:
        target[key] += source[key]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and 0 <= result <= 1_000_000_000 else None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and 0 <= result <= 100_000_000_000 else None


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(float(value)) or value < 0:
        return 0
    return int(value)


def _read_child_state(path: Path) -> dict[str, Any] | None:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
