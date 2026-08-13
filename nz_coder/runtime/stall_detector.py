"""InfCodeX-compatible bounded-window L1 tool stall detector."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StallSignal:
    """One immutable detector verdict and its sidecar input envelope."""

    kind: str
    tool_name: str = ""
    input_json: str = ""
    occurrence_count: int = 0
    cache_hit_count: int = 0
    turns: tuple[int, ...] = ()
    envelope: str = ""


@dataclass(frozen=True)
class _RecordedEvent:
    tool_name: str
    input_json: str
    cache_hit: bool
    turn: int


def stable_stringify(value: Any) -> str:
    """Return compact, recursively key-sorted JSON used as call identity."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return json.dumps("__non_serializable__")


def build_stall_signal_envelope(
    *,
    tool_name: str,
    input_json: str,
    occurrence_count: int,
    cache_hit_count: int,
    turns: tuple[int, ...],
) -> str:
    """Build the byte-stable FEATURE_178 sidecar envelope."""
    rendered_turns = ",".join(str(turn) for turn in turns)
    return (
        "[Stall detector signal]\n"
        f"tool={tool_name} input={input_json} "
        f"occurrence_count={occurrence_count} "
        f"cache_hit_count={cache_hit_count} turns=[{rendered_turns}]"
    )


class StallDetector:
    """Track identical calls across a bounded per-run event window."""

    def __init__(self, window_size: int = 20, disabled: bool | None = None):
        self._disabled = (
            os.environ.get("KODAX_STALL_DETECT") == "0"
            if disabled is None
            else bool(disabled)
        )
        self._window_size = max(1, int(window_size))
        self._events: list[_RecordedEvent] = []
        self._next_turn = 1

    def record_tool_use(
        self,
        tool_name: str,
        tool_input: Any,
        cache_hit: bool = False,
    ) -> StallSignal:
        """Record a call and return the translated InfCodeX L1 verdict."""
        if self._disabled:
            return StallSignal(kind="no_stall")
        input_json = stable_stringify(tool_input)
        event = _RecordedEvent(
            tool_name=str(tool_name),
            input_json=input_json,
            cache_hit=bool(cache_hit),
            turn=self._next_turn,
        )
        self._next_turn += 1
        self._events.append(event)
        if len(self._events) > self._window_size:
            del self._events[:len(self._events) - self._window_size]
        matches = tuple(
            item for item in self._events
            if item.tool_name == event.tool_name and item.input_json == input_json
        )
        cache_hits = sum(1 for item in matches if item.cache_hit)
        if len(matches) < 3 and not (len(matches) >= 2 and cache_hits >= 1):
            return StallSignal(kind="no_stall")
        turns = tuple(item.turn for item in matches)
        envelope = build_stall_signal_envelope(
            tool_name=event.tool_name,
            input_json=input_json,
            occurrence_count=len(matches),
            cache_hit_count=cache_hits,
            turns=turns,
        )
        return StallSignal(
            kind="stall",
            tool_name=event.tool_name,
            input_json=input_json,
            occurrence_count=len(matches),
            cache_hit_count=cache_hits,
            turns=turns,
            envelope=envelope,
        )

    def reset(self) -> None:
        """Drop task-local history at run or compaction boundaries."""
        if self._disabled:
            return
        self._events.clear()
        self._next_turn = 1

    def size(self) -> int:
        """Return current ring-buffer occupancy for diagnostics."""
        return len(self._events)
