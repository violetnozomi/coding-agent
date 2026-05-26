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

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    all_rows = []
    for raw in raw_lines:
        try:
            all_rows.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    # IMPROVED: 汇总统计，方便快速了解本次 run 的整体情况。
    tool_rows = [r for r in all_rows if r.get("event") == "tool_call"]
    total_tools = len(tool_rows)
    success_count = sum(1 for r in tool_rows if r.get("status") == "ok")
    fail_count = total_tools - success_count

    # 按工具名统计调用次数，取 top-3
    from collections import Counter
    tool_counter: Counter = Counter(r.get("name", "?") for r in tool_rows)
    top3 = tool_counter.most_common(3)

    # 总耗时：run_start 到 run_end 的 ts 差
    run_start = next((r.get("ts", 0.0) for r in all_rows if r.get("event") == "run_start"), None)
    run_end   = next((r.get("ts", 0.0) for r in reversed(all_rows) if r.get("event") == "run_end"), None)
    elapsed = f"{run_end - run_start:.1f}s" if run_start and run_end else "unknown"

    lines = [f"Trace: {path}", ""]
    # ── 汇总块 ───────────────────────────────────────────────────────────────
    lines.append("=== Summary ===")
    lines.append(f"  Total tool calls : {total_tools}  (ok={success_count}, fail/error={fail_count})")
    lines.append(f"  Elapsed          : {elapsed}")
    if top3:
        top3_str = ", ".join(f"{name}×{cnt}" for name, cnt in top3)
        lines.append(f"  Top-3 tools      : {top3_str}")
    lines.append("")

    # ── 最近事件列表 ──────────────────────────────────────────────────────────
    lines.append("=== Recent events ===")
    for row in all_rows[-max_events:]:
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
