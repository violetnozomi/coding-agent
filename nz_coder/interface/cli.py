"""Rich terminal REPL for NZ-Coder with streaming support."""
from __future__ import annotations

import asyncio
import os
import re

import select
import sys
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder import __version__
from nz_coder.foundation import config
from nz_coder.interface.commands import (
    CommandContext,
    build_default_registry,
    default_command_registry,
)
from nz_coder.interface.interactions import bind_terminal_interactions
from nz_coder.interface.preferences import record_recent_model, rich_theme
from nz_coder.interface.run_renderer import TerminalRunRenderer
from nz_coder.interface.terminal_input import TerminalInput
from nz_coder.interface.submission import build_user_submission
from nz_coder.protocol.message_schema import bind_user_context
from nz_coder.state.memory import memory_mgr
from nz_coder.runtime.conversation.prompt import build
from nz_coder.state.sessions import activate_session, create_session_id, save_session
from nz_coder.state.skills import skill_loader

theme = Theme({
    "tool": "bold yellow",
    "info": "bold cyan",
    "error": "bold red",
    "success": "bold green",
})
console = Console(theme=theme)


class _SurfaceConsole:
    """Project Rich renderables into the persistent prompt_toolkit screen."""

    def __init__(self, base: Console, surface) -> None:  # noqa: ANN001
        self._base = base
        self.surface = surface

    @property
    def is_terminal(self) -> bool:
        if self.surface is None:
            return bool(self._base.is_terminal)
        return True

    def print(self, *objects, **kwargs) -> None:  # noqa: ANN002, ANN003
        if self.surface is None:
            self._base.print(*objects, **kwargs)
            return
        from nz_coder.interface.fullscreen import render_rich_output

        self.surface.append_output(
            render_rich_output(*objects, width=self.width, **kwargs)
        )

    def print_terminal(self, *objects, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Project a run terminal result into the durable idle notice area."""
        if self.surface is None:
            self._base.print(*objects, **kwargs)
            return
        from nz_coder.interface.fullscreen import render_rich_output

        self.surface.append_notice(
            render_rich_output(*objects, width=self.width, **kwargs)
        )

    def disable_surface(self) -> None:
        """Route future output to Rich after the full-screen error boundary fails."""
        self.surface = None

    def __getattr__(self, name: str):
        return getattr(self._base, name)

# 流式预览的最小刷新间隔（秒）。每个 token 都 refresh 既浪费 CPU 也会在
# 终端高度不足时放大重复打印问题，节流到 ~12fps 肉眼无感知差异。
_LIVE_REFRESH_INTERVAL = 0.08
_STREAM_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_STREAM_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe_stream_text(value: object) -> str:
    """Remove terminal control bytes while preserving Markdown/newlines."""
    return _STREAM_CONTROL.sub("", _STREAM_ANSI_ESCAPE.sub("", str(value or "")))


def print_banner(session_id: str = "", output_console=None):  # noqa: ANN001
    from nz_coder.providers.models import active_model_selection

    selection = active_model_selection()
    banner = Text()
    banner.append("NZ-Coder", style="bold cyan")
    banner.append(f" v{__version__}\n")
    banner.append(f"Model: {selection.provider}/{selection.model_id}\n")
    banner.append(f"Workspace: {current_workdir()}\n")
    banner.append(f"Mode: {config.PERMISSION_MODE}\n")
    if session_id:
        banner.append(f"Session: {session_id}\n")
    banner.append("Type /help for commands · /keys for shortcuts · exit to quit")
    (output_console or console).print(Panel.fit(
        banner,
        border_style="cyan",
    ))


def on_tool(name: str, output: str):
    heading = Text("  ▶ ", style="tool")
    heading.append(str(name))
    console.print(heading, highlight=False)
    truncated = output[:500]
    if len(output) > 500:
        truncated += f"\n  ... ({len(output)} chars total)"
    for line in truncated.splitlines():
        console.print(Text(f"    {_safe_stream_text(line)}"), highlight=False)


def on_text(text: str, output_console=None):  # noqa: ANN001
    target = output_console or console
    text = _safe_stream_text(text)
    if text.startswith("["):
        target.print(Text(text, style="info"))
    else:
        try:
            target.print(Markdown(text))
        except Exception:
            target.print(text)


def _stdin_ready(stream, timeout: float) -> bool:
    """Return whether stdin has immediately available pasted data."""
    try:
        readable, _, _ = select.select([stream], [], [], timeout)
    except (OSError, TypeError, ValueError):
        return False
    return bool(readable)


def _drain_pasted_lines(stdin=None, is_ready=None, max_lines: int = 200, max_chars: int = 20000) -> list[str]:
    """Collect extra lines from a multi-line paste after the first input line."""
    stream = stdin or sys.stdin
    if stdin is None and hasattr(stream, "isatty") and not stream.isatty():
        return []
    ready = is_ready or (lambda timeout: _stdin_ready(stream, timeout))
    lines: list[str] = []
    total_chars = 0
    timeout = 0.03
    for _ in range(max_lines):
        if not ready(timeout):
            break
        line = stream.readline()
        if line == "":
            break
        line = line.rstrip("\r\n")
        lines.append(line)
        total_chars += len(line)
        if total_chars >= max_chars:
            break
        timeout = 0.0
    return lines


def read_user_query(prompt: str = "[bold cyan]nz-coder >> [/bold cyan]") -> str:
    first_line = console.input(prompt)
    extra_lines = _drain_pasted_lines()
    if extra_lines:
        return "\n".join([first_line, *extra_lines])
    return first_line


class StreamingRenderer:
    """Accumulates streaming tokens; shows a live *tail window*, renders full
    Markdown exactly once at the end.

    修复的核心问题（对应运行日志里的大段重复输出）：

    1. 重复打印几十遍同一段文字 —— 旧实现把**完整缓冲区**塞进 Live，并设
       ``vertical_overflow="visible"``、且每个 token 都 ``refresh``。一旦内容
       高度超过终端高度（或输出被重定向到非 TTY），Rich 无法把光标移回
       已滚出屏幕的旧帧去擦除，于是每次刷新都把整段内容重新追加打印一遍。
       现在 Live 只显示缓冲区**最后 N 行**（N 严格小于终端高度），任何一帧
       都能被完整擦除；并以 ``vertical_overflow="crop"`` + ``transient=True``
       兜底，保证中间帧永远不会残留在滚动历史里。

    2. 完整回答被打印两遍 —— 旧 ``_finish()`` 里最终 Markdown 渲染被
       复制粘贴了两次。现在只渲染一次，且 ``finish()`` 幂等，可放进
       ``finally`` 兜底调用。

    3. 暂停期间丢 token —— 旧实现在 ``pause()`` 期间直接丢弃到达的 token，
       最终渲染会缺字。现在 token 始终写入缓冲，暂停只影响画面刷新。

    4. resume 后擦掉暂停期间打印的内容 —— rich 的 ``Live`` 在 ``stop()`` 后
       内部仍记着上一帧的高度（``LiveRender._shape`` 不会被 ``start()``
       重置），复用同一个对象恢复时，第一次刷新会按"旧帧还在屏幕上"
       向上擦行，把 pause 期间打印的工具输出抹掉。现在 ``pause()`` 直接
       丢弃 Live 对象，``resume()`` 重建一个全新的（只用公开 API）。

    5. Ctrl-C / 异常后 Live 悬挂 —— 旧实现只有流正常走到 ``on_token(None)``
       才会关闭 Live。现在主循环在 ``finally`` 中调用幂等的 ``finish()``。

    6. 非 TTY（管道/重定向）时逐帧追加 —— 现在直接跳过 Live，只在结束时
       打印一次完整内容。

    Supports pause() / resume() so callers (e.g. PermissionManager) can safely
    perform interactive input() without corrupting the Live display.
    """

    def __init__(self, live_console: Console = None):
        self.console = live_console or console
        self._buffer: list[str] = []
        self._live: Live | None = None
        self._pause_depth = 0
        self._last_refresh = 0.0
        self._active = False   # start() 与 finish() 之间为 True
        self._status_lines: tuple[str, ...] = ()

    # ── 生命周期 ─────────────────────────────────────────────────────────

    def start(self):
        self.finish()  # 防御：上一轮异常退出时若 Live 未关闭，先收尾
        self._buffer = []
        self._pause_depth = 0
        self._last_refresh = 0.0
        self._active = True
        surface = getattr(self.console, "surface", None)
        if surface is not None:
            surface.begin_run()
            return
        if not self.console.is_terminal:
            return  # 非 TTY：不做实时渲染，finish() 时统一打印一次
        self._live = self._new_live()
        self._live.start()

    def on_token(self, token):
        if token is None:
            # End of stream
            self.finish()
            return
        value = _safe_stream_text(token)
        if not value:
            return
        self._buffer.append(value)  # 关键：暂停期间也要入缓冲，否则最终渲染缺字
        surface = getattr(self.console, "surface", None)
        if surface is not None:
            surface.set_stream("".join(self._buffer))
            return
        if self._pause_depth:
            return
        if time.monotonic() - self._last_refresh >= _LIVE_REFRESH_INTERVAL:
            self._refresh()

    @property
    def text(self) -> str:
        """Return the current projection without exposing the mutable buffer."""
        return "".join(self._buffer)

    def replace_text(self, value: str) -> None:
        """Replace the transient projection from authoritative Message/Part state."""
        text = _safe_stream_text(value)
        if text == self.text:
            return
        self._buffer = [text] if text else []
        surface = getattr(self.console, "surface", None)
        if surface is not None:
            surface.set_stream(text)
            return
        if not self._pause_depth:
            self._refresh()

    def set_status(self, lines) -> None:  # noqa: ANN001
        """Update transient Agent/tool status without polluting final Markdown."""
        if lines is None:
            normalized = ()
        elif isinstance(lines, str):
            normalized = (lines,) if lines.strip() else ()
        else:
            normalized = tuple(str(line) for line in lines if str(line).strip())[:4]
        if normalized == self._status_lines:
            return
        self._status_lines = normalized
        surface = getattr(self.console, "surface", None)
        if surface is not None:
            surface.set_run_status(normalized)
            return
        if not self._pause_depth:
            self._refresh()

    def pause(self):
        """Pause the live display — call before interactive input() or any
        direct console.print().

        注意必须**丢弃** Live 对象而不是留着复用：rich 在 stop() 后仍保留
        上一帧形状，复用恢复时第一次刷新会向上多擦行，抹掉 pause 期间
        打印的内容（详见类 docstring 第 4 点）。transient=True 保证 stop()
        把预览帧从屏幕上整体擦除。
        """
        if getattr(self.console, "surface", None) is not None:
            return
        self._pause_depth += 1
        if self._pause_depth == 1:
            self._drop_live()

    def resume(self):
        """Resume the live display after a pause（重建全新的 Live）。"""
        if getattr(self.console, "surface", None) is not None:
            return
        if self._pause_depth == 0:
            return
        self._pause_depth -= 1
        if self._pause_depth == 0 and self._active and self.console.is_terminal:
            self._live = self._new_live()
            self._live.start()
            self._refresh()

    def finish(self):
        """结束流式输出：擦除 Live 预览，把完整内容用 Markdown 渲染**一次**。

        幂等：重复调用（如 finally 兜底）不会产生任何额外输出。
        """
        full_text = "".join(self._buffer)
        self._drop_live()
        self._buffer = []
        self._status_lines = ()
        self._pause_depth = 0
        self._active = False
        surface = getattr(self.console, "surface", None)
        if surface is not None:
            surface.end_run()
            return
        if full_text.strip():
            try:
                self.console.print(Markdown(full_text))
            except Exception:
                try:
                    self.console.print(Text(full_text))
                except Exception:
                    pass

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _new_live(self) -> Live:
        return Live(
            Text(""),
            console=self.console,
            auto_refresh=False,
            screen=False,
            transient=True,
            vertical_overflow="crop",
        )

    def _drop_live(self):
        live, self._live = self._live, None
        if live is not None:
            try:
                if live.is_started:
                    live.stop()
            except Exception:
                pass

    def _refresh(self):
        if self._live is None or not self._live.is_started:
            return
        self._last_refresh = time.monotonic()
        self._live.update(self._tail_view(), refresh=True)

    def _tail_view(self) -> Text:
        """缓冲区尾部窗口：显示行数严格小于终端高度。

        这是不重复打印的关键不变量——只要 Live 的每一帧都矮于终端，Rich
        就总能把上一帧完整擦除。单行用 no_wrap+ellipsis 截断（1 逻辑行 =
        1 屏幕行，CJK 宽度由 Rich 处理），所以行数即高度。被省略的部分
        会在流结束后的完整 Markdown 渲染中出现。
        """
        height = self.console.size.height or 24
        status_rows = len(self._status_lines)
        max_rows = max(3, height - 6 - status_rows)
        lines = "".join(self._buffer).split("\n")
        hidden = max(0, len(lines) - max_rows)
        view = Text(no_wrap=True, overflow="ellipsis")
        for index, status in enumerate(self._status_lines):
            view.append(status, style="bold cyan" if index == 0 else "dim")
            view.append("\n")
        if hidden:
            view.append(f"… 已省略前 {hidden} 行（结束后完整渲染）\n", style="dim")
        view.append("\n".join(lines[-max_rows:]))
        return view


def _build_agent(system_prompt: str, renderer: StreamingRenderer, session_id: str,
                 permission_mode: str = None):
    from nz_coder.providers.configuration import provider_connection
    from nz_coder.providers.models import active_model_selection
    from nz_coder.runtime.execution.composition import build_product_environment

    connection = provider_connection(active_model_selection().provider)
    return build_product_environment(
        system_prompt,
        renderer=renderer,
        permission_mode=permission_mode,
        session_id=session_id,
        client=None if connection.configured else _UnavailableModelClient(connection.provider),
        auto_mode_classifier_enabled=config.AUTO_MODE_CLASSIFIER_ENABLED,
    )


class _UnavailableModelClient:
    """Allow the terminal to start so /connect can repair missing credentials."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def __getattr__(self, _name: str):
        raise RuntimeError(
            f"Provider '{self.provider}' is not connected; run /connect before sending a request"
        )


def handle_command(cmd: str, history: list, session_state: dict,
                   system_prompt: str, renderer: StreamingRenderer,
                   registry=None) -> bool:  # noqa: ANN001
    """Handle slash commands. Returns True if handled."""
    context = CommandContext(
        history=history,
        session_state=session_state,
        system_prompt=system_prompt,
        renderer=renderer,
        console=console,
        build_agent=_build_agent,
    )
    return (registry or default_command_registry).dispatch(cmd, context)


async def handle_command_async(
    cmd: str,
    history: list,
    session_state: dict,
    system_prompt: str,
    renderer: StreamingRenderer,
    terminal_input: TerminalInput,
    registry=None,  # noqa: ANN001
) -> bool:
    """Handle sync or interactive async commands inside the CLI event loop."""
    def build_interactive_agent(*args, **kwargs):  # noqa: ANN002, ANN003
        agent = _build_agent(*args, **kwargs)
        _attach_terminal_interactions(agent, terminal_input, renderer)
        return agent

    context = CommandContext(
        history=history,
        session_state=session_state,
        system_prompt=system_prompt,
        renderer=renderer,
        console=getattr(terminal_input, "console", console),
        build_agent=build_interactive_agent,
        terminal_input=terminal_input,
    )
    return await (registry or default_command_registry).dispatch_async(cmd, context)


async def _run_cli_impl(owner_state: list[dict]) -> None:
    from nz_coder.providers.configuration import provider_connection
    from nz_coder.providers.models import active_model_selection

    selection = active_model_selection()
    record_recent_model(f"{selection.provider}/{selection.model_id}")
    # Rich and prompt_toolkit consume the same persisted semantic theme.
    from nz_coder.interface.preferences import load_terminal_preferences
    push_theme = getattr(console, "push_theme", None)
    if callable(push_theme):
        push_theme(rich_theme(load_terminal_preferences().theme))
    connection = provider_connection(selection.provider)
    credential_warning = ""
    if not connection.configured:
        credential_warning = (
            f"[error]Credential missing for provider '{selection.provider}'. "
            f"Use /connect or set {connection.credential_name} in the shell/user config; "
            "Agent requests will fail until a provider is connected.[/error]"
        )

    memory_mgr.load_all()
    system_prompt = build(
        memory_block="",
        skill_descriptions=skill_loader.descriptions(),
    )
    renderer = StreamingRenderer()
    initial_session_id = activate_session(create_session_id())
    initial_environment = _build_agent(system_prompt, renderer, initial_session_id)
    from nz_coder.interface.session_controller import TerminalSessionController
    session_state = {
        "id": initial_session_id,
        "session_title": "",
        "agent": initial_environment,
        "controller": TerminalSessionController(initial_environment),
        "provider_configured": connection.configured,
    }
    owner_state.append(session_state)
    history = []
    from nz_coder.interface.custom_commands import (
        default_command_catalog,
        register_command_completion,
    )

    command_catalog = default_command_catalog(current_workdir())
    command_registry = build_default_registry()
    register_command_completion(command_registry, command_catalog)
    input_ui = TerminalInput(
        console=console,
        registry=command_registry,
        workspace=current_workdir(),
        state_provider=lambda: _terminal_status(session_state, history),
        transcript_provider=lambda: _terminal_transcript(
            session_state,
            history,
            tool_details=input_ui.preferences.tool_details,
        ),
        sidebar_provider=lambda: _terminal_sidebar(session_state, history),
        fallback_reader=read_user_query,
    )
    active_console = (
        _SurfaceConsole(console, input_ui.fullscreen)
        if input_ui.fullscreen is not None
        else console
    )
    input_ui.console = active_console
    renderer.console = active_console
    session_state["terminal_input"] = input_ui
    if credential_warning:
        active_console.print(credential_warning)
    _attach_terminal_interactions(session_state["agent"], input_ui, renderer)
    run_view = TerminalRunRenderer(
        active_console,
        renderer,
        detail_provider=lambda: input_ui.preferences.tool_details,
    )

    def _on_tool(name, output):
        run_view.on_tool(name, output)

    def _on_text(text):
        renderer.pause()
        try:
            on_text(text, active_console)
        finally:
            renderer.resume()

    if input_ui.fullscreen is None:
        print_banner(initial_session_id, active_console)

    last_empty_ctrl_c: float | None = None
    while True:
        try:
            query = await input_ui.read_async(
                open_editor=bool(session_state.pop("open_editor", False)),
            )
        except EOFError:
            active_console.print("\n[info]Goodbye![/info]")
            break
        except KeyboardInterrupt:
            # prompt-toolkit handles this gesture itself. Preserve the same
            # contract for fallback readers that surface KeyboardInterrupt.
            now = time.monotonic()
            if last_empty_ctrl_c is not None and now - last_empty_ctrl_c <= 1.0:
                active_console.print("\n[info]Goodbye![/info]")
                break
            last_empty_ctrl_c = now
            active_console.print("[info]Press Ctrl+C again to exit.[/info]")
            continue

        last_empty_ctrl_c = None
        stripped = query.strip()
        if stripped.lower() in ("q", "exit", "quit", ""):
            if stripped.lower() in ("q", "exit", "quit"):
                active_console.print("[info]Goodbye![/info]")
                break
            continue

        if stripped.startswith("!"):
            from nz_coder.interface.direct_shell import execute_direct_shell

            command = stripped[1:].strip()
            try:
                result = await asyncio.to_thread(
                    execute_direct_shell,
                    command,
                    permissions=session_state["agent"].permissions,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                _consume_current_task_cancellation()
                active_console.print("[info]Command cancelled.[/info]")
                continue
            except Exception as exc:
                active_console.print(
                    f"[error]Command failed: {type(exc).__name__}: {exc}[/error]"
                )
                continue
            active_console.print(f"$ {command}", markup=False, style="bold cyan")
            active_console.print(result.output, markup=False)
            input_ui.refresh_view()
            continue

        if stripped.startswith("/"):
            registered = command_registry.get(stripped.split(maxsplit=1)[0])
            expanded_command = (
                command_catalog.expand_invocation(stripped)
                if registered is not None and registered.category == "Custom"
                else None
            )
            if expanded_command is not None:
                stripped = expanded_command.prompt
                command_allowed_tools = expanded_command.allowed_tools or None
                command_model = expanded_command.model
            else:
                command_allowed_tools = None
                command_model = None
        else:
            command_allowed_tools = None
            command_model = None

        if stripped.startswith("/"):
            try:
                handled = await handle_command_async(
                    stripped,
                    history,
                    session_state,
                    system_prompt,
                    renderer,
                    input_ui,
                    command_registry,
                )
            except asyncio.CancelledError:
                _consume_current_task_cancellation()
                active_console.print("[info]Command cancelled.[/info]")
                continue
            except KeyboardInterrupt:
                active_console.print("[info]Command cancelled.[/info]")
                continue
            except Exception as exc:
                active_console.print(
                    f"[error]Command failed: {type(exc).__name__}: {exc}[/error]"
                )
                continue
            if not handled:
                active_console.print(
                    f"[error]Unknown command: {stripped.split()[0]} — type /help for commands[/error]"
                )
            input_ui.refresh_view()
            if session_state.pop("exit_requested", False):
                active_console.print("[info]Goodbye![/info]")
                break
            continue

        agent = session_state["agent"]
        controller = session_state["controller"]
        submission, attachments = input_ui.prepare_submission(stripped)
        _render_submission_metadata(
            stripped, attachments, input_ui.preferences.paste_summary, active_console
        )
        user_message = build_user_submission(
            submission,
            attachments,
            workspace=current_workdir(),
            session_id=agent.session_id,
            agent="plan" if agent.permissions.mode == "plan" else "build",
            provider_id=str(getattr(agent, "provider_id", "unknown")),
            model_id=str(getattr(agent, "model_id", "unknown")),
            variant=getattr(agent, "model_variant", None),
            natural_text=stripped,
        )
        history.append(user_message)
        renderer.start()
        run_view.begin(agent)
        watch_stop = asyncio.Event()
        watch_task = asyncio.create_task(run_view.watch(watch_stop))
        result = None
        cancelled = False
        failure: Exception | None = None
        try:
            agent_task = asyncio.create_task(controller.run(
                history,
                on_tool=_on_tool,
                on_text=_on_text,
                # Terminal text is reduced from Session Message/Part events by
                # TerminalRunRenderer. Direct callbacks remain available to SDK
                # callers without double-appending the local CLI projection.
                on_token=None,
                stream=True,
                allowed_tools=command_allowed_tools,
                model=command_model,
            ))
            if input_ui.fullscreen is not None:
                input_ui.fullscreen.set_cancel_run(controller.cancel)
            result = await agent_task
        except asyncio.CancelledError:
            # asyncio.run() translates SIGINT into cancellation of the main
            # task before it raises KeyboardInterrupt at the process boundary.
            # Consume that cancellation here so Ctrl+C cancels only this Agent
            # run and returns to the same interactive REPL.
            _consume_current_task_cancellation()
            cancelled = True
        except KeyboardInterrupt:
            cancelled = True
        except Exception as exc:
            failure = exc
        finally:
            watch_stop.set()
            try:
                await asyncio.shield(watch_task)
            except asyncio.CancelledError:
                _consume_current_task_cancellation()
            if cancelled:
                run_view.cancel()
            elif failure is not None:
                run_view.fail(failure)
            else:
                run_view.finish(result)
            renderer.finish()
            run_view.close()
            input_ui.refresh_files()

        result_status = (
            str(result.get("status") or "") if isinstance(result, dict) else ""
        )
        if (
            not cancelled
            and result_status not in {"cancelled", "interrupted"}
            and getattr(agent, "tool_calls_this_run", 0) >= 3
            and not getattr(agent, "used_save_memory", False)
        ):
            history.append(bind_user_context(
                {
                    "role": "user",
                    "content": (
                        "<reminder>This was a substantial task. If you learned anything worth "
                        "remembering (user preferences, project constraints, pitfalls), "
                        "consider using save_memory.</reminder>"
                    ),
                    "_nz_synthetic": True,
                },
                agent="plan" if agent.permissions.mode == "plan" else "build",
                provider_id=str(getattr(agent, "provider_id", "unknown")),
                model_id=str(getattr(agent, "model_id", "unknown")),
                variant=getattr(agent, "model_variant", None),
            ))
        save_session(history, mode=agent.permissions.mode, session_id=session_state["id"])
        active_console.print()


def _render_submission_metadata(
    text: str, attachments, paste_summary: bool, output_console=None
) -> None:  # noqa: ANN001
    """Render compact cards for large paste and one-shot file attachments."""
    if paste_summary and (len(text) > 800 or text.count("\n") + 1 >= 5):
        lines = text.count("\n") + 1
        (output_console or console).print(Panel(
            f"{lines} lines · {len(text)} characters retained verbatim",
            title="Pasted content",
            border_style="cyan",
            expand=False,
        ))
    if attachments:
        body = "\n".join(f"{item.path} · {item.size} bytes" for item in attachments)
        (output_console or console).print(Panel(
            body,
            title=f"Attached files · {len(attachments)}",
            border_style="cyan",
            expand=False,
        ))


def _terminal_status(session_state: dict, history: list) -> dict[str, str]:
    """Build a cheap, secret-free status snapshot for the inline composer."""
    from nz_coder.state.context import estimate_tokens

    agent = session_state.get("agent")
    provider = getattr(getattr(agent, "provider", None), "name", "-")
    model = str(getattr(agent, "model_id", "-"))
    permissions = getattr(agent, "permissions", None)
    mode = str(getattr(permissions, "mode", "-"))
    capabilities = getattr(agent, "model_capabilities", None)
    context_limit = max(0, int(getattr(capabilities, "context_tokens", 0) or 0))
    used = estimate_tokens(history)
    context = _format_token_count(used)
    if context_limit:
        context += f"/{_format_token_count(context_limit)}"
    tracker = getattr(agent, "change_tracker", None)
    changed_getter = getattr(tracker, "current_changed_paths", None)
    try:
        changed = len(changed_getter()) if callable(changed_getter) else 0
    except (OSError, RuntimeError, ValueError):
        changed = 0
    process_service = getattr(agent, "process_service", None)
    try:
        processes = len(process_service.list(active_only=True)) if process_service else 0
    except (OSError, RuntimeError, ValueError):
        processes = 0
    return {
        "provider": str(provider),
        "model": model,
        "mode": mode,
        "session": str(session_state.get("id", "-")),
        "session_title": str(session_state.get("session_title") or ""),
        "context": context,
        "workspace": str(current_workdir()),
        "location": str(session_state.get("location") or "LOCAL"),
        "run_state": "idle",
        "provider_configured": bool(session_state.get("provider_configured", True)),
        "changed": str(changed),
        "processes": str(processes),
    }


def _terminal_transcript(
    session_state: dict,
    history: list,
    *,
    tool_details: str = "compact",
):  # noqa: ANN202
    """Build the full-screen transcript from the same durable message graph."""
    from nz_coder.interface.timeline import build_transcript_document

    return build_transcript_document(
        str(session_state.get("id") or "session"),
        history,
        title="NZ-Coder Session",
        tool_details=tool_details != "hidden",
        compact_tools=tool_details == "compact",
    )


def _terminal_sidebar(session_state: dict, history: list) -> str:
    """Build a bounded, secret-free sidebar from current Session owners."""
    from nz_coder.state.context import estimate_tokens
    from nz_coder.state.sessions import load_session

    agent = session_state.get("agent")
    session_id = str(session_state.get("id") or "-")
    try:
        payload = load_session(session_id)
    except (OSError, ValueError):
        payload = {}
    title = str(payload.get("title") or "New Session")
    changed = []
    tracker = getattr(agent, "change_tracker", None)
    getter = getattr(tracker, "current_changed_paths", None)
    if callable(getter):
        try:
            changed = [str(path) for path in getter()]
        except (OSError, RuntimeError, ValueError):
            changed = []
    lines = [
        title,
        session_id,
        "",
        f"Workspace\n{current_workdir()}",
        "",
        f"Messages  {len(history)}",
        f"Context   {_format_token_count(estimate_tokens(history))}",
        f"Changed   {len(changed)}",
    ]
    if changed:
        lines.extend(["", "Files", *(f"• {path}" for path in changed[:12])])
    from nz_coder.tools.todo import render as render_todo

    todo = render_todo()
    if todo != "No todos.":
        lines.extend(["", "Todo", *todo.splitlines()[:12]])
    runtime = getattr(agent, "_mcp_runtime", None)
    if runtime is not None:
        try:
            mcp_rows = runtime.status_summary()
        except (OSError, RuntimeError, ValueError):
            mcp_rows = []
        if mcp_rows:
            lines.extend(["", "MCP"])
            lines.extend(
                f"• {item.get('name', '-')}  {item.get('status', 'unknown')}"
                for item in mcp_rows[:8]
            )
    from nz_coder.lsp import client_status_summary

    lsp_rows = client_status_summary(current_workdir())
    if lsp_rows:
        lines.extend(["", "LSP"])
        lines.extend(
            f"• {item['id']}  {item['status']}"
            for item in lsp_rows[:8]
        )
    return "\n".join(lines)


def _attach_terminal_interactions(agent, terminal_input, renderer) -> None:  # noqa: ANN001
    """Bind async terminal askers when the Agent supports interaction injection."""
    set_followup_pending = getattr(agent, "set_followup_pending", None)
    if callable(set_followup_pending):
        set_followup_pending(terminal_input.has_pending_submission)
    if callable(getattr(agent, "set_interaction_askers", None)):
        bridge = bind_terminal_interactions(agent, terminal_input, renderer)
        setattr(agent, "_terminal_interaction_bridge", bridge)


def _format_token_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 10_000:
        return f"{value / 1_000:.1f}k"
    return f"{round(value / 1_000):.0f}k"


def _consume_current_task_cancellation() -> None:
    """Clear handled SIGINT cancellation counts on Python versions that expose them."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    uncancel = getattr(task, "uncancel", None)
    if not callable(cancelling) or not callable(uncancel):
        return
    while cancelling() > 0:
        uncancel()


async def _run_cli() -> None:
    """Run the terminal session and always close its current Agent owner."""
    owner_state: list[dict] = []
    completed = False
    try:
        await _run_cli_impl(owner_state)
        completed = True
    finally:
        if owner_state:
            terminal_input = owner_state[-1].get("terminal_input")
            close_input = getattr(terminal_input, "close_async", None)
            if callable(close_input):
                await close_input()
            controller = owner_state[-1].get("controller")
            close_controller = getattr(controller, "close", None)
            if callable(close_controller):
                close_controller()
    terminal_input = owner_state[-1].get("terminal_input") if owner_state else None
    if completed and getattr(terminal_input, "fullscreen", None):
        # The final message must be printed after leaving the alternate screen.
        console.print("[info]Goodbye![/info]")


def main(argv: list[str] | None = None) -> int:
    """Dispatch the default terminal UI or the optional local HTTP service."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "run":
        from nz_coder.interface.headless import run_main

        return run_main(args[1:])
    if args and args[0] == "completion":
        from nz_coder.interface.completion import completion_main

        return completion_main(args[1:])
    if args and args[0] == "serve":
        from nz_coder.http_service.cli import serve_main

        return serve_main(args[1:])
    if args and args[0] == "daemon":
        from nz_coder.http_service.daemon import daemon_main

        return daemon_main(args[1:])
    if args and args[0] in {"attach", "connect"}:
        from nz_coder.interface.remote import attach_main

        return attach_main(args[1:])
    if args and args[0] == "mcp":
        from nz_coder.mcp.cli import mcp_main

        return mcp_main(args[1:])
    if args and args[0] == "provider-smoke":
        from nz_coder.evaluation.provider_smoke import main as provider_smoke_main

        return provider_smoke_main(args[1:])
    if args and args[0] in {"swebench", "swe-bench"}:
        from nz_coder.swebench.cli import main as swebench_main

        return swebench_main(args[1:])
    if args and args[0] in ("model", "models"):
        from nz_coder.providers.cli import models_main

        return models_main(args[1:])
    if args and args[0] == "memory":
        from nz_coder.state.memory_cli import memory_main

        return memory_main(args[1:])
    if args and args[0] in ("extension", "extensions"):
        from nz_coder.extensions.cli import extensions_main

        return extensions_main(args[1:])
    if args and args[0] == "config":
        from nz_coder.interface.config_cli import config_main

        return config_main(args[1:])
    if args and args[0] == "platform":
        from nz_coder.interface.platform_capabilities import platform_main

        return platform_main(args[1:])
    if args and args[0] == "doctor":
        from nz_coder.interface.setup.doctor import doctor_main

        return doctor_main(args[1:], output_console=console)
    if args and args[0] == "init":
        from nz_coder.interface.setup.initializer import init_main

        return init_main(args[1:])
    if args and args[0] in ("-V", "--version"):
        console.print(f"nz-coder {__version__}", markup=False)
        return 0
    if args and args[0] in ("-h", "--help"):
        console.print(
            "NZ-Coder terminal coding agent\n\n"
            "Usage: nz-coder [COMMAND] [OPTIONS]\n\n"
            "Interactive:\n"
            "  nz-coder                         Start the full-screen coding assistant\n"
            "  nz-coder attach [URL]            Attach to a daemon Session\n\n"
            "Headless:\n"
            "  nz-coder run --prompt TEXT       Run one non-interactive task\n"
            "  nz-coder serve                   Start the loopback HTTP Session service\n"
            "  nz-coder daemon                  Manage the persistent local service\n\n"
            "Configuration:\n"
            "  nz-coder init                    Create workspace configuration\n"
            "  nz-coder doctor                  Check credentials, LSP, MCP, and terminal\n"
            "  nz-coder config show             Show effective configuration\n"
            "  nz-coder models list             List available models\n"
            "  nz-coder models select PROVIDER/MODEL  Select a model\n\n"
            "More: nz-coder mcp --help | swebench --help | extensions --help\n"
            "Inside the terminal use /help, /model, /mode, or Ctrl+K.",
            markup=False,
        )
        return 0
    if args:
        console.print(Text(f"Unknown argument: {args[0]}", style="error"))
        return 2
    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        console.print("[info]Goodbye![/info]")
        return 130
    except Exception as exc:
        console.print(f"NZ-Coder could not start: {exc}", markup=False)
        console.print(
            "[info]Run `nz-coder doctor` for diagnostics. "
            "Set NZ_CODER_DEBUG=1 to expose a traceback.[/info]"
        )
        if os.getenv("NZ_CODER_DEBUG", "").strip().lower() in {"1", "true", "yes"}:
            raise
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
