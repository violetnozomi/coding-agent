"""Remote terminal attach frontend for the long-lived local product runtime."""
from __future__ import annotations

import argparse
import asyncio
import threading
import json
from dataclasses import dataclass
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any
from enum import Enum
from urllib.parse import urlsplit

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from nz_coder.http_service.client import NZCoderClient, NZCoderHTTPError
from nz_coder.foundation import config
from nz_coder.http_service.daemon import daemon_paths, daemon_status
from nz_coder.interface.backend import RemoteTerminalBackend
from nz_coder.interface.cli import StreamingRenderer
from nz_coder.interface.interactions import TerminalInteractionBridge
from nz_coder.interface.run_renderer import TerminalRunRenderer
from nz_coder.interface.remote_mailbox import (
    RemoteEventMailbox,
    RemoteTransportBridge,
    is_critical_remote_payload,
)
from nz_coder.protocol.public_error import (
    PublicError,
    PublicRuntimeError,
    public_error_from_wire,
    to_public_error,
)
from nz_coder.interface.terminal_input import TerminalInput
from nz_coder.interface.commands.registry import Command, CommandRegistry
from nz_coder.interface.timeline import format_transcript


def attach_main(argv: list[str] | None = None, *, console: Console | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nz-coder attach")
    parser.add_argument("session_id", nargs="?", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--state-root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--new", action="store_true")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_attach(args, console or Console()))
    except (OSError, RuntimeError, ValueError) as exc:
        (console or Console()).print(Text(f"Attach failed: {exc}", style="error"))
        return 2


async def _attach(args: argparse.Namespace, console: Console) -> int:
    url, token, _nonce, local_daemon = _connection(args)
    client = NZCoderClient(url, token, timeout=30)
    health = await asyncio.to_thread(client.health)
    if health.get("status") != "ok":
        raise RuntimeError("remote runtime health check failed")
    sessions = await asyncio.to_thread(client.list_sessions)
    if args.new or not sessions:
        info = await asyncio.to_thread(client.create_session, None, args.workspace_id)
    elif args.session_id:
        info = await asyncio.to_thread(client.get_session, args.session_id)
    else:
        info = max(
            sessions,
            key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
        )
    backend = RemoteTerminalBackend(client, str(info["id"]))
    console.print(
        f"[info]Attached[/info] session={backend.session_id} "
        f"status={info.get('status', 'unknown')} endpoint={url}"
    )
    messages = await asyncio.to_thread(client.messages, backend.session_id)
    if messages:
        console.print(Markdown(format_transcript(
            backend.session_id,
            messages[-20:],
            title="Attached session",
            tool_details=False,
        )))
    if info.get("status") == "running":
        await _follow_run(backend, console)

    remote_commands = await asyncio.to_thread(backend.commands)
    remote_registry = _remote_command_registry(remote_commands)
    location = _remote_location_label(url, local_daemon=local_daemon)

    def remote_state() -> dict[str, str]:
        return {
            "provider": str(info.get("provider") or "remote"),
            "model": str(info.get("model") or "unknown"),
            "mode": str(info.get("mode") or "default"),
            "session": backend.session_id,
            "session_title": str(info.get("title") or ""),
            "context": "",
            "workspace": str(info.get("workspace") or "server workspace"),
            "location": location,
            "run_state": str(info.get("status") or "idle"),
        }

    terminal_input = TerminalInput(
        console=console,
        registry=remote_registry,
        workspace=(
            Path(str(info.get("workspace") or Path.cwd()))
            if local_daemon
            else Path.cwd()
        ),
        state_provider=remote_state,
        fallback_reader=lambda: input("nz-coder >> "),
        interactive=None,
        attachments_enabled=local_daemon,
    )
    try:
        while True:
            try:
                query = await terminal_input.read_async()
            except (EOFError, KeyboardInterrupt):
                break
            text = str(query).strip()
            if not text:
                continue
            if text.lower() in {"exit", "quit", "/exit", "/quit"}:
                break
            submission_error = _remote_submission_error(text, (), local_daemon)
            if submission_error:
                console.print(Text(submission_error, style="error"))
                continue
            if text in {"/help", "/keys"}:
                _render_remote_help(console, remote_registry)
                continue
            if text in {"/status", "/session"}:
                _render_session_info(console, await asyncio.to_thread(backend.info))
                continue
            if text in {"/abort", "/stop"}:
                await asyncio.to_thread(backend.abort)
                continue
            if text.startswith("/sessions"):
                _render_sessions(console, await asyncio.to_thread(backend.sessions))
                continue
            if text.startswith("/resume ") or text.startswith("/attach "):
                target = text.split(maxsplit=1)[1].strip()
                info = await asyncio.to_thread(backend.select_session, target)
                _render_session_info(console, info)
                continue
            if text.startswith("/rename "):
                info = await asyncio.to_thread(backend.rename, text.split(maxsplit=1)[1])
                _render_session_info(console, info)
                continue
            if text == "/fork" or text.startswith("/fork "):
                raw = text.split(maxsplit=1)[1] if " " in text else ""
                info = await asyncio.to_thread(backend.fork, int(raw) if raw else None)
                await asyncio.to_thread(backend.select_session, str(info["id"]))
                console.print(Text(
                    f"Forked into {backend.session_id}.",
                    style="success",
                ))
                continue
            if text == "/delete-session":
                confirmed = await terminal_input.prompt_text_async(
                    f"Type {backend.session_id} to delete it and its processes: "
                )
                if confirmed != backend.session_id:
                    console.print("[info]Session deletion cancelled.[/info]")
                    continue
                if await asyncio.to_thread(backend.delete):
                    console.print(Text(
                        f"Deleted {backend.session_id}.",
                        style="success",
                    ))
                    break
                continue
            if text == "/timeline":
                messages = await asyncio.to_thread(backend.messages)
                console.print(Markdown(format_transcript(
                    backend.session_id,
                    messages,
                    title="Session timeline",
                    tool_details=False,
                )))
                continue
            if text.startswith("/message"):
                messages = await asyncio.to_thread(backend.messages)
                raw = text.split(maxsplit=1)[1] if " " in text else ""
                from nz_coder.interface.timeline import conversation_turns

                turns = conversation_turns(messages)
                selected = int(raw) if raw else (turns[-1].number if turns else 0)
                turn = next((item for item in turns if item.number == selected), None)
                if turn is None:
                    console.print("[error]No matching Session turn.[/error]")
                else:
                    console.print(Markdown(format_transcript(
                        backend.session_id,
                        messages[turn.start:turn.end],
                        title=f"Turn {selected}",
                    )))
                continue
            if text == "/diff":
                console.print(json.dumps(await asyncio.to_thread(backend.diff), ensure_ascii=False, indent=2))
                continue
            if text == "/undo":
                console.print(await asyncio.to_thread(backend.undo))
                continue
            if text == "/redo":
                console.print(await asyncio.to_thread(backend.redo))
                continue
            if text == "/parent":
                current = await asyncio.to_thread(backend.info)
                parent = str(current.get("parent_session_id") or "")
                if parent:
                    _render_session_info(console, await asyncio.to_thread(backend.select_session, parent))
                else:
                    console.print("[info]This Session has no parent.[/info]")
                continue
            if text == "/children":
                current = await asyncio.to_thread(backend.info)
                child_ids = list(current.get("children") or [])
                if child_ids:
                    sessions = await asyncio.to_thread(backend.sessions)
                    rows = [item for item in sessions if item.get("id") in child_ids]
                    _render_sessions(console, rows)
                else:
                    console.print("[info]This Session has no fork children.[/info]")
                continue
            if text == "/subagents":
                _render_subagents(console, await asyncio.to_thread(backend.children))
                continue
            if text == "/agents":
                _render_agents(console, await asyncio.to_thread(backend.agents))
                continue
            if text.startswith("/workflow"):
                await _workflow_command(backend, console, terminal_input, text)
                continue
            if text.startswith("/memory"):
                await _memory_command(backend, console, text)
                continue
            if text.startswith("/child "):
                child_id = text.split(maxsplit=1)[1].strip()
                console.print(await asyncio.to_thread(backend.child, child_id))
                continue
            if text.startswith("/export"):
                payload = await asyncio.to_thread(backend.export)
                raw = text.split(maxsplit=1)[1] if " " in text else ""
                if raw:
                    target = Path(raw).expanduser().resolve()
                    target.write_text(str(payload.get("markdown") or ""), encoding="utf-8")
                    console.print(Text(f"Session exported: {target}", style="success"))
                else:
                    console.print(Markdown(str(payload.get("markdown") or "")))
                continue
            if text.startswith("/process"):
                await _process_command(backend, console, text)
                continue
            if text == "/extensions":
                _render_extensions(console, await asyncio.to_thread(backend.extensions), "Extensions")
                continue
            if text == "/skills":
                _render_extensions(console, await asyncio.to_thread(backend.skills), "Skills")
                continue
            if text in {"/mcps", "/mcp"}:
                _render_extensions(console, await asyncio.to_thread(backend.mcps), "MCP servers")
                continue
            if text.startswith("/"):
                registered = remote_registry.get(text.split(maxsplit=1)[0])
                if registered is not None and registered.category == "Custom":
                    name, separator, arguments = text[1:].partition(" ")
                    expanded = await asyncio.to_thread(
                        backend.expand_command,
                        name,
                        arguments if separator else "",
                    )
                    submission, attachments = terminal_input.prepare_submission(
                        str(expanded.get("prompt") or "")
                    )
                    submission_error = _remote_submission_error(
                        submission,
                        attachments,
                        local_daemon,
                    )
                    if submission_error:
                        console.print(Text(submission_error, style="error"))
                        continue
                    await asyncio.to_thread(
                        backend.start_run,
                        submission,
                        attachments=[item.path for item in attachments],
                        allowed_tools=list(expanded.get("allowed_tools") or []),
                        model=expanded.get("model"),
                        command_digest=expanded.get("command_digest"),
                    )
                    await _follow_run(backend, console)
                    continue
            if text.startswith("/"):
                console.print(
                    f"[error]Unsupported remote command: {text.split()[0]} · use /help[/error]"
                )
                continue
            submission, attachments = terminal_input.prepare_submission(text)
            submission_error = _remote_submission_error(
                submission,
                attachments,
                local_daemon,
            )
            if submission_error:
                console.print(Text(submission_error, style="error"))
                continue
            await asyncio.to_thread(
                backend.start_run,
                submission,
                attachments=[item.path for item in attachments],
            )
            await _follow_run(backend, console)
    finally:
        await terminal_input.close_async()
        backend.close()
    return 0


class _RemotePumpExit(str, Enum):
    """Explicit terminal reason for one cursor-bound SSE reader."""

    SETTLED = "settled"
    CLEAN_EOF = "clean_eof"
    STOP_REQUESTED = "stop_requested"
    TRANSPORT_ERROR = "transport_error"


class _RemoteStreamPump:
    """Own one SSE iterator, its thread, and its close lifecycle."""

    def __init__(self, stream: Any, transport: RemoteTransportBridge) -> None:
        self.stream = stream
        self.transport = transport
        self._stop = threading.Event()
        self._done = threading.Event()
        self.exit_reason: _RemotePumpExit | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="nz-remote-events",
            daemon=True,
        )

    def start(self) -> "_RemoteStreamPump":
        self.thread.start()
        return self

    def request_stop(self) -> None:
        """Fence late publications; iterator close remains owner-thread-only."""
        self._stop.set()
        self.transport.deactivate()

    def wait(self, timeout: float | None = None) -> bool:
        if not self._done.wait(timeout):
            return False
        self.thread.join(timeout=0)
        return not self.thread.is_alive()

    def _run(self) -> None:
        try:
            self.exit_reason = _pump_remote_stream(
                self.stream,
                self.transport,
                stop_requested=self._stop,
            )
        finally:
            self._done.set()


def _pump_remote_stream(
    stream: Any,
    transport: RemoteTransportBridge,
    *,
    stop_requested: threading.Event | None = None,
) -> _RemotePumpExit:
    """Copy one SSE stream; only this iterator owner may close it."""
    stop = stop_requested or threading.Event()
    reason = _RemotePumpExit.CLEAN_EOF
    try:
        for payload in stream:
            if stop.is_set():
                reason = _RemotePumpExit.STOP_REQUESTED
                break
            accepted = transport.offer(payload)
            if not accepted:
                if stop.is_set():
                    reason = _RemotePumpExit.STOP_REQUESTED
                    break
                transport.fail_closed(
                    PublicError(
                        "remote_transport_overflow",
                        "The remote event stream requires resynchronization.",
                        retryable=True,
                    ),
                    reconnect_required=True,
                )
                reason = _RemotePumpExit.TRANSPORT_ERROR
                break
            if payload.get("type") == "session.run.settled":
                reason = _RemotePumpExit.SETTLED
                break
            if (
                payload.get("type") == "server.event_gap"
                or transport.rebase_required
            ):
                # Stop before requesting another potentially blocking HTTP
                # read. The frontend will establish the next snapshot epoch.
                reason = _RemotePumpExit.STOP_REQUESTED
                break
    except Exception as exc:
        reason = _RemotePumpExit.TRANSPORT_ERROR
        if not stop.is_set():
            transport.fail_closed(to_public_error(exc), reconnect_required=True)
        else:
            reason = _RemotePumpExit.STOP_REQUESTED
    finally:
        try:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            if not stop.is_set():
                reason = _RemotePumpExit.TRANSPORT_ERROR
                transport.fail_closed(to_public_error(exc), reconnect_required=True)
        transport.close_reader(reason.value)
    return reason


@dataclass(frozen=True)
class _ReconnectedRemoteStream:
    """Authoritative replacement for one invalidated remote SSE stream."""

    pump: _RemoteStreamPump | None
    transport: RemoteTransportBridge
    cursor: str | None
    settled: bool
    terminal_status: str


def _remote_gap_requires_rebase(properties: object) -> bool:
    """Return whether a gap proves the current stream is non-contiguous."""
    value = properties if isinstance(properties, dict) else {}
    reason = str(value.get("overflow_reason") or value.get("reason") or "")
    return bool(
        value.get("resume_required") is True
        or value.get("local_queue_overflow") is True
        or reason in {
            "remote_mailbox_overflow",
            "subscriber_queue_overflow",
            "cursor_expired",
            "event_cursor_expired",
        }
    )


async def _rebase_remote_stream(
    *,
    backend: RemoteTerminalBackend,
    pump: _RemoteStreamPump,
    transport: RemoteTransportBridge,
    run_view: TerminalRunRenderer,
    bridge: TerminalInteractionBridge,
    interaction_tasks: "_InteractionTaskRegistry",
    loop: asyncio.AbstractEventLoop,
) -> _ReconnectedRemoteStream:
    """Retire one reader and resume from one authoritative snapshot."""
    try:
        pump.request_stop()
        stopped = await asyncio.to_thread(pump.wait, 5.0)
        if not stopped:
            raise PublicRuntimeError(PublicError(
                "remote_stream_shutdown_timeout",
                "The previous remote event stream did not stop safely.",
                retryable=True,
            ))
        try:
            latest = await asyncio.to_thread(backend.attach_snapshot)
        except Exception as exc:
            raise PublicRuntimeError(PublicError(
                "remote_stream_ended",
                "The remote event stream ended and its run state could not be recovered.",
                retryable=True,
            )) from exc
        if not isinstance(latest, dict):
            raise ValueError("remote snapshot must be an object")
        _feed_snapshot_events(run_view, latest)
        _register_pending_interactions(
            backend,
            latest.get("pending") or {},
            bridge,
            interaction_tasks,
        )
        session = (
            latest.get("session")
            if isinstance(latest.get("session"), dict)
            else {}
        )
        terminal_status = str(session.get("status") or "unknown")
        settled = bool(latest.get("settled"))
        running = bool(session.get("running")) or terminal_status == "running"
        cursor = str((latest.get("cursor") or {}).get("event_id") or "") or None
        transport.clear_gap()
        if settled:
            return _ReconnectedRemoteStream(
                pump=None,
                transport=transport,
                cursor=cursor,
                settled=True,
                terminal_status=terminal_status,
            )
        if not running:
            raise PublicRuntimeError(PublicError(
                "remote_stream_ended",
                "The remote event stream ended without an authoritative run state.",
                retryable=True,
            ))
        replacement = RemoteTransportBridge(
            loop,
            capacity=getattr(config, "REMOTE_EVENT_QUEUE_SIZE", 512),
            critical_reserve=16,
        )
        replacement_stream = backend.events(last_event_id=cursor)
        replacement_pump = _RemoteStreamPump(
            replacement_stream,
            replacement,
        ).start()
        return _ReconnectedRemoteStream(
            pump=replacement_pump,
            transport=replacement,
            cursor=cursor,
            settled=False,
            terminal_status=terminal_status,
        )
    except PublicRuntimeError:
        raise
    except Exception as exc:
        raise PublicRuntimeError(to_public_error(exc)) from exc


async def _follow_run(backend: RemoteTerminalBackend, console: Console) -> None:
    baseline = await asyncio.to_thread(backend.attach_snapshot)
    cursor = str((baseline.get("cursor") or {}).get("event_id") or "") or None
    renderer = StreamingRenderer(console)
    renderer.start()
    run_view = TerminalRunRenderer(console, renderer)
    run_view.begin_remote(SimpleNamespace(model_id="remote"))
    bridge = TerminalInteractionBridge(
        TerminalInput(
            console=console,
            registry=_remote_command_registry(),
            fallback_reader=lambda: "",
            interactive=None,
        ),
        renderer,
        asyncio.get_running_loop(),
    )
    interaction_input = bridge.terminal_input
    loop = asyncio.get_running_loop()
    transport = RemoteTransportBridge(
        loop,
        capacity=getattr(config, "REMOTE_EVENT_QUEUE_SIZE", 512),
        critical_reserve=16,
    )
    pump = None

    terminal_status = str((baseline.get("session") or {}).get("status") or "unknown")
    reconnecting = False
    interaction_tasks = _InteractionTaskRegistry(
        on_error=lambda exc: transport.fail_closed(
            to_public_error(exc),
            reconnect_required=False,
        ),
    )
    reader_done = False
    transport_reconnects = 0
    try:
        _feed_snapshot_events(run_view, baseline)
        if not bool((baseline.get("session") or {}).get("running")):
            _register_pending_interactions(
                backend,
                baseline.get("pending") or {},
                bridge,
                interaction_tasks,
            )
            await interaction_tasks.wait()
            return
        stream = backend.events(last_event_id=cursor)
        pump = _RemoteStreamPump(stream, transport).start()
        _register_pending_interactions(
            backend,
            baseline.get("pending") or {},
            bridge,
            interaction_tasks,
        )
        while not reader_done or transport.buffered_count:
            try:
                payload = await transport.get(timeout=0.25)
            except asyncio.TimeoutError:
                interaction_tasks.raise_if_failed()
                continue
            if payload.get("_transport_done"):
                exit_reason = str(payload.get("_exit_reason") or "clean_eof")
                if exit_reason == _RemotePumpExit.SETTLED.value:
                    reader_done = True
                    continue
                if exit_reason == _RemotePumpExit.STOP_REQUESTED.value:
                    reader_done = True
                    continue
                if transport_reconnects >= 3:
                    raise PublicRuntimeError(PublicError(
                        "remote_stream_ended",
                        "The remote event stream ended before the run settled.",
                        retryable=True,
                    ))
                transport_reconnects += 1
                reconnecting = True
                console.print("[info]Reconnecting…[/info]")
                replacement = await _rebase_remote_stream(
                    backend=backend,
                    pump=pump,
                    transport=transport,
                    run_view=run_view,
                    bridge=bridge,
                    interaction_tasks=interaction_tasks,
                    loop=loop,
                )
                pump = replacement.pump
                transport = replacement.transport
                cursor = replacement.cursor
                terminal_status = replacement.terminal_status
                if replacement.settled:
                    reader_done = True
                    break
                reader_done = False
                console.print("[success]Reconnected[/success]")
                reconnecting = False
                continue
            if payload.get("_error"):
                public = public_error_from_wire(payload["_error"])
                if payload.get("_reconnect_required") and transport_reconnects < 3:
                    transport_reconnects += 1
                    reconnecting = True
                    console.print("[info]Reconnecting…[/info]")
                    replacement = await _rebase_remote_stream(
                        backend=backend,
                        pump=pump,
                        transport=transport,
                        run_view=run_view,
                        bridge=bridge,
                        interaction_tasks=interaction_tasks,
                        loop=loop,
                    )
                    pump = replacement.pump
                    transport = replacement.transport
                    cursor = replacement.cursor
                    terminal_status = replacement.terminal_status
                    if replacement.settled:
                        reader_done = True
                        break
                    reader_done = False
                    console.print("[success]Reconnected[/success]")
                    reconnecting = False
                    continue
                raise PublicRuntimeError(public or to_public_error(None))
            event_type = payload.get("type")
            if event_type == "server.snapshot":
                latest = payload.get("properties") or {}
                _feed_snapshot_events(run_view, latest)
                _register_pending_interactions(
                    backend,
                    latest.get("pending") or {},
                    bridge,
                    interaction_tasks,
                )
                transport.clear_gap()
                if reconnecting:
                    console.print("[success]Reconnected[/success]")
                    reconnecting = False
                continue
            if event_type == "server.event_gap":
                console.print("[info]Reconnecting…[/info]")
                reconnecting = True
                if _remote_gap_requires_rebase(payload.get("properties")):
                    if transport_reconnects >= 3:
                        raise PublicRuntimeError(PublicError(
                            "remote_reconnect_exhausted",
                            "The remote event stream could not be resynchronized.",
                            retryable=True,
                        ))
                    transport_reconnects += 1
                    replacement = await _rebase_remote_stream(
                        backend=backend,
                        pump=pump,
                        transport=transport,
                        run_view=run_view,
                        bridge=bridge,
                        interaction_tasks=interaction_tasks,
                        loop=loop,
                    )
                    pump = replacement.pump
                    transport = replacement.transport
                    cursor = replacement.cursor
                    terminal_status = replacement.terminal_status
                    if replacement.settled:
                        reader_done = True
                        break
                    reader_done = False
                    console.print("[success]Reconnected[/success]")
                    reconnecting = False
                continue
            if event_type in {"permission.asked", "question.asked"}:
                _register_interaction_payload(
                    backend,
                    bridge,
                    payload,
                    interaction_tasks,
                )
                # Give the prompt task one scheduling turn even when the SSE
                # reader has already filled the mailbox through terminal.
                await asyncio.sleep(0)
                continue
            event = payload
            run_view.feed(event)
            meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
            transport.mark_applied(int(meta.get("sequence") or 0))
            if event_type in {"session.run.completed", "session.run.failed", "session.run.cancelled", "session.run.settled"}:
                terminal_status = str(
                    (event.get("properties") or {}).get("status") or terminal_status
                )
                if event_type == "session.run.settled":
                    reader_done = True
                interaction_tasks.cancel()
    finally:
        interaction_tasks.cancel()
        await interaction_tasks.wait(return_exceptions=True)
        if pump is not None:
            pump.request_stop()
            await asyncio.to_thread(pump.wait, 5.0)
        try:
            final_snapshot = await asyncio.to_thread(backend.attach_snapshot)
            _feed_snapshot_events(run_view, final_snapshot)
        except Exception:
            # Final reconciliation is best effort; cleanup and the original
            # stream outcome are more authoritative than a second HTTP error.
            pass
        try:
            renderer.finish()
        finally:
            try:
                run_view.finish({"status": terminal_status})
            finally:
                run_view.close()
                await interaction_input.close_async()


def _offer_remote_payload(
    event_queue: asyncio.Queue | RemoteEventMailbox,
    payload: dict,
) -> None:
    """Compatibility offer with semantic eviction for older queue consumers."""
    if isinstance(event_queue, RemoteEventMailbox):
        event_queue.offer(payload)
        return
    try:
        event_queue.put_nowait(payload)
        return
    except asyncio.QueueFull:
        pass
    critical = is_critical_remote_payload(payload)
    queued = getattr(event_queue, "_queue", ())
    if not critical and any(
        isinstance(item, dict)
        and item.get("type") == "server.event_gap"
        and (item.get("properties") or {}).get("local_queue_overflow")
        for item in queued
    ):
        return
    queued = getattr(event_queue, "_queue", ())
    removable = next((
        item for item in queued
        if isinstance(item, dict)
        and not is_critical_remote_payload(item)
    ), None)
    if removable is None:
        return
    queued.remove(removable)
    event_queue._unfinished_tasks = max(0, event_queue._unfinished_tasks - 1)
    replacement = payload if critical else {
        "type": "server.event_gap",
        "properties": {
            "local_queue_overflow": True,
            "resume_required": True,
        },
    }
    try:
        event_queue.put_nowait(replacement)
    except asyncio.QueueFull:
        return


class _InteractionTaskRegistry:
    """Deduplicate pending permission/question prompts by request identity."""

    def __init__(self, on_error=None) -> None:  # noqa: ANN001
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._resolved: set[tuple[str, str]] = set()
        self._failures: list[BaseException] = []
        self._on_error = on_error

    def register(self, kind: str, request_id: str, factory) -> asyncio.Task | None:  # noqa: ANN001
        key = (str(kind), str(request_id))
        if not key[1] or key in self._resolved:
            return None
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(factory())
        self._tasks[key] = task

        def settled(completed: asyncio.Task) -> None:
            self._tasks.pop(key, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is None:
                self._resolved.add(key)
                return
            self._failures.append(error)
            if callable(self._on_error):
                self._on_error(error)

        task.add_done_callback(settled)
        return task

    def cancel(self) -> None:
        for task in tuple(self._tasks.values()):
            if not task.done():
                task.cancel()

    async def wait(self, *, return_exceptions: bool = False) -> None:
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not return_exceptions:
            self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._failures:
            raise self._failures.pop(0)


def _register_interaction_payload(
    backend: RemoteTerminalBackend,
    bridge: TerminalInteractionBridge,
    payload: dict,
    registry: _InteractionTaskRegistry,
) -> asyncio.Task | None:
    event_type = str(payload.get("type") or "")
    properties = (
        payload.get("properties")
        if isinstance(payload.get("properties"), dict)
        else {}
    )
    request_id = str(properties.get("id") or "")
    kind = "permission" if event_type == "permission.asked" else "question"
    return registry.register(
        kind,
        request_id,
        lambda: _resolve_remote_interaction(backend, bridge, payload),
    )


def _register_pending_interactions(
    backend: RemoteTerminalBackend,
    pending: dict[str, Any],
    bridge: TerminalInteractionBridge,
    registry: _InteractionTaskRegistry,
) -> None:
    """Register snapshot and live interactions through the same dedupe path."""
    selected = pending if isinstance(pending, dict) else {}
    for item in selected.get("permissions", []):
        if not isinstance(item, dict):
            continue
        _register_interaction_payload(
            backend,
            bridge,
            {"type": "permission.asked", "properties": dict(item)},
            registry,
        )
    for item in selected.get("questions", []):
        if not isinstance(item, dict):
            continue
        _register_interaction_payload(
            backend,
            bridge,
            {"type": "question.asked", "properties": dict(item)},
            registry,
        )


async def _resolve_remote_interaction(
    backend: RemoteTerminalBackend,
    bridge: TerminalInteractionBridge,
    payload: dict,
) -> None:
    """Collect user input without pausing the SSE reducer consumer."""
    event_type = str(payload.get("type") or "")
    props = (
        payload.get("properties")
        if isinstance(payload.get("properties"), dict)
        else {}
    )
    request_id = str(props.get("id") or "")
    if not request_id:
        return
    if event_type == "permission.asked":
        reply = await bridge._ask_permission(
            str(props.get("permission") or "tool"),
            props.get("tool_input")
            if isinstance(props.get("tool_input"), dict)
            else {},
        )
        await _reply_once(backend.reply_permission, request_id, reply)
        return
    result = await bridge._ask_questions(props.get("questions", []))
    if result is None:
        await _reply_once(backend.reject_question, request_id)
    else:
        await _reply_once(backend.reply_question, request_id, result)


def _feed_snapshot_events(run_view: TerminalRunRenderer, snapshot: dict[str, Any]) -> None:
    run = snapshot.get("run") if isinstance(snapshot, dict) else None
    run = run if isinstance(run, dict) else {}
    interaction_run_id = str(
        run.get("interaction_run_id")
        or snapshot.get("active_interaction_run_id")
        or ""
    )
    run_view.rebase_remote(
        (
            run.get("messages")
            if isinstance(run.get("messages"), list)
            else snapshot.get("messages")
            if isinstance(snapshot, dict)
            else None
        ),
        interaction_run_id=interaction_run_id,
        run=run,
    )
    for payload in snapshot.get("events", []) if isinstance(snapshot, dict) else []:
        if not isinstance(payload, dict):
            continue
        if payload.get("type") in {"permission.asked", "question.asked"}:
            continue
        run_view.feed(payload)


async def _resolve_pending(
    backend: RemoteTerminalBackend,
    pending: dict[str, Any],
    bridge: TerminalInteractionBridge,
) -> None:
    """Resume interactions that were already waiting before this attach."""
    permissions = pending.get("permissions", []) if isinstance(pending, dict) else []
    for item in permissions:
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("id") or "")
        if not request_id:
            continue
        reply = await bridge._ask_permission(
            str(item.get("permission") or "tool"),
            item.get("tool_input") if isinstance(item.get("tool_input"), dict) else {},
        )
        await _reply_once(backend.reply_permission, request_id, reply)
    questions = pending.get("questions", []) if isinstance(pending, dict) else []
    for item in questions:
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("id") or "")
        payload = item.get("questions")
        if not request_id or not isinstance(payload, list):
            continue
        answers = await bridge._ask_questions(payload)
        if answers is None:
            await _reply_once(backend.reject_question, request_id)
        else:
            await _reply_once(backend.reply_question, request_id, answers)


