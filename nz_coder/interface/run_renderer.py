"""Structured terminal projection of the existing Session run/tool events."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import queue
import re
import time

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from nz_coder.interface.presentation_tokens import clip_terminal_text
from nz_coder.protocol.session_events import (
    EventSubscriptionGapError,
    SessionEvent,
    SessionSubscription,
)


_EVENT_TYPES = {
    "session.run.started",
    "session.run.completed",
    "session.run.failed",
    "session.run.cancelled",
    "session.tool.started",
    "session.tool.completed",
    "message.updated",
    "message.part.updated",
    "message.part.delta",
    "process.started",
    "process.exited",
    "process.killed",
    "process.failed",
}
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class TerminalRunRenderer:
    """Render compact cards while treating SessionEventBus as authoritative."""

    def __init__(self, console, streaming_renderer, detail_provider=None) -> None:  # noqa: ANN001
        self.console = console
        self.streaming_renderer = streaming_renderer
        self.detail_provider = detail_provider or (lambda: "compact")
        self._subscription: SessionSubscription | None = None
        self._started_at = 0.0
        self._tool_count = 0
        self._terminal_seen = False
        self._rendered_tool_ids: set[str] = set()
        self._rendered_error_ids: set[str] = set()
        self._started_tools: dict[str, dict] = {}
        self._running_parts: dict[str, dict] = {}
        self._retry_part: dict | None = None
        self._assistant_messages: dict[str, dict] = {}
        self._pending_errors: dict[str, dict] = {}
        self._pending_completed: dict[str, dict] = {}
        self._pending_terminal: tuple[str, str] | None = None
        self._agent = None
        self._seen_event_ids: set[str] = set()
        self._seen_event_order: deque[str] = deque()

    def begin(self, agent) -> None:
        """Subscribe before a run so no lifecycle event is missed."""
        self.close()
        self._started_at = time.monotonic()
        self._tool_count = 0
        self._terminal_seen = False
        self._rendered_tool_ids.clear()
        self._rendered_error_ids.clear()
        self._started_tools.clear()
        self._running_parts.clear()
        self._retry_part = None
        self._assistant_messages.clear()
        self._pending_errors.clear()
        self._pending_completed.clear()
        self._pending_terminal = None
        self._seen_event_ids.clear()
        self._seen_event_order.clear()
        self._agent = agent
        event_bus = getattr(agent, "event_bus", None)
        if event_bus is not None:
            self._subscription = event_bus.subscribe(_EVENT_TYPES, max_queue=512)
        self._refresh_status()
        if not callable(getattr(self.streaming_renderer, "set_status", None)):
            model = str(getattr(agent, "model_id", "model"))
            self.console.print(f"[info]● Working[/info] [dim]with {model}[/dim]")

    def begin_remote(self, agent) -> None:
        """Start the same renderer state machine for an external event source."""
        self.begin(type("RemoteAgent", (), {
            "event_bus": None,
            "model_id": getattr(agent, "model_id", "model"),
        })())

    def rebase_remote(self) -> None:
        """Reset transient reducer state at an authoritative remote snapshot.

        Event identities and already-rendered cards are retained so events shared
        by the old stream and snapshot replay cannot be applied twice.
        """
        self._started_tools.clear()
        self._running_parts.clear()
        self._retry_part = None
        self._assistant_messages.clear()
        self._pending_errors.clear()
        self._pending_completed.clear()
        self._pending_terminal = None
        self._terminal_seen = False
        self._refresh_status()

    def feed(self, event: SessionEvent | dict) -> None:
        """Project one event supplied by a remote backend.

        Remote SSE uses the same ``SessionEvent`` wire envelope as the local
        bus. Text deltas are forwarded to the existing streaming renderer;
        tool and lifecycle events use the exact local reducer below.
        """
        if isinstance(event, dict):
            event = SessionEvent.from_dict(event)
        if event is None:
            return
        if event.event_id:
            if event.event_id in self._seen_event_ids:
                return
            self._seen_event_ids.add(event.event_id)
            self._seen_event_order.append(event.event_id)
            while len(self._seen_event_order) > 4096:
                self._seen_event_ids.discard(self._seen_event_order.popleft())
        if event.type == "message.part.delta":
            delta = event.properties.get("delta")
            if isinstance(delta, str) and delta:
                on_token = getattr(self.streaming_renderer, "on_token", None)
                if callable(on_token):
                    on_token(delta)
            return
        self._handle_event(event)
        self._flush_completed()

    async def watch(self, stop: asyncio.Event, interval: float = 0.08) -> None:
        """Continuously project running Part updates while the Agent is busy."""
        delay = max(0.04, min(float(interval), 0.5))
        while not stop.is_set():
            self.drain(render_completed=False)
            self._refresh_status()
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue
        self.drain(render_completed=False)
        self._refresh_status()

    def on_tool(self, name: str, output: str) -> None:
        """Drain the completed event emitted for this legacy callback."""
        before = self._tool_count
        self._with_stream_paused(self.drain)
        if self._tool_count == before:
            self._with_stream_paused(
                lambda: self._render_tool({
                    "tool_call_id": f"fallback-{before}-{name}",
                    "name": name,
                    "status": _status_from_output(output),
                    "output": output,
                    "duration_ms": 0.0,
                    "category": "tool",
                    "summary": name,
                })
            )

    def finish(self, result: dict | None = None) -> None:
        self._with_stream_paused(self.drain)
        if not self._terminal_seen:
            status = str((result or {}).get("status") or "completed")
            self._render_run_end(status)
        self.close()

    def cancel(self) -> None:
        self._with_stream_paused(self.drain)
        if not self._terminal_seen:
            self._render_run_end("cancelled")
        self.close()

    def fail(self, exc: Exception) -> None:
        self._with_stream_paused(self.drain)
        if not self._terminal_seen:
            self._render_run_end("failed", str(exc))
        self.close()

    def drain(self, *, render_completed: bool = True) -> None:
        subscription = self._subscription
        if subscription is None:
            return
        while True:
            try:
                event = subscription.get(timeout=0)
            except queue.Empty:
                break
            except StopIteration:
                break
            except EventSubscriptionGapError as exc:
                self.console.print(
                    f"[error]Terminal event view lost {exc.dropped_events} update(s); "
                    "use /status or /trace to resync.[/error]"
                )
                break
            self._handle_event(event)
        if render_completed:
            self._flush_completed()

    def close(self) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            subscription.close()
        setter = getattr(self.streaming_renderer, "set_status", None)
        if callable(setter):
            setter(None)

    def _handle_event(self, event: SessionEvent) -> None:
        properties = event.properties
        if event.type in {"process.started", "process.exited", "process.killed", "process.failed"}:
            self._render_process_event(event.type, properties)
            return
        if event.type == "message.updated":
            info = properties.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                return
            message_id = str(info.get("id") or properties.get("message_id") or "")
            if not message_id:
                return
            self._assistant_messages[message_id] = info
            error = info.get("error")
            if (
                isinstance(error, dict)
                and error.get("name") != "MessageAbortedError"
                and message_id not in self._rendered_error_ids
            ):
                self._pending_errors[message_id] = error
            else:
                self._pending_errors.pop(message_id, None)
            return
        if event.type == "session.tool.started":
            self._retry_part = None
            call_id = str(properties.get("tool_call_id") or f"index-{properties.get('index')}")
            self._started_tools[call_id] = properties
            self._running_parts.setdefault(call_id, {
                "type": "tool",
                "tool": properties.get("name") or "tool",
                "call_id": call_id,
                "state": {
                    "status": "running",
                    "title": properties.get("summary") or properties.get("name") or "tool",
                    "time": {"start": time.time()},
                },
            })
            self._refresh_status()
            return
        if event.type == "message.part.updated":
            part = properties.get("part")
            if not isinstance(part, dict):
                return
            if part.get("type") == "retry":
                self._retry_part = part
                self._refresh_status()
                return
            # Any later assistant progress retires the transient retry notice;
            # the durable RetryPart remains in Session history for inspection.
            self._retry_part = None
            if part.get("type") != "tool":
                self._refresh_status()
                return
            call_id = str(part.get("call_id") or f"index-{part.get('index')}")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if state.get("status") in {"pending", "running"}:
                self._running_parts[call_id] = part
            else:
                self._running_parts.pop(call_id, None)
            self._refresh_status()
            return
        if event.type == "session.tool.completed":
            call_id = str(properties.get("tool_call_id") or f"index-{properties.get('index')}")
            if call_id in self._rendered_tool_ids:
                return
            merged = {**self._started_tools.pop(call_id, {}), **properties}
            self._running_parts.pop(call_id, None)
            self._pending_completed[call_id] = merged
            self._refresh_status()
            return
        if event.type == "session.run.completed":
            self._retry_part = None
            self._pending_terminal = (str(properties.get("status") or "completed"), "")
            return
        if event.type == "session.run.failed":
            self._retry_part = None
            self._pending_terminal = ("failed", str(properties.get("error") or ""))
            return
        if event.type == "session.run.cancelled":
            self._retry_part = None
            self._pending_terminal = ("cancelled", "")

    def _flush_completed(self) -> None:
        pending, self._pending_completed = self._pending_completed, {}
        for call_id, properties in pending.items():
            if call_id in self._rendered_tool_ids:
                continue
            self._render_tool(properties)
            self._rendered_tool_ids.add(call_id)
        errors, self._pending_errors = self._pending_errors, {}
        for message_id, error in errors.items():
            if message_id in self._rendered_error_ids:
                continue
            self._render_assistant_error(error)
            self._rendered_error_ids.add(message_id)
        terminal, self._pending_terminal = self._pending_terminal, None
        if terminal is not None:
            self._render_run_end(*terminal)

    def _refresh_status(self) -> None:
        setter = getattr(self.streaming_renderer, "set_status", None)
        if not callable(setter):
            return
        elapsed = max(0.0, time.monotonic() - self._started_at)
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(elapsed * 10) % 10]
        if self._retry_part is not None:
            raw_attempt = self._retry_part.get("attempt")
            attempt = max(1, raw_attempt) if isinstance(raw_attempt, int) else 1
            next_at = self._retry_part.get("next")
            remaining = (
                max(0.0, float(next_at) - time.time())
                if isinstance(next_at, (int, float))
                else 0.0
            )
            message = _sanitize(
                str(self._retry_part.get("message") or "Provider request failed"),
                160,
            )
            timing = f"in {remaining:.1f}s" if remaining > 0 else "now"
            setter((f"{spinner} Retry {attempt} {timing} · {message}",))
            return
        if not self._running_parts:
            model = str(getattr(self._agent, "model_id", "model"))
            setter((f"{spinner} Waiting for {model} · {elapsed:.1f}s",))
            return
        rows: list[str] = []
        from nz_coder.interface.presentation_tokens import activity_label

        for part in list(self._running_parts.values())[:3]:
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            name = _sanitize(str(part.get("tool") or "tool"), 60)
            title = _sanitize(str(state.get("title") or name), 120)
            started = state.get("time", {}).get("start") if isinstance(state.get("time"), dict) else None
            duration = max(0.0, time.time() - float(started)) if isinstance(started, (int, float)) else elapsed
            rows.append(
                f"{spinner} {activity_label(name, title)} · {name} · {title} · {duration:.1f}s"
            )
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            output = str(metadata.get("output") or "").strip()
            if output:
                preview = _sanitize(output.splitlines()[-1], 160)
                if preview:
                    rows.append(f"  {preview}")
            if name == "task":
                child_status = _sanitize(str(metadata.get("child_status") or "starting"), 60)
                child_tool = _sanitize(str(metadata.get("child_current_tool") or ""), 80)
                child_title = _sanitize(str(metadata.get("child_current_title") or ""), 120)
                child_count = metadata.get("child_tool_count")
                if child_tool:
                    detail = f" {child_title}" if child_title and child_title != child_tool else ""
                    rows.append(f"  ↳ {child_tool}{detail}")
                elif isinstance(child_count, int) and child_count > 0:
                    rows.append(f"  ↳ {child_count} tool call(s) · {child_status}")
                else:
                    rows.append(f"  ↳ {child_status}")
        if len(self._running_parts) > 3:
            rows.append(f"  +{len(self._running_parts) - 3} more running tool(s)")
        status_width = max(20, int(getattr(self.console, "width", 100) or 100) - 4)
        setter(tuple(clip_terminal_text(row, status_width) for row in rows))

    def _render_tool(self, properties: dict) -> None:
        self._tool_count += 1
        try:
            detail_mode = str(self.detail_provider())
        except Exception:
            detail_mode = "compact"
        if detail_mode == "hidden":
            return
        status = str(properties.get("status") or "ok")
        category = str(properties.get("category") or "tool")
        name = str(properties.get("name") or "tool")
        duration = float(properties.get("duration_ms") or 0.0)
        summary = _sanitize(str(properties.get("summary") or name), 240)
        raw_output = str(properties.get("output") or "")
        if name == "process":
            process_card = _process_card(raw_output, properties)
            if process_card is not None:
                self.console.print(process_card)
                return
        specialized = _specialized_tool_line(
            name=name,
            category=category,
            summary=summary,
            status=status,
            duration=duration,
        )
        if (
            specialized is not None
            and detail_mode == "compact"
            and status == "ok"
            and not (name == "bash" and raw_output.strip())
        ):
            self.console.print(specialized, soft_wrap=False, overflow="ellipsis")
            return
        # InfCode keeps read/search/generic tools inline, but renders a Bash
        # command with captured output as a block.
        compact_inline = not (name == "bash" and raw_output.strip())
        if detail_mode == "compact" and status == "ok" and compact_inline:
            line = Text(no_wrap=True, overflow="ellipsis")
            icon, border = _status_style(status)
            label = _tool_label(name, category) or category.title()
            line.append(f"{icon} {label} · {name}", style=f"bold {border}")
            if duration:
                line.append(f"  {duration:.0f} ms", style="dim")
            if summary and summary != name:
                line.append(f"  {summary}", style="dim")
            self.console.print(line, soft_wrap=False, overflow="ellipsis")
            return
        output = (
            _sanitize(raw_output, 4_000)
            if detail_mode in {"full", "detailed"}
            else _output_preview(raw_output, status=status, category=category)
        )
        icon, border = _status_style(status)
        heading = Text()
        label = _tool_label(name, category) or category.title()
        heading.append(f"{icon} {label} · {name}", style=f"bold {border}")
        if duration:
            heading.append(f"  {duration:.0f} ms", style="dim")
        body = [heading]
        if summary and summary != name:
            body.append(Text(summary, style="cyan"))
        if output:
            body.append(Text(output, style="dim" if status == "ok" else ""))
        self.console.print(
            Panel(
                Group(*body),
                border_style=border,
                padding=(0, 1),
                expand=False,
                width=_panel_width(self.console),
            )
        )

    def _render_run_end(self, status: str, detail: str = "") -> None:
        if self._terminal_seen:
            return
        self._terminal_seen = True
        printer = getattr(self.console, "print_terminal", None)
        if not callable(printer):
            printer = self.console.print
        elapsed = max(0.0, time.monotonic() - self._started_at)
        normalized = status.lower()
        if normalized in {"completed", "completed_unverified"}:
            icon, style = "✓", "success"
        elif normalized == "cancelled":
            icon, style = "■", "info"
        else:
            icon, style = "✗", "error"
        suffix = f" · {self._tool_count} tool(s) · {elapsed:.1f}s"
        line = f"{icon} Run {normalized.replace('_', ' ')}{suffix}"
        printer(f"[{style}]{line}[/{style}]", highlight=False)
        footer = self._assistant_footer()
        if footer:
            printer(Text(footer, style="dim"))
        changed = self._changed_paths()
        if changed:
            preview = ", ".join(changed[:5])
            suffix = f", +{len(changed) - 5} more" if len(changed) > 5 else ""
            printer(
                f"[info]Δ {len(changed)} changed file(s):[/info] {preview}{suffix}",
                highlight=False,
            )
        if detail:
            printer(Text(_sanitize(detail, 600), style="red"))

    def _render_process_event(self, event_type: str, properties: dict) -> None:
        process = properties.get("process") if isinstance(properties.get("process"), dict) else {}
        process_id = str(process.get("process_id") or properties.get("process_id") or "process")
        command = str(process.get("command") or properties.get("command") or "").strip()
        status = str(
            process.get("status")
            or properties.get("status")
            or event_type.split(".", 1)[1]
        )
        exit_code = process.get("exit_code", properties.get("exit_code"))
        body = [Text(f"Process · {process_id}", style="bold cyan")]
        if command:
            body.append(Text(_sanitize(command, 200)))
        label = status.upper() + (f" {exit_code}" if exit_code is not None else "")
        body.append(Text(label, style="green" if status == "running" else "dim"))
        self.console.print(Panel(
            Group(*body),
            border_style="cyan" if status == "running" else "green",
            padding=(0, 1),
            expand=False,
        ))

    def _render_assistant_error(self, error: dict) -> None:
        """Render the durable Assistant error once, like InfCode's error block."""
        name = _sanitize(str(error.get("name") or "UnknownError"), 100)
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        message = _safe_error_message(
            str(data.get("message") or _error_fallback(name)), 2_000
        )
        status = data.get("statusCode")
        title = _error_category(name, status)
        if isinstance(status, int) and not isinstance(status, bool):
            title = f"{title} · HTTP {status}"
        body = [Text(message or "The request failed.")]
        action = _error_action(name)
        if action:
            body.append(Text(action, style="bold cyan"))
        self.console.print(Panel(
            Group(*body),
            title=title,
            title_align="left",
            border_style="red",
            padding=(0, 1),
            expand=False,
            width=_panel_width(self.console),
        ))

    def _assistant_footer(self) -> str:
        """Return metadata for the latest terminal Assistant message."""
        for info in reversed(tuple(self._assistant_messages.values())):
            end_state = info.get("end_state")
            if not isinstance(end_state, dict):
                continue
            values = []
            agent = str(info.get("agent") or info.get("mode") or "").strip()
            model = str(info.get("model_id") or "").strip()
            if agent:
                values.append(agent.title())
            if model:
                values.append(model)
            timing = info.get("time") if isinstance(info.get("time"), dict) else {}
            created, completed = timing.get("created"), timing.get("completed")
            if isinstance(created, (int, float)) and isinstance(completed, (int, float)):
                values.append(f"{max(0.0, float(completed) - float(created)):.1f}s")
            reason = str(end_state.get("reason") or "")
            if reason and reason != "completed":
                values.append(reason)
            return " ▣ " + " · ".join(values) if values else ""
        return ""

    def _with_stream_paused(self, callback) -> None:  # noqa: ANN001
        self.streaming_renderer.pause()
        try:
            callback()
        finally:
            self.streaming_renderer.resume()

    def _changed_paths(self) -> list[str]:
        tracker = getattr(self._agent, "change_tracker", None)
        getter = getattr(tracker, "current_changed_paths", None)
        if not callable(getter):
            return []
        try:
            return [str(path) for path in getter()]
        except (OSError, RuntimeError, ValueError):
            return []


