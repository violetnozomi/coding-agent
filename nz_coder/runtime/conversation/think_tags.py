"""Incrementally separate leading think-tag reasoning from visible model text."""
from __future__ import annotations

from dataclasses import dataclass


_TAGS = (
    ("thinking", "<thinking>", "</thinking>"),
    ("think", "<think>", "</think>"),
)
_CLOSE_TAGS = tuple(item[2] for item in _TAGS)


@dataclass(frozen=True)
class ThinkTagEvent:
    """One reasoning/text transition produced by the incremental demuxer."""

    type: str
    text: str = ""
    tag: str = ""


class ThinkTagDemux:
    """State machine matching InfCode's leading think-tag stream contract."""

    def __init__(self) -> None:
        self.mode = "detecting"
        self.buffer = ""
        self.tag = ""
        self.close_tag = ""
        self.skip_leading_text_whitespace = False

    def push(self, text: str) -> list[ThinkTagEvent]:
        if not text:
            return []
        if self.mode == "detecting":
            return self._push_detecting(text)
        if self.mode == "reasoning":
            return self._push_reasoning(text)
        return self._push_text(text)

    def finish(self) -> list[ThinkTagEvent]:
        if self.mode == "detecting":
            text = self.buffer
            self.buffer = ""
            self.mode = "done"
            return [ThinkTagEvent("text-delta", text=text)] if text else []
        if self.mode == "reasoning":
            events = []
            if self.buffer:
                events.append(ThinkTagEvent("reasoning-delta", text=self.buffer))
            self.buffer = ""
            self.mode = "done"
            events.append(ThinkTagEvent("reasoning-end"))
            return events
        if self.mode == "text":
            return self._flush_text()
        self.mode = "done"
        return []

    def _push_detecting(self, text: str) -> list[ThinkTagEvent]:
        raw = self.buffer + text
        candidate = raw.lstrip()
        match = next(
            (item for item in _TAGS if candidate.startswith(item[1])),
            None,
        )
        if match is not None:
            self.buffer = ""
            self.mode = "reasoning"
            self.tag = match[0]
            self.close_tag = match[2]
            return [
                ThinkTagEvent("reasoning-start", tag=self.tag),
                *self._push_reasoning(candidate[len(match[1]):]),
            ]
        if not candidate or any(item[1].startswith(candidate) for item in _TAGS):
            self.buffer = raw
            return []
        self.buffer = ""
        self.mode = "text"
        return self._push_text(raw)

    def _push_reasoning(self, text: str) -> list[ThinkTagEvent]:
        if not self.close_tag:
            return [ThinkTagEvent("reasoning-delta", text=text)] if text else []
        value = self.buffer + text
        close_index = value.find(self.close_tag)
        if close_index >= 0:
            self.buffer = ""
            self.mode = "text"
            self.skip_leading_text_whitespace = True
            events = []
            reasoning = value[:close_index]
            if reasoning:
                events.append(ThinkTagEvent("reasoning-delta", text=reasoning))
            events.append(ThinkTagEvent("reasoning-end"))
            events.extend(self._push_text(value[close_index + len(self.close_tag):]))
            return events
        keep = _longest_close_prefix_suffix(value, self.close_tag)
        emitted = value[:len(value) - len(keep)] if keep else value
        self.buffer = keep
        return [ThinkTagEvent("reasoning-delta", text=emitted)] if emitted else []

    def _push_text(self, text: str) -> list[ThinkTagEvent]:
        raw = self.buffer + text
        value = raw.lstrip() if self.skip_leading_text_whitespace else raw
        if self.skip_leading_text_whitespace:
            self.buffer = ""
            if not value:
                return []
            self.skip_leading_text_whitespace = False
        cursor = 0
        output = ""
        while cursor < len(value):
            close = _next_close_tag(value, cursor)
            if close is None:
                break
            index, tag = close
            output += value[cursor:index]
            cursor = index + len(tag)
        tail = value[cursor:]
        keep = _longest_any_close_prefix_suffix(tail)
        output += tail[:len(tail) - len(keep)] if keep else tail
        self.buffer = keep
        return [ThinkTagEvent("text-delta", text=output)] if output else []

    def _flush_text(self) -> list[ThinkTagEvent]:
        text = self.buffer.lstrip() if self.skip_leading_text_whitespace else self.buffer
        self.buffer = ""
        self.skip_leading_text_whitespace = False
        self.mode = "done"
        return [ThinkTagEvent("text-delta", text=text)] if text else []


def demux_think_tags(text: str) -> tuple[str, str]:
    """Split one complete response into visible text and tagged reasoning."""
    state = ThinkTagDemux()
    events = [*state.push(str(text or "")), *state.finish()]
    visible = "".join(item.text for item in events if item.type == "text-delta")
    reasoning = "".join(
        item.text for item in events if item.type == "reasoning-delta"
    )
    return visible, reasoning


def _longest_close_prefix_suffix(value: str, close_tag: str) -> str:
    maximum = min(len(value), len(close_tag) - 1)
    for length in range(maximum, 0, -1):
        suffix = value[-length:]
        if close_tag.startswith(suffix):
            return suffix
    return ""


def _longest_any_close_prefix_suffix(value: str) -> str:
    best = ""
    for close_tag in _CLOSE_TAGS:
        suffix = _longest_close_prefix_suffix(value, close_tag)
        if len(suffix) > len(best):
            best = suffix
    return best


def _next_close_tag(value: str, start: int) -> tuple[int, str] | None:
    best = None
    for tag in _CLOSE_TAGS:
        index = value.find(tag, start)
        if index < 0:
            continue
        if best is None or index < best[0]:
            best = (index, tag)
    return best


__all__ = ["ThinkTagDemux", "ThinkTagEvent", "demux_think_tags"]
