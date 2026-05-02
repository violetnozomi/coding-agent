"""Structured JSONL tracing for agent runs."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from nz_coder import config

MAX_FIELD_CHARS = 4000


class TraceRecorder:
    """Append-only trace recorder for model/tool/debug events."""

    def __init__(self, run_id: str = None, trace_dir: Path = None, enabled: bool = True):
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.enabled = enabled
        self.trace_dir = trace_dir or config.TRACE_DIR
        self.path = self.trace_dir / f"{self.run_id}.jsonl"
        if self.enabled:
            self.trace_dir.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **payload: Any) -> None:
        if not self.enabled:
            return
        row = {
            "ts": time.time(),
            "run_id": self.run_id,
            "event": event,
            **_sanitize(payload),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def latest_trace(trace_dir: Path = None) -> Path | None:
    base = trace_dir or config.TRACE_DIR
    if not base.exists():
        return None
    traces = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return traces[0] if traces else None


def summarize_trace(path: Path, max_events: int = 40) -> str:
    if not path or not path.exists():
        return "No trace found."
    lines = [f"Trace: {path}", ""]
    events = path.read_text(encoding="utf-8").splitlines()
    for raw in events[-max_events:]:
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event = row.get("event", "?")
        if event == "tool_call":
            lines.append(f"- tool {row.get('name')} -> {row.get('status')} ({row.get('output_len', 0)} chars)")
        elif event == "llm_response":
            lines.append(f"- llm response: tools={row.get('tool_calls', 0)} content={row.get('content_len', 0)} chars")
        elif event == "api_error":
            lines.append(f"- api error: {row.get('error')}")
        else:
            lines.append(f"- {event}")
    return "\n".join(lines)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, str):
        if len(value) > MAX_FIELD_CHARS:
            return value[:MAX_FIELD_CHARS] + "... (truncated)"
        return value
    return value