def render_permission_request(console, summary: str) -> None:  # noqa: ANN001
    """Render one permission card while preserving simple-console compatibility."""
    if console is None:
        return
    if isinstance(console, Console):
        console.print(
            Panel(
                Text(_sanitize(summary, 500)),
                title="Permission required",
                border_style="yellow",
                padding=(0, 1),
                expand=False,
            )
        )
        return
    console.print(f"\n  [Permission] {summary}", markup=False, highlight=False)


def render_question_request(console, question: dict) -> None:  # noqa: ANN001
    """Render one structured question card in a real Rich terminal."""
    if not isinstance(console, Console):
        console.print(
            f"\n[{question['header']}] {question['question']}",
            markup=False,
            highlight=False,
        )
        for index, option in enumerate(question["options"], 1):
            console.print(
                f"  {index}. {option['label']} — {option['description']}",
                markup=False,
                highlight=False,
            )
        return
    lines = Text(_sanitize(str(question["question"]), 500), style="bold")
    for index, option in enumerate(question["options"], 1):
        lines.append(f"\n{index}. ", style="cyan bold")
        lines.append(_sanitize(str(option["label"]), 160))
        description = _sanitize(str(option.get("description") or ""), 240)
        if description:
            lines.append(f" — {description}", style="dim")
    console.print(
        Panel(
            lines,
            title=_sanitize(str(question["header"]), 80),
            border_style="cyan",
            padding=(0, 1),
            expand=False,
        )
    )


