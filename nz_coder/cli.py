"""Rich terminal REPL for NZ-Coder with streaming support."""

import sys

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from nz_coder import __version__, config
from nz_coder.changes import render_latest_diff, revert_latest
from nz_coder.context import auto_compact
from nz_coder.loop import AgentLoop
from nz_coder.memory import memory_mgr
from nz_coder.prompt import build
from nz_coder.sessions import describe_sessions, load_session, save_session
from nz_coder.skills import skill_loader
from nz_coder.trace import latest_trace, summarize_trace
from nz_coder.tools.todo import render as render_todo
from nz_coder.workspace import status_report

theme = Theme({
    "tool": "bold yellow",
    "info": "bold cyan",
    "error": "bold red",
    "success": "bold green",
})
console = Console(theme=theme)


def print_banner():
    console.print(Panel.fit(
        f"[bold cyan]NZ-Coder[/] v{__version__}\n"
        f"Model: {config.MODEL_ID}\n"
        f"Workspace: {config.WORKDIR}\n"
        f"Mode: {config.PERMISSION_MODE}\n"
        f"Type /help for commands, exit to quit",
        border_style="cyan",
    ))


def on_tool(name: str, output: str):
    console.print(f"  [tool]▶ {name}[/tool]", highlight=False)
    truncated = output[:500]
    if len(output) > 500:
        truncated += f"\n  ... ({len(output)} chars total)"
    for line in truncated.splitlines():
        console.print(f"    {line}", highlight=False)


def on_text(text: str):
    if text.startswith("["):
        console.print(f"[info]{text}[/info]")
    else:
        try:
            console.print(Markdown(text))
        except Exception:
            console.print(text)


class StreamingRenderer:
    """Accumulates streaming tokens and renders them live as Markdown."""

    def __init__(self):
        self._buffer = []
        self._live = None

    def start(self):
        self._buffer = []
        self._live = Live("", console=console, refresh_per_second=8, vertical_overflow="visible")
        self._live.start()

    def on_token(self, token):
        if token is None:
            # End of stream
            self._finish()
            return
        self._buffer.append(token)
        text = "".join(self._buffer)
        try:
            self._live.update(Markdown(text))
        except Exception:
            self._live.update(Text(text))

    def _finish(self):
        if self._live:
            self._live.stop()
            self._live = None
        # Final render with full markdown
        full_text = "".join(self._buffer)
        if full_text.strip():
            try:
                console.print(Markdown(full_text))
            except Exception:
                console.print(full_text)
        self._buffer = []


def handle_command(cmd: str, history: list, agent: AgentLoop) -> bool:
    """Handle slash commands. Returns True if handled."""
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/help":
        console.print(Panel(
            "[bold]/compact[/]  - Compress conversation context\n"
            "[bold]/todo[/]     - Show current task list\n"
            "[bold]/memory[/]   - Show saved memories\n"
            "[bold]/mode[/] <m> - Switch permission mode (default/auto/plan)\n"
            "[bold]/status[/]   - Show workspace, git, trace, and session status\n"
            "[bold]/trace[/]    - Show recent agent trace events\n"
            "[bold]/diff[/]     - Show latest agent-authored file diff\n"
            "[bold]/revert-last[/] - Revert latest agent-authored change set\n"
            "[bold]/save-session[/] - Save this conversation\n"
            "[bold]/sessions[/] - List saved sessions\n"
            "[bold]/resume[/] [id] - Resume a saved session (default: latest)\n"
            "[bold]/clear[/]    - Clear conversation history\n"
            "[bold]/help[/]     - Show this help",
            title="Commands", border_style="cyan",
        ))
        return True

    if command == "/compact":
        if history:
            console.print("[info]Compacting conversation...[/info]")
            from openai import OpenAI
            client = OpenAI(api_key=config.API_KEY, base_url=config.API_BASE_URL)
            history[:] = auto_compact(history, client, config.MODEL_ID, focus=arg or None)
            console.print("[success]Context compacted.[/success]")
        else:
            console.print("[info]Nothing to compact.[/info]")
        return True

    if command == "/todo":
        console.print(render_todo())
        return True

    if command == "/memory":
        console.print(memory_mgr.list_memories())
        return True

    if command == "/status":
        console.print(status_report(agent, history))
        return True

    if command == "/mode":
        if arg in ("default", "auto", "plan"):
            agent.permissions.mode = arg
            console.print(f"[success]Permission mode: {arg}[/success]")
        else:
            console.print("[error]Usage: /mode <default|auto|plan>[/error]")
        return True

    if command == "/trace":
        console.print(summarize_trace(latest_trace()))
        return True

    if command == "/diff":
        console.print(agent.change_tracker.render_diff() if agent.change_tracker else render_latest_diff())
        return True

    if command == "/revert-last":
        result = agent.change_tracker.revert() if agent.change_tracker else revert_latest()
        console.print(result)
        return True

    if command == "/save-session":
        path = save_session(history, mode=agent.permissions.mode, session_id=arg or None)
        console.print(f"[success]Session saved: {path}[/success]")
        return True

    if command == "/sessions":
        console.print(describe_sessions())
        return True

    if command == "/resume":
        payload = load_session(arg or "latest")
        if not payload:
            console.print("[error]No saved session found.[/error]")
            return True
        if payload.get("workspace") and payload["workspace"] != str(config.WORKDIR):
            console.print(f"[error]Session workspace differs: {payload['workspace']}[/error]")
            return True
        history[:] = payload.get("messages", [])
        mode = payload.get("mode")
        if mode in ("default", "auto", "plan"):
            agent.permissions.mode = mode
        console.print(f"[success]Resumed session {payload.get('session_id', 'latest')} ({len(history)} messages).[/success]")
        return True

    if command == "/clear":
        history.clear()
        console.print("[success]Conversation cleared.[/success]")
        return True

    return False


def main():
    if not config.API_KEY:
        console.print("[error]API_KEY not set. Copy .env.example to .env and configure it.[/error]")
        sys.exit(1)

    # Initialize
    memory_mgr.load_all()
    system_prompt = build(
        memory_block=memory_mgr.build_prompt_block(),
        skill_descriptions=skill_loader.descriptions(),
    )
    agent = AgentLoop(system_prompt)
    history = []
    renderer = StreamingRenderer()

    print_banner()

    while True:
        try:
            query = console.input("[bold cyan]nz-coder >> [/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[info]Goodbye![/info]")
            break

        stripped = query.strip()
        if stripped.lower() in ("q", "exit", "quit", ""):
            if stripped.lower() in ("q", "exit", "quit"):
                console.print("[info]Goodbye![/info]")
                break
            continue

        if stripped.startswith("/"):
            if handle_command(stripped, history, agent):
                continue

        history.append({"role": "user", "content": stripped})
        renderer.start()
        agent.run(
            history,
            on_tool=on_tool,
            on_text=on_text,
            on_token=renderer.on_token,
            stream=True,
        )
        save_session(history, mode=agent.permissions.mode, session_id="autosave")
        console.print()


if __name__ == "__main__":
    main()