async def _reply_once(callback, *args) -> bool:  # noqa: ANN001
    """First valid interaction response wins across attached clients."""
    try:
        return bool(await asyncio.to_thread(callback, *args))
    except NZCoderHTTPError as exc:
        if exc.code == "interaction_not_found":
            return False
        raise


def _render_session_info(console: Console, info: dict[str, Any]) -> None:
    status = str(info.get("runtime_status") or info.get("status") or "unknown").upper()
    console.print(Panel(
        "\n".join((
            f"{info.get('title') or 'New Session'}",
            f"id: {info.get('id')}",
            f"status: {status}",
            f"workspace: {info.get('workspace')}",
            f"model: {info.get('model') or 'unknown'}",
            f"provider: {info.get('provider') or 'unknown'}",
            f"permission: {info.get('permission_mode') or info.get('mode') or 'unknown'}",
            f"parent: {info.get('parent_session_id') or '-'}",
            f"children: {', '.join(info.get('children') or []) or '-'}",
        )),
        title="Session",
        border_style="cyan",
    ))


def _render_sessions(console: Console, sessions: list[dict[str, Any]]) -> None:
    table = Table(title="Remote sessions", expand=True)
    table.add_column("Session", style="cyan")
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Workspace")
    table.add_column("Model")
    table.add_column("Parent")
    for item in sessions:
        table.add_row(
            str(item.get("id") or ""),
            str(item.get("runtime_status") or item.get("status") or "").upper(),
            str(item.get("title") or ""),
            str(item.get("workspace") or ""),
            str(item.get("model") or "unknown"),
            str(item.get("parent_session_id") or "-"),
        )
    console.print(table)