def _status_from_output(output: str) -> str:
    if output.startswith(("Error:", "Denied")):
        return "error"
    if output.startswith("Command exited with code"):
        return "nonzero"
    return "ok"


def _process_card(output: str, properties: dict) -> Panel | None:
    """Render ProcessService results without dumping the wire JSON."""
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    operation = str(payload.get("operation") or "status")
    process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
    process_id = str(process.get("process_id") or payload.get("process_id") or "process")
    status = str(process.get("status") or payload.get("status") or properties.get("status") or "unknown")
    command = str(process.get("command") or "").strip()
    exit_code = process.get("exit_code", payload.get("exit_code"))
    heading = Text(f"Process · {process_id}", style="bold cyan")
    body = [heading]
    if command:
        body.append(Text(_sanitize(command, 200)))
    state = status.upper()
    if exit_code is not None:
        state += f" {exit_code}"
    body.append(Text(f"{operation.upper()} · {state}", style="green" if status == "running" else "dim"))
    if operation == "read" and payload.get("output"):
        body.append(Text(_output_preview(str(payload["output"]), status="ok", category="command"), style="dim"))
    return Panel(
        Group(*body),
        border_style="cyan" if status == "running" else "green",
        padding=(0, 1),
        expand=False,
    )


def _specialized_tool_line(
    *,
    name: str,
    category: str,
    summary: str,
    status: str,
    duration: float,
) -> Text | None:
    """Project common coding tools into product language instead of raw names."""
    label = _tool_label(name, category)
    if label is None:
        return None
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(f"{label} · {name}", style="bold green")
    if duration:
        line.append(f"  {duration:.0f} ms", style="dim")
    if summary and summary != name:
        line.append(f"  {summary}", style="dim")
    return line


