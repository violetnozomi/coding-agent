"""Structured JSONL tracing for agent runs."""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from typing import Any

from nz_coder.foundation import config
from nz_coder.protocol.message_schema import (
    project_public_protocol_value,
    project_public_tool_part,
)
from nz_coder.protocol.public_error import public_error_from_wire, redact_failed_event_output, to_public_error
from nz_coder.state.workdir import current_derived_path
from nz_coder.state.sessions import active_session_id, session_trace_dir

MAX_FIELD_CHARS = 4000
MAX_CONTAINER_ITEMS = 200
MAX_NESTING_DEPTH = 16
_DEFAULT_TRACE_DIR = Path(config.TRACE_DIR)


def trace_dir(trace_dir: Path = None) -> Path:
    if trace_dir is not None:
        return Path(trace_dir)
    configured = Path(getattr(config, "TRACE_DIR", _DEFAULT_TRACE_DIR))
    if configured != _DEFAULT_TRACE_DIR:
        return configured
    return current_derived_path("TRACE_DIR")


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
        self._lock = threading.Lock()
        self.dropped_events = 0
        self.last_write_error: str | None = None
        if self.enabled:
            try:
                self.trace_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.enabled = False
                self.dropped_events += 1
                self.last_write_error = str(exc)

    def __getstate__(self) -> dict[str, Any]:
        """Serialize durable trace identity without the process-local lock."""
        state = dict(self.__dict__)
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Recreate synchronization after crossing a spawn boundary."""
        self.__dict__.update(state)
        self._lock = threading.Lock()

    def log(self, event: str, **payload: Any) -> None:
        if not self.enabled:
            return
        try:
            projected = _project_trace_diagnostics(payload)
            redact_failed_event_output(projected)
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
                **_sanitize(projected),
            }
            encoded = json.dumps(
                row,
                ensure_ascii=False,
                default=str,
                allow_nan=False,
            ) + "\n"
            with self._lock:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(encoded)
        except Exception as exc:
            with self._lock:
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
        else:
            pattern = "*.jsonl"
        dated_traces: list[tuple[float, str, Path]] = []
        for path in base.glob(pattern):
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                # Rotation/cleanup may remove a candidate between glob() and
                # stat().  A stale candidate must not hide a valid trace.
                continue
            dated_traces.append((modified_at, path.name, path))
        if dated_traces:
            dated_traces.sort(reverse=True)
            return dated_traces[0][2]
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def summarize_trace(path: Path, max_events: int = 40) -> str:
    if not path or not path.exists():
        return "No trace found."

    rows = _read_trace_rows(path)
    lines = [f"Trace: {path}", "", "=== Summary ==="]
    lines.extend(_trace_summary_lines(rows))
    lines.extend(("", "=== Recent events ==="))

    # 上限只影响最近事件。零要单独处理，因为 rows[-0:] 会取出所有记录。
    event_limit = max(0, int(max_events))
    if event_limit:
        lines.extend(_format_trace_event(row) for row in rows[-event_limit:])
    return "\n".join(lines)


def _read_trace_rows(path: Path) -> list[dict]:
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # 运行中的文件可能以半行结尾，已写完整的事件仍可用于摘要。
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _trace_summary_lines(rows: list[dict]) -> list[str]:
    first = rows[0] if rows else {}
    lines = [
        f"  Session          : {first.get('session_id', '-')}",
        f"  Agent            : {first.get('agent_id', '-')}",
        f"  Agent type       : {first.get('agent_type', '-')}",
        f"  Trace id         : {first.get('trace_id', '-')}",
    ]
    if first.get("parent_agent_id"):
        lines.append(f"  Parent agent     : {first['parent_agent_id']}")

    tool_calls = [row for row in rows if row.get("event") == "tool_call"]
    total_tools = len(tool_calls)
    success_count = sum(1 for row in tool_calls if row.get("status") == "ok")
    lines.append(
        f"  Total tool calls : {total_tools}  "
        f"(ok={success_count}, fail/error={total_tools - success_count})"
    )

    llm_samples = _llm_samples(rows)
    if llm_samples:
        lines.extend(_model_summary_lines(llm_samples))

    tool_durations = [_safe_float(row.get("duration_ms", 0.0)) for row in tool_calls]
    if tool_durations:
        lines.append(f"  Tool duration    : {_format_durations(tool_durations)}")
    batches = [row for row in rows if row.get("event") == "tool_batch_completed"]
    if batches:
        peak = max(_safe_int(row.get("peak_concurrency", 0)) for row in batches)
        barrier_wait_ms = sum(_safe_float(row.get("barrier_wait_ms", 0.0)) for row in batches)
        resets = sum(1 for row in rows if row.get("event") == "doom_loop_streak_reset")
        lines.append(
            f"  Tool scheduling  : batches={len(batches)} peak={peak} "
            f"barrier_wait={barrier_wait_ms:.1f}ms streak_resets={resets}"
        )

    child_wait_ms = _child_wait_ms(rows)
    if child_wait_ms:
        lines.append(f"  Child agent wait : total={child_wait_ms:.1f}ms")
    elapsed_seconds, elapsed = _trace_elapsed(rows)
    if elapsed_seconds and llm_samples:
        # 剩余时间包含子代理等待。并行调用的累计耗时可能超过实际经过的时间。
        known_ms = sum(tool_durations) + sum(sample["duration_ms"] for sample in llm_samples)
        overhead_ms = max(0.0, elapsed_seconds * 1000 - known_ms)
        lines.append(f"  Other/child wait : {overhead_ms:.1f}ms")
    lines.append(f"  Elapsed          : {elapsed}")

    if tool_calls:
        counts = Counter(row.get("name", "?") for row in tool_calls)
        top_tools = ", ".join(f"{name}×{count}" for name, count in counts.most_common(3))
        lines.append(f"  Top-3 tools      : {top_tools}")
    return lines


def _model_summary_lines(samples: list[dict]) -> list[str]:
    durations = [sample["duration_ms"] for sample in samples]
    lines = [
        f"  Model calls      : {len(samples)}",
        f"  Model wait       : {_format_durations(durations)}",
    ]
    first_token_times = [
        sample["first_token_ms"]
        for sample in samples
        if sample["first_token_ms"] is not None
    ]
    if first_token_times:
        average = sum(first_token_times) / len(first_token_times)
        lines.append(f"  First token      : avg={average:.1f}ms max={max(first_token_times):.1f}ms")
    estimates = [sample["token_estimate"] for sample in samples]
    if any(estimates):
        lines.append(f"  Input estimate   : first={estimates[0]} max={max(estimates)} tokens")
    return lines


def _format_durations(durations: list[float]) -> str:
    total = sum(durations)
    average = total / len(durations)
    return f"total={total:.1f}ms avg={average:.1f}ms max={max(durations):.1f}ms"


def _trace_elapsed(rows: list[dict]) -> tuple[float, str]:
    run_start = next(
        (row.get("ts", 0.0) for row in rows if row.get("event") == "run_start"), None
    )
    run_end = next(
        (row.get("ts", 0.0) for row in reversed(rows) if row.get("event") == "run_end"), None
    )
    # 结束时间缺失或为零时，运行中的耗时暂计到文件最后一条记录。
    last_ts = rows[-1].get("ts", 0.0) if rows else 0.0
    seconds = max(0.0, _safe_float(run_end or last_ts) - _safe_float(run_start))
    if not seconds:
        return seconds, "unknown"
    suffix = "" if run_end else " (running)"
    return seconds, f"{seconds:.1f}s{suffix}"


def _llm_samples(rows: list[dict]) -> list[dict]:
    """请求和响应按写入顺序配对，只有配对成功的调用才进入统计。"""
    pending: deque[dict] = deque()
    samples: list[dict] = []
    for row in rows:
        event = row.get("event")
        if event == "llm_request":
            pending.append(row)
            continue
        if event != "llm_response" or not pending:
            continue
        request = pending.popleft()
        duration = _safe_float(row.get("duration_ms", 0.0))
        if duration <= 0:
            # 早期轨迹没有耗时字段，可由对应请求和响应的时间戳还原。
            requested_at = _safe_float(request.get("ts", 0.0))
            responded_at = _safe_float(row.get("ts", 0.0))
            duration = max(0.0, (responded_at - requested_at) * 1000)
        first_token = row.get("first_token_ms")
        samples.append({
            "duration_ms": duration,
            "first_token_ms": _safe_float(first_token) if first_token is not None else None,
            "token_estimate": _safe_int(request.get("token_estimate", 0)),
            "attempts": _safe_int(row.get("attempts", 1), 1),
        })
    return samples


def _child_wait_ms(rows: list[dict]) -> float:
    """累加父轨迹中已结束的子代理区间，并行子代理各自计时。"""
    started_at: dict[str, float] = {}
    total_seconds = 0.0
    for row in rows:
        event = row.get("event")
        child_id = str(row.get("child_trace_id") or row.get("child_session_id") or "")
        if not child_id:
            continue
        if event == "subagent_spawn":
            started_at[child_id] = _safe_float(row.get("ts", 0.0))
        elif event == "subagent_complete" and child_id in started_at:
            duration = _safe_float(row.get("ts", 0.0)) - started_at.pop(child_id)
            total_seconds += max(0.0, duration)
    return total_seconds * 1000


def _format_trace_event(row: dict) -> str:
    event = row.get("event", "?")
    if event == "tool_call":
        return (
            f"- tool {row.get('name')} -> {row.get('status')} "
            f"({_safe_float(row.get('duration_ms', 0.0)):.1f}ms, {row.get('output_len', 0)} chars)"
        )
    if event == "tool_batch_completed":
        return (
            f"- tool batch {row.get('batch_id')} -> peak={row.get('peak_concurrency', 0)} "
            f"wall={_safe_float(row.get('wall_ms', 0.0)):.1f}ms "
            f"barrier_wait={_safe_float(row.get('barrier_wait_ms', 0.0)):.1f}ms"
        )
    if event == "doom_loop_streak_reset":
        return (
            f"- tool streak reset: {row.get('reason')} "
            f"after {row.get('previous_tool')}×{row.get('previous_count', 0)}"
        )
    if event == "llm_response":
        duration = _safe_float(row.get("duration_ms", 0.0))
        timing = f" duration={duration:.1f}ms" if duration else ""
        first_token = row.get("first_token_ms")
        if first_token is not None:
            timing += f" ttft={_safe_float(first_token):.1f}ms"
        return (
            f"- llm response: tools={row.get('tool_calls', 0)} "
            f"content={row.get('content_len', 0)} chars{timing}"
        )
    if event == "api_error":
        return f"- api error: {row.get('error')}"
    return f"- {event}"


def _sanitize(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> Any:
    if _depth >= MAX_NESTING_DEPTH:
        return "[maximum trace nesting depth reached]"
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        seen = _seen if _seen is not None else set()
        identity = id(value)
        if identity in seen:
            return "[circular reference]"
        seen.add(identity)
        try:
            if isinstance(value, dict):
                items = list(value.items())
                result = {
                    str(key): _sanitize(
                        item,
                        _seen=seen,
                        _depth=_depth + 1,
                    )
                    for key, item in items[:MAX_CONTAINER_ITEMS]
                }
                omitted = len(items) - MAX_CONTAINER_ITEMS
                if omitted > 0:
                    result["__nz_truncated_items__"] = omitted
                return result
            items = list(value)
            result = [
                _sanitize(item, _seen=seen, _depth=_depth + 1)
                for item in items[:MAX_CONTAINER_ITEMS]
            ]
            omitted = len(items) - MAX_CONTAINER_ITEMS
            if omitted > 0:
                result.append(f"(... {omitted} more items)")
            return result
        finally:
            seen.remove(identity)
    if isinstance(value, str):
        if len(value) > MAX_FIELD_CHARS:
            return value[:MAX_FIELD_CHARS] + "... (truncated)"
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _project_trace_diagnostics(
    value: object,
    *,
    field: str = "",
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> object:
    """Keep ordinary trace exports structural rather than diagnostic-bearing."""
    if _depth >= MAX_NESTING_DEPTH:
        return "[maximum trace nesting depth reached]"
    normalized_field = field.casefold().replace("-", "_")
    error_like = bool(
        normalized_field in {"error", "exception", "failure", "diagnostic"}
        or normalized_field.startswith(
            ("error_", "exception_", "failure_", "diagnostic_")
        )
        or normalized_field.endswith(
            ("_error", "_exception", "_failure", "_diagnostic")
        )
    )
    safe_error_scalar = bool(
        isinstance(value, bool)
        or isinstance(value, (int, float))
        or (
            normalized_field.endswith(("_type", "_code", "_kind", "_status", "_phase"))
            and isinstance(value, str)
            and 0 < len(value) <= 120
            and all(character.isalnum() or character in "._-" for character in value)
        )
    )
    if error_like and not safe_error_scalar:
        if value is None:
            return None
        public = public_error_from_wire(value)
        return (public or to_public_error(value)).to_dict()
    if normalized_field in {
        "private_diagnostic",
        "response_body",
        "responsebody",
        "response_headers",
        "responseheaders",
        "headers",
        "authorization",
        "provider_extra",
        "reasoning_content",
        "_nz_provider_extra",
        "_nz_provider_metadata",
        "_nz_provider_reasoning_content",
    }:
        return "[private diagnostic omitted]"
    if isinstance(value, dict):
        if (
            "type" in value
            and (value.get("internal") is True or value.get("visible") is False)
        ):
            return {}
        if value.get("type") == "tool" and isinstance(value.get("state"), dict):
            value = project_public_protocol_value(project_public_tool_part(value))
        seen = _seen if _seen is not None else set()
        identity = id(value)
        if identity in seen:
            return "[circular reference]"
        seen.add(identity)
        try:
            return {
                str(key): _project_trace_diagnostics(
                    item,
                    field=str(key),
                    _seen=seen,
                    _depth=_depth + 1,
                )
                for key, item in value.items()
            }
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple, set, frozenset)):
        seen = _seen if _seen is not None else set()
        identity = id(value)
        if identity in seen:
            return "[circular reference]"
        seen.add(identity)
        try:
            return [
                _project_trace_diagnostics(
                    item,
                    _seen=seen,
                    _depth=_depth + 1,
                )
                for item in value
            ]
        finally:
            seen.remove(identity)
    return value


def _safe_session_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in ("_", "-"))
    return safe or None