def _render_subagents(console: Console, children: list[dict[str, Any]]) -> None:
    table = Table(title="Child Agent sessions", expand=True)
    for heading in ("Session", "Agent", "Status", "Messages", "Model"):
        table.add_column(heading)
    for item in children:
        table.add_row(
            str(item.get("session_id") or ""),
            str(item.get("agent_type") or "unknown"),
            str(item.get("status") or "unknown").upper(),
            str(item.get("message_count") or 0),
            str(item.get("model_id") or "-"),
        )
    if not children:
        table.add_row("-", "-", "NONE", "0", "-")
    console.print(table)


def _render_agents(console: Console, agents: list[dict[str, Any]]) -> None:
    table = Table(title="Agent definitions", expand=True)
    for heading in ("Name", "Role", "Model", "Tools", "Permissions", "Description"):
        table.add_column(heading)
    for item in agents:
        table.add_row(
            str(item.get("name") or ""),
            str(item.get("role") or ""),
            str(item.get("model") or "-"),
            str(item.get("tools") or "-"),
            str(item.get("permissions") or "-"),
            str(item.get("description") or ""),
        )
    if not agents:
        table.add_row("-", "-", "-", "-", "-", "No Agent definitions")
    console.print(table)


async def _workflow_command(
    backend: RemoteTerminalBackend,
    console: Console,
    terminal_input,
    text: str,
) -> None:
    parts = text.split(maxsplit=2)
    action = parts[1].lower() if len(parts) > 1 else "list"
    run_id = parts[2].strip() if len(parts) > 2 else ""
    if action == "list":
        payload = await asyncio.to_thread(backend.workflows)
        rows = list(payload.get("runs") or [])
        table = Table(title="Workflow runs", expand=True)
        for heading in ("Run", "Name", "Status", "Started"):
            table.add_column(heading)
        for item in rows:
            table.add_row(
                str(item.get("run_id") or ""),
                str(item.get("name") or item.get("workflow_name") or "workflow"),
                str(item.get("status") or "unknown").upper(),
                str(item.get("started_at") or "-"),
            )
        console.print(table if rows else "[info]No workflow runs.[/info]")
        return
    if action == "show" and run_id:
        console.print_json(
            json.dumps(await asyncio.to_thread(backend.workflow, run_id), ensure_ascii=False)
        )
        return
    if action == "run" and run_id:
        name, separator, raw_arguments = run_id.partition(" ")
        arguments = {}
        if separator and raw_arguments.strip():
            try:
                decoded = json.loads(raw_arguments)
            except json.JSONDecodeError:
                decoded = {"question": raw_arguments.strip()}
            if not isinstance(decoded, dict):
                console.print("[error]Workflow arguments must be a JSON object.[/error]")
                return
            arguments = decoded
        prepared = await asyncio.to_thread(backend.prepare_workflow, name, arguments)
        summary = dict(prepared.get("summary") or {})
        decision = await terminal_input.select_async(
            title="Remote workflow approval",
            text=(
                f"{summary.get('name', name)} — {summary.get('description', '')}\n"
                f"Phases: {', '.join(summary.get('phases') or []) or 'unspecified'}\n"
                f"Agents: {summary.get('planned_agents', 'unspecified')} planned; "
                f"concurrency {summary.get('max_concurrency', 'unspecified')}\n"
                f"Risk: {'may write files' if summary.get('writes_files') else 'read-only'}"
            ),
            values=[
                ("approve", "Approve this exact daemon-side plan"),
                ("cancel", "Cancel without starting"),
            ],
        )
        if decision != "approve":
            console.print("[info]Workflow cancelled.[/info]")
            return
        started = await asyncio.to_thread(
            backend.start_workflow,
            name,
            arguments,
            approval_digest=str(prepared.get("approval_digest") or ""),
        )
        console.print(
            f"[success]Started workflow {started.get('name', name)} "
            f"({started.get('run_id', '-')}).[/success]"
        )
        return
    if action in {"pause", "resume", "stop"} and run_id:
        result = await asyncio.to_thread(backend.control_workflow, run_id, action)
        status = str(result.get("status") or "unknown")
        console.print(Text(f"Workflow {run_id}: {status}.", style="success"))
        return
    console.print(
        "[error]Usage: /workflow [list|show ID|run NAME [JSON_ARGS|REQUEST]|"
        "pause ID|resume ID|stop ID][/error]"
    )