def _tool_label(name: str, category: str) -> str | None:
    """Return stable product language shared by Embedded and Remote events."""
    normalized = name.lower()
    if category == "edit" or normalized in {
        "write_file", "edit_file", "replace_lines", "apply_patch", "python_structural_edit",
    }:
        return "Edit"
    elif category == "read" and normalized in {
        "read_file", "read_image", "read_many_files", "code_references", "symbol_context",
    }:
        return "Read"
    elif normalized in {"repo_context", "repo_map", "repo_intel", "lookup"}:
        return "Repo Lookup"
    elif category in {"search", "read"} and normalized in {
        "grep_search", "smart_search", "search", "code_references", "semantic_search",
    }:
        return "Search"
    elif category == "command" or normalized == "bash":
        return "Bash"
    elif category == "process" or normalized == "process":
        return "Process"
    elif category == "web" or normalized in {"web_search", "webfetch", "web_fetch"}:
        return "Web Search"
    elif category == "agent" or normalized in {
        "task", "background_task_start", "background_task_apply", "subagent",
    }:
        return "Child"
    elif category == "verification" or normalized.startswith("verification"):
        return "Verification"
    elif category == "mcp" or normalized.startswith("mcp__"):
        return "MCP"
    return None


def _status_style(status: str) -> tuple[str, str]:
    if status == "ok":
        return "✓", "green"
    if status == "nonzero":
        return "!", "yellow"
    return "✗", "red"


