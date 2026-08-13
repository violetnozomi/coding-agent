"""Structured JSONL tracing for agent runs."""
from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from nz_coder import config
from nz_coder.state.workdir import current_workdir
from nz_coder.sessions import active_session_id, session_trace_dir

MAX_FIELD_CHARS = 4000
_DEFAULT_TRACE_DIR = Path(config.TRACE_DIR)


def trace_dir(trace_dir: Path = None) -> Path:
    if trace_dir is not None:
        return Path(trace_dir)
    configured = Path(getattr(config, "TRACE_DIR", _DEFAULT_TRACE_DIR))
    if configured != _DEFAULT_TRACE_DIR:
        return configured
    return current_workdir() / ".nz-coder" / "runs"


class TraceRecorder:
    """Append-only trace recorder for model/tool/debug events."""

    def __init__(
        self,
        run_id: str = None,
        trace_dir: Path = None,
        enabled: bool = True,
        session_id: str = None,
        agent_id: str = None,
        trace_id: str = None,
        parent_agent_id: str = None,
        parent_trace_id: str = None,
        agent_type: str = "main",
    ):
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.enabled = enabled
        self.session_id = _safe_session_id(session_id or active_session_id())
        self.agent_id = agent_id or f"agent-{uuid.uuid4().hex[:8]}"
        self.trace_id = trace_id or self.run_id
        self.parent_agent_id = parent_agent_id
        self.parent_trace_id = parent_trace_id
        self.agent_type = agent_type
        self.trace_dir = trace_dir if trace_dir is not None else globals()["trace_dir"]()
        prefix = f"{self.session_id}__" if self.session_id else ""
        self.path = self.trace_dir / f"{prefix}{self.run_id}.jsonl"
        self.dropped_events = 0
        self.last_write_error: str | None = None
        if self.enabled:
            try:
                self.trace_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.enabled = False
                self.dropped_events += 1
                self.last_write_error = str(exc)

    def log(self, event: str, **payload: Any) -> None:
        if not self.enabled:
            return
        row = {
            "ts": time.time(),
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "parent_trace_id": self.parent_trace_id,
            "agent_type": self.agent_type,
            "event": event,
            **_sanitize(payload),
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            self.dropped_events += 1
            self.last_write_error = str(exc)


def latest_trace(trace_dir: Path = None, session_id: str = None) -> Path | None:
    explicit_dir = Path(trace_dir) if trace_dir is not None else None
    candidates: list[Path] = []
    if explicit_dir is not None:
        candidates.append(explicit_dir)
    elif session_id:
        candidates.append(session_trace_dir(session_id))
        candidates.append(globals()["trace_dir"]())
    else:
        candidates.append(globals()["trace_dir"]())

    for base in candidates:
        if not base.exists():
            continue
        if session_id:
            safe = _safe_session_id(session_id)
            pattern = f"{safe}__*.jsonl"
            traces = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        else:
            traces = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if traces:
            return traces[0]
    return None


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

    tool_rows = [r for r in all_rows if r.get("event") == "tool_call"]
    batch_rows = [r for r in all_rows if r.get("event") == "tool_batch_completed"]
    streak_rows = [r for r in all_rows if r.get("event") == "doom_loop_streak_reset"]
    llm_samples = _llm_samples(all_rows)
    total_tools = len(tool_rows)
    success_count = sum(1 for r in tool_rows if r.get("status") == "ok")
    fail_count = total_tools - success_count
    tool_counter: Counter = Counter(r.get("name", "?") for r in tool_rows)
    top3 = tool_counter.most_common(3)
    durations = [
        float(row.get("duration_ms", 0.0) or 0.0)
        for row in tool_rows
    ]
    total_tool_ms = sum(durations)
    peak_concurrency = max(
        (int(row.get("peak_concurrency", 0) or 0) for row in batch_rows),
        default=0,
    )
    barrier_wait_ms = sum(
        float(row.get("barrier_wait_ms", 0.0) or 0.0)
        for row in batch_rows
    )
    run_start = next((r.get("ts", 0.0) for r in all_rows if r.get("event") == "run_start"), None)
    run_end = next((r.get("ts", 0.0) for r in reversed(all_rows) if r.get("event") == "run_end"), None)
    last_ts = all_rows[-1].get("ts", 0.0) if all_rows else 0.0
    elapsed_seconds = (
        float((run_end or last_ts) - run_start)
        if run_start and (run_end or last_ts) else 0.0
    )
    elapsed = (
        f"{elapsed_seconds:.1f}s"
        + ("" if run_end else " (running)")
        if elapsed_seconds else "unknown"
    )
    child_wait_ms = _child_wait_ms(all_rows)

    first = all_rows[0] if all_rows else {}
    lines = [f"Trace: {path}", ""]
    lines.append("=== Summary ===")
    lines.append(f"  Session          : {first.get('session_id', '-')}")
    lines.append(f"  Agent            : {first.get('agent_id', '-')}")
    lines.append(f"  Agent type       : {first.get('agent_type', '-')}")
    lines.append(f"  Trace id         : {first.get('trace_id', '-')}")
    if first.get("parent_agent_id"):
        lines.append(f"  Parent agent     : {first.get('parent_agent_id')}")
    lines.append(f"  Total tool calls : {total_tools}  (ok={success_count}, fail/error={fail_count})")
    if llm_samples:
        llm_durations = [sample["duration_ms"] for sample in llm_samples]
        lines.append(f"  Model calls      : {len(llm_samples)}")
        lines.append(
            "  Model wait       : "
            f"total={sum(llm_durations):.1f}ms "
            f"avg={sum(llm_durations) / len(llm_durations):.1f}ms "
            f"max={max(llm_durations):.1f}ms"
        )
        ttft = [
            sample["first_token_ms"]
            for sample in llm_samples
            if sample["first_token_ms"] is not None
        ]
        if ttft:
            lines.append(
                "  First token      : "
                f"avg={sum(ttft) / len(ttft):.1f}ms max={max(ttft):.1f}ms"
            )
        estimates = [sample["token_estimate"] for sample in llm_samples]
        if any(estimates):
            lines.append(
                "  Input estimate   : "
                f"first={estimates[0]} max={max(estimates)} tokens"
            )
    if durations:
        lines.append(
            "  Tool duration    : "
            f"total={total_tool_ms:.1f}ms avg={total_tool_ms / len(durations):.1f}ms "
            f"max={max(durations):.1f}ms"
        )
    if batch_rows:
        lines.append(
            f"  Tool scheduling  : batches={len(batch_rows)} peak={peak_concurrency} "
            f"barrier_wait={barrier_wait_ms:.1f}ms streak_resets={len(streak_rows)}"
        )
    if child_wait_ms:
        lines.append(f"  Child agent wait : total={child_wait_ms:.1f}ms")
    if elapsed_seconds and llm_samples:
        known_ms = total_tool_ms + sum(sample["duration_ms"] for sample in llm_samples)
        overhead_ms = max(0.0, elapsed_seconds * 1000 - known_ms)
        lines.append(f"  Other/child wait : {overhead_ms:.1f}ms")
    lines.append(f"  Elapsed          : {elapsed}")
    if top3:
        top3_str = ", ".join(f"{name}×{cnt}" for name, cnt in top3)
        lines.append(f"  Top-3 tools      : {top3_str}")
    lines.append("")
    lines.append("=== Recent events ===")
    for row in all_rows[-max_events:]:
        event = row.get("event", "?")
        if event == "tool_call":
            lines.append(
                f"- tool {row.get('name')} -> {row.get('status')} "
                f"({float(row.get('duration_ms', 0.0) or 0.0):.1f}ms, "
                f"{row.get('output_len', 0)} chars)"
            )
        elif event == "tool_batch_completed":
            lines.append(
                f"- tool batch {row.get('batch_id')} -> peak={row.get('peak_concurrency', 0)} "
                f"wall={float(row.get('wall_ms', 0.0) or 0.0):.1f}ms "
                f"barrier_wait={float(row.get('barrier_wait_ms', 0.0) or 0.0):.1f}ms"
            )
        elif event == "doom_loop_streak_reset":
            lines.append(
                f"- tool streak reset: {row.get('reason')} "
                f"after {row.get('previous_tool')}×{row.get('previous_count', 0)}"
            )
        elif event == "llm_response":
            duration = float(row.get("duration_ms", 0.0) or 0.0)
            ttft = row.get("first_token_ms")
            timing = f" duration={duration:.1f}ms" if duration else ""
            if ttft is not None:
                timing += f" ttft={float(ttft):.1f}ms"
            lines.append(
                f"- llm response: tools={row.get('tool_calls', 0)} "
                f"content={row.get('content_len', 0)} chars{timing}"
            )
        elif event == "api_error":
            lines.append(f"- api error: {row.get('error')}")
        else:
            lines.append(f"- {event}")

    return "\n".join(lines)


def _llm_samples(rows: list[dict]) -> list[dict]:
    """Pair sequential request/response events, including legacy traces."""
    pending: list[dict] = []
    samples: list[dict] = []
    for row in rows:
        event = row.get("event")
        if event == "llm_request":
            pending.append(row)
            continue
        if event != "llm_response" or not pending:
            continue
        request = pending.pop(0)
        duration = float(row.get("duration_ms", 0.0) or 0.0)
        if duration <= 0:
            duration = max(
                0.0,
                (float(row.get("ts", 0.0)) - float(request.get("ts", 0.0))) * 1000,
            )
        first_token = row.get("first_token_ms")
        samples.append({
            "duration_ms": duration,
            "first_token_ms": float(first_token) if first_token is not None else None,
            "token_estimate": int(request.get("token_estimate", 0) or 0),
            "attempts": int(row.get("attempts", 1) or 1),
        })
    return samples


def _child_wait_ms(rows: list[dict]) -> float:
    """Return foreground child spans visible in the parent trace."""
    pending: dict[str, float] = {}
    total = 0.0
    for row in rows:
        event = row.get("event")
        child_id = str(row.get("child_trace_id") or row.get("child_session_id") or "")
        if not child_id:
            continue
        if event == "subagent_spawn":
            pending[child_id] = float(row.get("ts", 0.0) or 0.0)
        elif event == "subagent_complete" and child_id in pending:
            total += max(0.0, float(row.get("ts", 0.0) or 0.0) - pending.pop(child_id))
    return total * 1000


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


def _safe_session_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in ("_", "-"))
    return safe or None