async def _memory_command(
    backend: RemoteTerminalBackend,
    console: Console,
    text: str,
) -> None:
    parts = text.split(maxsplit=3)
    action = parts[1].lower() if len(parts) > 1 else "pending"
    if action in {"pending", "ledger"}:
        payload = await asyncio.to_thread(backend.memory)
        if action == "ledger":
            console.print_json(json.dumps(payload.get("ledger") or [], ensure_ascii=False))
            return
        rows = list(payload.get("pending") or [])
        table = Table(title="Memory review inbox", expand=True)
        for heading in ("Fingerprint", "Name", "Risk", "Confidence", "Source"):
            table.add_column(heading)
        for item in rows:
            table.add_row(
                str(item.get("fingerprint") or "")[:16],
                str(item.get("name") or ""),
                str(item.get("risk") or ""),
                str(item.get("confidence") or ""),
                str(item.get("source_session") or "-"),
            )
        console.print(table if rows else "[info]No memory proposals are pending review.[/info]")
        return
    if action == "inspect" and len(parts) >= 3:
        console.print_json(json.dumps(
            await asyncio.to_thread(backend.memory_proposal, parts[2]),
            ensure_ascii=False,
        ))
        return
    if action in {"approve", "reject"} and len(parts) >= 3:
        result = await asyncio.to_thread(
            backend.review_memory,
            parts[2],
            action,
            reason=parts[3] if len(parts) > 3 else "",
        )
        console.print(
            f"[success]Memory proposal {str(result.get('fingerprint') or '')[:16]} "
            f"is {result.get('status', 'unknown')}.[/success]"
        )
        return
    console.print(
        "[error]Usage: /memory [pending|inspect FINGERPRINT|approve FINGERPRINT|"
        "reject FINGERPRINT [REASON]|ledger][/error]"
    )