def _error_fallback(name: str) -> str:
    if name == "MessageOutputLengthError":
        return "The model reached its output limit before completing the response."
    if name == "ContextOverflowError":
        return "The request exceeded the model context window."
    if name == "StructuredOutputError":
        return "The model returned output that did not match the required schema."
    return "The request failed."


def _error_action(name: str) -> str:
    if name == "ProviderAuthError":
        return "Run /connect to update provider credentials."
    if name == "ContextOverflowError":
        return "Run /compact, then retry the request."
    if name == "MessageOutputLengthError":
        return "Ask NZ-Coder to continue from the interrupted response."
    lowered = name.lower()
    if "ratelimit" in lowered or "rate_limit" in lowered:
        return "Wait briefly, then retry; use /model if the limit persists."
    if "network" in lowered or "connection" in lowered or "timeout" in lowered:
        return "Check the provider endpoint and network, then retry."
    if "model" in lowered and ("unavailable" in lowered or "notfound" in lowered):
        return "Run /model to choose an available model."
    if "configuration" in lowered or "config" in lowered:
        return "Run nz-coder doctor and correct the reported configuration."
    return ""


def _error_category(name: str, status: object = None) -> str:
    lowered = str(name).lower()
    if name == "ProviderAuthError" or status in {401, 403} or "auth" in lowered:
        return "Authentication"
    if status == 429 or "ratelimit" in lowered or "rate_limit" in lowered:
        return "Rate limit"
    if "model" in lowered and ("unavailable" in lowered or "notfound" in lowered):
        return "Model unavailable"
    if any(token in lowered for token in ("network", "connection", "timeout")):
        return "Network"
    if "config" in lowered:
        return "Invalid configuration"
    return str(name).removesuffix("Error") or "Error"