def _remote_command_registry(custom_commands=()) -> CommandRegistry:  # noqa: ANN001
    """Expose only commands implemented by the attached terminal controller."""
    registry = CommandRegistry()

    def remote_command(*_args, **_kwargs) -> None:
        return None

    for name, description, usage, aliases in (
        ("help", "Show remote commands", "/help", ("keys",)),
        ("status", "Inspect the attached Session", "/status", ("session",)),
        ("sessions", "List daemon Sessions", "/sessions", ()),
        ("resume", "Attach another Session", "/resume SESSION_ID", ("attach",)),
        ("abort", "Cancel the active run", "/abort", ("stop",)),
        ("rename", "Rename the attached Session", "/rename TITLE", ()),
        ("fork", "Fork through a completed turn", "/fork [TURN]", ()),
        ("delete-session", "Delete this Session after confirmation", "/delete-session", ()),
        ("timeline", "Show the Session transcript", "/timeline", ()),
        ("message", "Inspect one Session turn", "/message [TURN]", ()),
        ("diff", "Show the latest Session diff", "/diff", ()),
        ("undo", "Undo the latest revertible turn", "/undo", ()),
        ("redo", "Restore the latest undone turn", "/redo", ()),
        ("parent", "Attach the parent fork", "/parent", ()),
        ("children", "List fork children", "/children", ()),
        ("subagents", "List child Agent sessions", "/subagents", ()),
        ("agents", "Inspect available Agent definitions", "/agents", ()),
        (
            "workflow",
            "Inspect and control daemon workflow runs",
            "/workflow [list|show ID|run NAME [JSON_ARGS|REQUEST]|pause ID|resume ID|stop ID]",
            ("workflows",),
        ),
        (
            "memory",
            "Review daemon Session memory proposals",
            "/memory [pending|inspect FINGERPRINT|approve FINGERPRINT|reject FINGERPRINT [REASON]|ledger]",
            ("memory-review",),
        ),
        ("child", "Inspect one child Agent", "/child ID", ()),
        ("export", "Render or save the Session transcript", "/export [PATH]", ()),
        (
            "processes",
            "Inspect Session-owned persistent processes",
            "/processes [list|inspect|logs|follow|kill] [PROCESS_ID]",
            ("process",),
        ),
        ("extensions", "Inspect daemon workspace extensions", "/extensions", ()),
        ("skills", "Inspect daemon workspace skills", "/skills", ()),
        ("mcps", "Inspect daemon workspace MCP servers", "/mcps", ("mcp",)),
        ("exit", "Detach this terminal", "/exit", ("quit",)),
    ):
        registry.register(Command(
            name,
            description,
            usage,
            remote_command,
            aliases=aliases,
            category="Remote",
        ))
    for item in custom_commands:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            registry.register(Command(
                name,
                str(item.get("description") or "Run a custom prompt command"),
                f"/{name} [arguments]",
                remote_command,
                category="Custom",
            ))
        except ValueError:
            # Runtime controls cannot be shadowed by workspace prompt files.
            continue
    return registry


def _render_extensions(console: Console, items: list[dict], title: str) -> None:
    table = Table(title=title, expand=True)
    for heading in ("ID", "Status", "Source", "Health"):
        table.add_column(heading)
    for item in items:
        table.add_row(
            str(item.get("extension_id") or item.get("name") or "-"),
            str(item.get("status") or "unknown").upper(),
            str(item.get("source") or "-"),
            str(item.get("health") or "unknown"),
        )
    if not items:
        table.add_row("-", "NONE", "-", "-")
    console.print(table)


def _render_remote_help(
    console: Console,
    registry: CommandRegistry | None = None,
) -> None:
    table = Table(title="Remote commands", expand=True)
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description")
    for command in (registry or _remote_command_registry()).visible_commands():
        table.add_row(command.usage, command.description)
    console.print(table)


async def _process_command(
    backend: RemoteTerminalBackend,
    console: Console,
    text: str,
) -> None:
    parts = text.split()
    operation = parts[1].lower() if len(parts) > 1 else "list"
    process_id = parts[2] if len(parts) > 2 else ""
    if operation in {"list", "ls"}:
        values = await asyncio.to_thread(backend.processes)
        table = Table(title="Persistent processes", expand=True)
        for heading in ("Process", "Status", "Command", "CWD", "Uptime", "Exit", "Owner", "PTY"):
            table.add_column(heading)
        for item in values:
            table.add_row(
                str(item.get("process_id") or ""),
                str(item.get("status") or "").upper(),
                str(item.get("command") or ""),
                str(item.get("cwd") or ""),
                _format_uptime(item.get("started_at")),
                str(item.get("exit_code") if item.get("exit_code") is not None else "-"),
                str(item.get("owner_session_id") or "-"),
                str(item.get("pty_tier") or "pipe"),
            )
        if not values:
            table.add_row("-", "NONE", "", "", "-", "-", "-", "-")
        console.print(table)
        return
    if operation in {"inspect", "status"} and process_id:
        item = await asyncio.to_thread(backend.process, process_id)
        console.print(Panel(
            "\n".join((
                f"process_id: {item.get('process_id')}",
                f"command: {item.get('command')}",
                f"cwd: {item.get('cwd')}",
                f"pid: {item.get('pid')}",
                f"started_at: {item.get('started_at')}",
                f"uptime: {_format_uptime(item.get('started_at'))}",
                f"status: {item.get('status')}",
                f"exit_code: {item.get('exit_code')}",
                f"owner_session_id: {item.get('owner_session_id')}",
                f"owner_agent_id: {item.get('owner_agent_id')}",
                f"buffer_bytes: {item.get('buffer_bytes')}",
                f"pty_tier: {item.get('pty_tier')}",
            )),
            title="Persistent process",
            border_style="cyan",
        ))
        return
    if operation in {"logs", "read"} and process_id:
        result = await asyncio.to_thread(
            backend.process_read,
            process_id,
            tail_bytes=8192,
            max_bytes=8192,
        )
        console.print(Panel(
            str(result.get("output") or "(no output)"),
            title=f"Process logs · {process_id}",
            border_style="cyan",
        ))
        return
    if operation == "follow" and process_id:
        cursor = -1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while loop.time() < deadline:
            result = await asyncio.to_thread(
                backend.process_read,
                process_id,
                cursor=cursor,
                tail_bytes=None,
                max_bytes=8192,
                wait_seconds=1.0,
            )
            cursor = int(result.get("next_cursor") or cursor)
            output = str(result.get("output") or "")
            if output:
                console.print(output, markup=False, highlight=False)
            if result.get("status") not in {"starting", "running"}:
                break
        return
    if operation == "kill" and process_id:
        result = await asyncio.to_thread(backend.process_kill, process_id)
        console.print(Text(
            f"{process_id}: {str(result.get('status') or '').upper()}",
            style="success",
        ))
        return
    if operation == "write" and process_id and len(parts) > 3:
        data = text.split(maxsplit=3)[3]
        result = await asyncio.to_thread(backend.process_write, process_id, data)
        console.print(Text(
            f"{process_id}: {str(result.get('status') or '').upper()}",
            style="success",
        ))
        return
    if operation == "resize" and process_id and len(parts) == 5:
        result = await asyncio.to_thread(
            backend.process_resize,
            process_id,
            rows=int(parts[3]),
            cols=int(parts[4]),
        )
        console.print(Text(
            f"{process_id}: resized to {parts[4]}x{parts[3]}",
            style="success",
        ))
        return
    console.print(
        "[error]Usage: /processes [list|inspect|logs|follow|write ID DATA|"
        "resize ID ROWS COLS|kill] [PROCESS_ID][/error]"
    )