def _safe_error_message(value: str, limit: int) -> str:
    """Keep the useful terminal cause while hiding normal-mode stack traces."""
    clean = _sanitize(value, limit * 2)
    if "Traceback (most recent call last)" in clean:
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        clean = next(
            (line for line in reversed(lines) if not line.startswith(("File ", "Traceback"))),
            "The request failed. Run nz-coder doctor for diagnostics.",
        )
    return _sanitize(clean, limit)


def _output_preview(output: str, *, status: str, category: str) -> str:
    clean = _sanitize(output, 4_000)
    if not clean.strip():
        return ""
    lines = clean.splitlines()
    limit = 8 if status != "ok" else 5
    if category == "command" or status != "ok":
        selected = lines[-limit:]
        hidden = max(0, len(lines) - len(selected))
        prefix = f"… {hidden} earlier line(s)\n" if hidden else ""
        return _sanitize(prefix + "\n".join(selected), 900)
    selected = lines[:limit]
    hidden = max(0, len(lines) - len(selected))
    suffix = f"\n… {hidden} more line(s)" if hidden else ""
    return _sanitize("\n".join(selected) + suffix, 900)


def _sanitize(value: str, limit: int) -> str:
    cleaned = _CONTROL.sub("", _ANSI_ESCAPE.sub("", value.replace("\r\n", "\n")))
    if "\n" not in cleaned:
        return clip_terminal_text(cleaned, limit)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}…"


def _panel_width(console) -> int:  # noqa: ANN001
    try:
        width = int(getattr(console, "width", 100) or 100)
    except (TypeError, ValueError):
        width = 100
    return max(20, min(width, 100))