def _format_uptime(started_at: object) -> str:
    try:
        seconds = max(0, int(time.time() - float(started_at)))
    except (TypeError, ValueError):
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}"


def _connection(args: argparse.Namespace) -> tuple[str, str, str, bool]:
    paths = daemon_paths(args.profile, args.state_root)
    status = daemon_status(args.profile, state_root=args.state_root)
    url = str(args.url or status.get("endpoint") or "")
    if not url:
        raise RuntimeError("daemon is not running; start it with `nz-coder daemon start`")
    if args.url and not args.token_file:
        local_endpoint = str(status.get("endpoint") or "")
        if not local_endpoint or args.url.rstrip("/") != local_endpoint.rstrip("/"):
            raise RuntimeError("attaching an explicit URL requires --token-file")
    token_path = args.token_file or str(paths.token)
    token = open(token_path, encoding="utf-8").read().strip()
    if len(token) < 16:
        raise RuntimeError("daemon token is missing or invalid")
    local_endpoint = str(status.get("endpoint") or "")
    local_daemon = bool(
        local_endpoint and url.rstrip("/") == local_endpoint.rstrip("/")
    )
    return url, token, str(status.get("nonce") or ""), local_daemon


def _remote_location_label(url: str, *, local_daemon: bool) -> str:
    """Return the persistent execution-location label shown in the composer."""
    if local_daemon:
        return "LOCAL DAEMON"
    endpoint = urlsplit(str(url)).netloc or str(url)
    return f"REMOTE · {endpoint}"


def _remote_submission_error(text: str, attachments, local_daemon: bool) -> str:  # noqa: ANN001
    """Reject operations whose client/server location semantics are ambiguous."""
    if str(text).lstrip().startswith("!"):
        return (
            "Direct shell is disabled in attached mode; use an Agent request or "
            "/processes so execution stays on the daemon workspace."
        )
    if attachments and not local_daemon:
        return (
            "Client-local attachments cannot be sent to a remote URL. Put the file "
            "in the server workspace; remote file upload is not implemented."
        )
    return ""


__all__ = ["attach_main"]
