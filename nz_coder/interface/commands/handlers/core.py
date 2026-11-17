"""Built-in CLI slash commands."""
from __future__ import annotations

import os
import json
import sqlite3
from pathlib import Path
import tempfile
import time

from rich.console import Group
from rich.markup import escape
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.changes import (
    render_latest_diff,
)
from nz_coder.state.memory import current_memory_manager
from nz_coder.protocol.public_error import to_public_error
from nz_coder.providers.models import (
    active_model_selection,
    cached_models,
    clear_model_selection,
    configured_catalog_models,
    discover_models,
    save_model_selection,
)
from nz_coder.providers.registry import registry_models
from nz_coder.providers.connect import (
    provider_connect_spec,
    provider_connect_specs,
    save_provider_connection,
)
from nz_coder.interface.preferences import (
    command_keybinding,
    configurable_keybinding_actions,
    cycle_model_id,
    load_terminal_preferences,
    message_keybindings,
    record_recent_model,
    rich_theme,
    theme_names,
    toggle_favorite_model,
    update_terminal_preferences,
)
from nz_coder.interface.selector import SelectorActionResult
from nz_coder.foundation.async_utils import to_thread_settled
from nz_coder.runtime.session.lifecycle import delete_session
from nz_coder.state.sessions import (
    create_session_id,
    load_session,
    rename_session,
    save_session,
)
from nz_coder.interface.timeline import (
    conversation_turns,
    format_transcript,
    fork_history,
    forked_session_title,
    rebind_fork_history,
    render_sessions,
    render_timeline,
    session_options,
)
from nz_coder.tools.todo import render as render_todo
from nz_coder.state.trace import latest_trace, summarize_trace
from nz_coder.state.workspace import status_report

from ..registry import (
    Command,
    CommandContext,
    CommandRegistry,
    product_command_category,
)
from .permission import format_mode_usage, set_permission_mode


def register_core_commands(registry: CommandRegistry) -> None:
    registry.register(Command("help", "Show available commands", "/help", handle_help,
                              category="General", suggested=True))
    registry.register(Command('lsp','Inspect or manage this host language services',
        '/lsp [status | trust|untrust|restart ENTRY_ID]',handle_lsp,category='Extensions'))
    registry.register(Command("keys", "Show terminal editing shortcuts", "/keys", handle_keys,
                              category="General"))
    registry.register(
        Command(
            "model",
            "Choose or switch the current workspace model",
            "/model [list | reset | PROVIDER/MODEL [VARIANT]]",
            handle_model_command,
            aliases=("models",),
            category="Model",
            keybind="Ctrl+X M",
            suggested=True,
        )
    )
    registry.register(
        Command(
            "compact",
            "Compress conversation context",
            "/compact [focus]",
            handle_compact,
            category="Session",
            keybind="Ctrl+X C",
        )
    )
    registry.register(Command("todo", "Show current task list", "/todo", handle_todo,
                              category="Agent"))
    registry.register(Command(
        "agents",
        "Inspect primary and child Agent definitions",
        "/agents",
        handle_agents,
        category="Agent",
    ))
    registry.register(Command("memory", "Show saved memories", "/memory", handle_memory,
                              category="Agent"))
    registry.register(Command(
        "memory-review",
        "Review pending memory proposals",
        "/memory-review",
        handle_memory_review,
        category="Agent",
    ))
    registry.register(Command(
        "extensions",
        "Inspect extension metadata",
        "/extensions [list|status ID|reload|enable ID|disable ID]",
        handle_extensions,
        category="Agent",
    ))
    registry.register(Command("profile", "Show project profile", "/profile", handle_profile,
                              category="General"))
    registry.register(Command("stats", "Show persisted Session usage and cost", "/stats [days]", handle_stats,
                              category="Session"))
    registry.register(
        Command(
            "status",
            "Show workspace, git, trace, and session status",
            "/status",
            handle_status,
            category="General",
            keybind="Ctrl+X S",
        )
    )
    registry.register(
        Command(
            "mode",
            "Switch permission mode",
            "/mode [default|auto|plan|acceptEdits]",
            handle_mode_command,
            category="Permissions",
            suggested=True,
        )
    )
    registry.register(
        Command(
            "trace",
            "Show recent trace events for the current session",
            "/trace",
            handle_trace,
            category="Agent",
        )
    )
    registry.register(Command("diff", "Show latest agent-authored file diff", "/diff", handle_diff,
                              category="Changes"))
    registry.register(
        Command(
            "undo",
            "Undo the latest agent turn and its file changes",
            "/undo",
            handle_undo,
            aliases=("revert-last",),
            category="Changes",
            keybind="Ctrl+X U",
        )
    )
    registry.register(
        Command(
            "redo",
            "Redo the most recently undone agent turn",
            "/redo",
            handle_redo,
            category="Changes",
            keybind="Ctrl+X R",
        )
    )
    registry.register(
        Command(
            "save-session",
            "Save this conversation",
            "/save-session [id]",
            handle_save_session,
            category="Session",
        )
    )
    registry.register(Command("sessions", "List saved sessions", "/sessions", handle_sessions,
                              category="Session"))
    registry.register(Command(
        "processes",
        "Inspect and control persistent processes",
        "/processes [inspect|logs|follow|kill] [PROCESS_ID]",
        handle_processes,
        aliases=("process",),
        category="Agent",
    ))
    registry.register(
        Command(
            "delete-session",
            "Delete a Session and its owned artifacts",
            "/delete-session [ID]",
            handle_delete_session,
            category="Session",
        )
    )
    registry.register(
        Command(
            "session",
            "Choose and resume a saved session",
            "/session",
            handle_session_picker,
            aliases=("pick-session",),
            category="Session",
            keybind="Ctrl+X L",
        )
    )
    registry.register(
        Command(
            "timeline",
            "Show visible user turns and Agent summaries",
            "/timeline [LIMIT]",
            handle_timeline,
            category="Session",
            keybind="Ctrl+X G",
        )
    )
    registry.register(
        Command(
            "message",
            "Inspect one historical turn with full tool details",
            "/message [TURN]",
            handle_message_detail,
            category="Session",
        )
    )
    for name, description, keybind in (
        ("message-first", "Navigate to the first message", "Home"),
        ("message-last", "Navigate to the last message", "End"),
        ("message-next", "Navigate to the next message", "Ctrl+X J"),
        ("message-previous", "Navigate to the previous message", "Ctrl+X K"),
        ("message-last-user", "Navigate to the last user message", "Ctrl+X H"),
    ):
        registry.register(Command(
            name,
            description,
            f"/{name}",
            handle_message_navigation,
            category="Session",
            keybind=keybind,
        ))
    registry.register(
        Command(
            "subagents",
            "Browse child Agent sessions for this conversation",
            "/subagents [ID]",
            handle_subagents,
            aliases=("children",),
            category="Session",
        )
    )
    registry.register(
        Command(
            "subagent",
            "Continue an owned child Agent in its original workspace",
            "/subagent [ID] [PROMPT]",
            handle_subagent_route,
            category="Session",
        )
    )
    registry.register(
        Command(
            "fork",
            "Fork this conversation through a completed user turn",
            "/fork [TURN]",
            handle_fork,
            aliases=("fork-session",),
            category="Session",
        )
    )
    registry.register(
        Command(
            "fork-picker",
            "Choose a completed turn to fork",
            "/fork-picker",
            handle_fork_picker,
            aliases=("pick-fork",),
            category="Session",
        )
    )
    registry.register(
        Command(
            "model-picker",
            "Choose the current workspace model",
            "/model-picker",
            handle_model_picker,
            aliases=("pick-model",),
            category="Model",
            keybind="Ctrl+X M",
        )
    )
    registry.register(
        Command(
            "new-session",
            "Start a fresh isolated session",
            "/new-session",
            handle_new_session,
            category="Session",
            keybind="Ctrl+X N",
        )
    )
    registry.register(
        Command(
            "resume",
            "Resume a saved session",
            "/resume [id]",
            handle_resume,
            category="Session",
        )
    )
    registry.register(Command("clear", "Start a fresh session; keep the previous one", "/clear", handle_clear,
                              category="Session"))
    registry.register(Command(
        "model-cycle", "Cycle recent or favorite models",
        "/model-cycle [next|previous|favorite-next|favorite-previous]",
        handle_model_cycle, category="Model", keybind="F2",
    ))
    registry.register(Command(
        "model-favorite", "Toggle a model favorite", "/model-favorite [PROVIDER/MODEL]",
        handle_model_favorite, category="Model",
    ))
    registry.register(Command(
        "connect", "Connect a model provider", "/connect",
        handle_connect, category="Model", suggested=True,
    ))
    registry.register(Command(
        "theme", "Choose the terminal color theme", "/theme [NAME]",
        handle_theme, category="Terminal", keybind="Ctrl+X T",
    ))
    registry.register(Command(
        "tool-details", "Set tool card detail level", "/tool-details [hidden|compact|full]",
        handle_tool_details, category="Terminal",
    ))
    registry.register(Command(
        "mouse", "Enable or disable picker mouse support", "/mouse [on|off]",
        handle_mouse, category="Terminal",
    ))
    registry.register(Command(
        "sidebar", "Set full-screen sidebar visibility", "/sidebar [auto|show|hide]",
        handle_sidebar, category="Terminal",
    ))
    registry.register(Command(
        "keybind", "Configure message navigation keys",
        "/keybind [list|reset|ACTION [KEYS|none|default]]",
        handle_keybind, category="Terminal",
    ))
    registry.register(Command(
        "attach", "Attach a workspace file to the next request", "/attach PATH",
        handle_attach, category="Input",
    ))
    registry.register(Command(
        "attachments", "List files attached to the next request", "/attachments",
        handle_attachments, category="Input",
    ))
    registry.register(Command(
        "detach", "Remove queued file attachments", "/detach [PATH|all]",
        handle_detach, category="Input",
    ))
    registry.register(Command(
        "rename", "Rename the current session", "/rename [TITLE]",
        handle_rename, category="Session",
    ))
    registry.register(Command(
        "copy", "Copy this session transcript", "/copy",
        handle_copy, category="Session",
    ))
    registry.register(Command(
        "copy-last", "Copy the last assistant message", "/copy-last",
        handle_copy_last, category="Session", keybind="Ctrl+X Y",
    ))
    registry.register(Command(
        "export", "Export this session transcript as Markdown", "/export [PATH]",
        handle_export, category="Session",
    ))
    registry.register(Command(
        "skills", "List available and conditional skills", "/skills",
        handle_skills, category="Agent",
    ))
    registry.register(Command(
        "mcps", "Show configured MCP server status", "/mcps",
        handle_mcps, aliases=("mcp",), category="Agent",
    ))
    registry.register(Command(
        "variants", "Choose a model reasoning variant", "/variants [VARIANT|default]",
        handle_variants, category="Model",
    ))
    registry.register(Command(
        "editor", "Open the next request in the external editor", "/editor",
        handle_editor, category="Input", keybind="Ctrl+X E",
    ))
    registry.register(Command(
        "exit", "Exit NZ-Coder", "/exit",
        handle_exit, aliases=("quit", "q"), category="General",
    ))


def handle_help(ctx: CommandContext) -> None:
    lines = []
    category = ""
    preferences = load_terminal_preferences()
    commands = ctx.registry.visible_commands() if ctx.registry else ()
    show_all = ctx.args.strip().lower() == "all"
    if ctx.args.strip() and not show_all:
        ctx.console.print("[error]Usage: /help [all][/error]")
        return
    if not show_all:
        commands = tuple(
            command for command in commands
            if product_command_category(command) == "Essentials"
        )
    category_order = {
        name: index for index, name in enumerate((
            "Essentials", "Session", "Model", "Agent", "Files",
            "Processes", "Memory", "Extensions", "Settings",
        ))
    }
    for command in sorted(
        commands,
        key=lambda item: (
            category_order.get(product_command_category(item), 99),
            item.name,
        ),
    ):
        command_category = product_command_category(command)
        if command_category != category:
            category = command_category
            lines.append(f"\n[bold cyan]{category}[/bold cyan]")
        effective = command_keybinding(
            command.name, command.keybind, preferences,
        )
        keybind = f" [dim]({effective})[/dim]" if effective else ""
        lines.append(f"[bold]{command.usage}[/]{keybind} - {command.description}")
    if not show_all:
        lines.append(
            "\n[dim]Use /help all for every command or Ctrl+K to search the palette.[/dim]"
        )
    ctx.console.print(Panel("\n".join(lines), title="Commands", border_style="cyan"))


def handle_keys(ctx: CommandContext) -> None:
    bindings = message_keybindings(load_terminal_preferences())
    navigation = "\n".join(
        f"  {name:<24} {sequence}"
        for name, sequence in bindings.items()
    )
    ctx.console.print(Panel(
        "Enter       Send the current prompt\n"
        "Alt+Enter   Insert a newline\n"
        "Ctrl+K      Open the searchable command palette (Ctrl+P also works)\n"
        "Ctrl+V      Paste text from the terminal/application clipboard\n"
        "F2          Cycle recent models\n"
        "Ctrl+X M/T  Open model/theme picker\n"
        "Ctrl+X Y    Copy the last assistant message\n"
        "Ctrl+X E    Edit the request in $VISUAL or $EDITOR\n"
        "Tab         Open or accept completion\n"
        "Up/Down     Navigate input and saved history\n"
        "Picker      Type to filter · Up/Down move · Enter select · Esc cancel\n"
        "Ctrl+C      Clear input · press twice on empty input to exit\n"
        "Ctrl+C      Cancel the currently running Agent\n"
        "Ctrl+D      Exit when the input is empty\n"
        "/           Complete slash commands\n"
        "@           Complete workspace file references\n"
        "Mouse       Select picker rows when /mouse is on\n\n"
        "Message navigation (workspace configurable):\n"
        f"{navigation}\n"
        "Use /keybind ACTION KEYS; separate key chords with spaces.",
        title="Terminal shortcuts",
        border_style="cyan",
    ))


def handle_model(ctx: CommandContext) -> None:
    parts = ctx.args.split()
    if not parts:
        selection = active_model_selection()
        variant = f" variant={selection.variant}" if selection.variant else ""
        ctx.console.print(
            f"[info]Current model: {selection.provider}/{selection.model_id}{variant}[/info]"
        )
        return
    if parts == ["list"]:
        values = _known_model_ids()
        if not values:
            ctx.console.print("[info]No offline model catalog is available.[/info]")
            return
        active = active_model_selection()
        lines = []
        for value in values:
            marker = "*" if value == f"{active.provider}/{active.model_id}" else " "
            lines.append(f"{marker} {value}")
        ctx.console.print(Panel("\n".join(lines), title="Workspace models", border_style="cyan"))
        return
    ctx.ensure_environment_replaceable()
    previous = active_model_selection()
    previous_agent = ctx.agent
    selection_changed = False
    try:
        if parts == ["reset"]:
            clear_model_selection()
            selection_changed = True
        else:
            if len(parts) > 2 or "/" not in parts[0]:
                raise ValueError("Use /model PROVIDER/MODEL [VARIANT]")
            provider, model_id = parts[0].split("/", 1)
            save_model_selection(
                provider,
                model_id,
                variant=parts[1] if len(parts) == 2 else None,
            )
            selection_changed = True
        selection = active_model_selection()
        new_agent = ctx.build_agent(
            ctx.system_prompt,
            ctx.renderer,
            ctx.session_id,
            ctx.agent.permissions.mode,
            activate_session_on_build=False,
        )
        ctx.replace_agent(ctx.session_id, new_agent)
    except Exception as exc:
        if selection_changed and ctx.agent is previous_agent:
            try:
                if previous.source == "workspace":
                    save_model_selection(
                        previous.provider,
                        previous.model_id,
                        variant=previous.variant,
                    )
                else:
                    clear_model_selection()
            except Exception:
                pass
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return

    record_recent_model(f"{selection.provider}/{selection.model_id}")
    _reload_terminal_preferences(ctx)
    try:
        save_session(
            ctx.history,
            mode=new_agent.permissions.mode,
            session_id=ctx.session_id,
        )
    except OSError as exc:
        ctx.console.print(f"[error]Warning: model switched, but session save failed: {exc}[/error]")
    variant = f" variant={selection.variant}" if selection.variant else ""
    ctx.console.print(
        f"[success]Switched model to {selection.provider}/{selection.model_id}{variant}.[/success]"
    )


def handle_model_command(ctx: CommandContext):
    """Open the model picker by default while preserving explicit subcommands."""
    if not ctx.args.strip():
        return handle_model_picker(ctx)
    return handle_model(ctx)


def _known_model_ids() -> tuple[str, ...]:
    active = active_model_selection()
    values = {f"{active.provider}/{active.model_id}"}
    for loader in (cached_models, configured_catalog_models, registry_models):
        try:
            values.update(f"{item.provider}/{item.model_id}" for item in loader())
        except (OSError, RuntimeError, ValueError):
            continue
    return tuple(sorted(values, key=str.lower))


async def handle_model_picker(ctx: CommandContext) -> None:
    if not _picker_available(ctx):
        return
    active = active_model_selection()
    active_id = f"{active.provider}/{active.model_id}"
    model_ids = _known_model_ids()
    if model_ids == (active_id,):
        ctx.console.print(f"[info]Discovering models from {active.provider}...[/info]")
        try:
            await to_thread_settled(discover_models, active.provider)
            model_ids = _known_model_ids()
        except (OSError, RuntimeError, ValueError) as exc:
            ctx.console.print(
                f"[error]Model discovery failed: {exc}. "
                "Use /model PROVIDER/MODEL when you already know the model id.[/error]"
            )
            return
    while True:
        prefs = load_terminal_preferences()
        favorites = [value for value in prefs.favorite_models if value in model_ids]
        recent = [
            value for value in prefs.recent_models
            if value in model_ids and value not in favorites
        ]
        remainder = [value for value in model_ids if value not in favorites and value not in recent]
        ordered = [
            *(('[Favorites]', value) for value in favorites),
            *(('[Recent]', value) for value in recent),
            *((f"[{value.split('/', 1)[0]}]", value) for value in remainder),
        ]
        values = [
            (
                model_id,
                f"{section:<20} {'●' if model_id == active_id else ' '} "
                f"{'★' if model_id in favorites else ' '} {model_id}",
            )
            for section, model_id in ordered
        ]
        selected = await ctx.terminal_input.select_async(
            title="Choose model",
            values=values,
            text="Ctrl+F favorite · Ctrl+A connect provider · Enter select · Esc cancel",
            actions=(
                ("c-f", "favorite", "Ctrl+F favorite"),
                ("c-a", "connect", "Ctrl+A connect"),
            ),
        )
        if isinstance(selected, SelectorActionResult):
            if selected.action == "connect":
                await handle_connect(ctx)
                return
            if selected.action == "favorite" and selected.value is not None:
                enabled = toggle_favorite_model(str(selected.value))
                _reload_terminal_preferences(ctx)
                state = "Added to" if enabled else "Removed from"
                ctx.console.print(f"[success]{state} favorites: {selected.value}[/success]")
                continue
        if selected is not None and str(selected) != active_id:
            handle_model(_context_with_args(ctx, str(selected)))
        elif selected is not None:
            record_recent_model(active_id)
            _reload_terminal_preferences(ctx)
        return


def handle_model_cycle(ctx: CommandContext) -> None:
    action = ctx.args.strip().lower() or "next"
    actions = {
        "next": (False, False),
        "previous": (False, True),
        "favorite-next": (True, False),
        "favorite-previous": (True, True),
    }
    if action not in actions:
        ctx.console.print(
            "[error]Usage: /model-cycle [next|previous|favorite-next|favorite-previous][/error]"
        )
        return
    active = active_model_selection()
    target = cycle_model_id(
        favorites=actions[action][0],
        reverse=actions[action][1],
        current=f"{active.provider}/{active.model_id}",
    )
    if target is None:
        kind = "favorite" if actions[action][0] else "recent"
        ctx.console.print(f"[info]No {kind} models are available yet.[/info]")
        return
    if target == f"{active.provider}/{active.model_id}":
        ctx.console.print(f"[info]Model remains {target}.[/info]")
        return
    handle_model(_context_with_args(ctx, target))


def handle_model_favorite(ctx: CommandContext) -> None:
    target = ctx.args.strip()
    if not target:
        active = active_model_selection()
        target = f"{active.provider}/{active.model_id}"
    try:
        enabled = toggle_favorite_model(target)
    except ValueError as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    _reload_terminal_preferences(ctx)
    state = "Added to" if enabled else "Removed from"
    ctx.console.print(f"[success]{state} favorites: {target}[/success]")


async def handle_connect(ctx: CommandContext) -> None:
    """Run a masked credential flow and offer models discovered from the provider."""
    ctx.ensure_environment_replaceable()
    if not _picker_available(ctx):
        return
    specs = provider_connect_specs()
    selected = await ctx.terminal_input.select_async(
        title="Connect provider",
        values=[(spec.provider, f"{spec.label:<22} {spec.credential_name}") for spec in specs],
        text="Credentials are written only to the user-private NZ-Coder config.",
    )
    if selected is None:
        return
    spec = provider_connect_spec(str(selected))
    api_key = await ctx.terminal_input.prompt_text_async(
        f"{spec.credential_name}: ", password=True,
    )
    if api_key is None:
        return
    endpoint = await ctx.terminal_input.prompt_text_async(
        f"{spec.endpoint_name}: ", default=spec.default_endpoint,
    )
    if endpoint is None:
        return
    ctx.ensure_environment_replaceable()
    try:
        save_provider_connection(spec.provider, api_key, endpoint)
    except (OSError, ValueError) as exc:
        ctx.console.print(f"[error]Connection was not saved: {exc}[/error]")
        return
    ctx.console.print(
        f"[success]Connected {spec.label}; credential saved in the user-private config.[/success]"
    )
    ctx.console.print(f"[info]Discovering models from {spec.label}...[/info]")
    try:
        discovered = await to_thread_settled(
            discover_models,
            spec.provider,
            api_key=api_key,
            base_url=endpoint,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        ctx.console.print(
            f"[error]Credential is saved, but model discovery failed: {exc}. "
            "Use /model PROVIDER/MODEL if the endpoint has no model-list API.[/error]"
        )
        return
    model = await ctx.terminal_input.select_async(
        title=f"Models · {spec.label}",
        values=[
            (f"{item.provider}/{item.model_id}", item.display_name or item.model_id)
            for item in discovered
        ],
        text="Choose a model to activate this connection now.",
    )
    if model is not None:
        handle_model(_context_with_args(ctx, str(model)))


async def handle_theme(ctx: CommandContext) -> None:
    selected = ctx.args.strip().lower()
    if not selected:
        if not _picker_available(ctx):
            return
        current = load_terminal_preferences().theme
        result = await ctx.terminal_input.select_async(
            title="Terminal theme",
            values=[
                (name, f"{'●' if name == current else ' '}  {name}")
                for name in theme_names()
            ],
            text="Choose a workspace terminal theme · Esc keeps the current theme.",
        )
        if result is None:
            return
        selected = str(result)
    try:
        preferences = update_terminal_preferences(theme=selected)
    except (OSError, ValueError) as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    push_theme = getattr(ctx.console, "push_theme", None)
    if callable(push_theme):
        push_theme(rich_theme(preferences.theme))
    _reload_terminal_preferences(ctx)
    ctx.console.print(f"[success]Terminal theme: {preferences.theme}[/success]")


def handle_tool_details(ctx: CommandContext) -> None:
    selected = ctx.args.strip().lower()
    current = load_terminal_preferences().tool_details
    values = ("compact", "normal", "detailed")
    legacy = {"hidden": "compact", "full": "detailed"}
    current = legacy.get(current, current)
    if not selected:
        selected = values[(values.index(current) + 1) % len(values)]
    if selected not in {*values, "hidden", "full"}:
        ctx.console.print("[error]Usage: /tool-details [compact|normal|detailed][/error]")
        return
    preferences = update_terminal_preferences(tool_details=selected)
    _reload_terminal_preferences(ctx)
    ctx.console.print(f"[success]Tool details: {preferences.tool_details}[/success]")


def handle_mouse(ctx: CommandContext) -> None:
    selected = ctx.args.strip().lower()
    current = load_terminal_preferences().mouse
    if not selected:
        selected = "off" if current else "on"
    if selected not in {"on", "off"}:
        ctx.console.print("[error]Usage: /mouse [on|off][/error]")
        return
    preferences = update_terminal_preferences(mouse=selected == "on")
    _reload_terminal_preferences(ctx)
    state = "on" if preferences.mouse else "off"
    ctx.console.print(f"[success]Picker mouse support: {state}[/success]")


def handle_sidebar(ctx: CommandContext) -> None:
    selected = ctx.args.strip().lower() or "auto"
    if selected not in {"auto", "show", "hide"}:
        ctx.console.print("[error]Usage: /sidebar [auto|show|hide][/error]")
        return
    preferences = update_terminal_preferences(sidebar=selected)
    _reload_terminal_preferences(ctx)
    ctx.console.print(f"[success]Full-screen sidebar: {preferences.sidebar}[/success]")


def handle_keybind(ctx: CommandContext) -> None:
    """Inspect or update workspace-owned message navigation bindings."""
    parts = ctx.args.strip().lower().split()
    current = load_terminal_preferences()
    if not parts or parts == ["list"]:
        effective = message_keybindings(current)
        table = Table(title="Terminal message keybindings", show_lines=False)
        table.add_column("Action", style="cyan")
        table.add_column("Keys")
        table.add_column("Source", style="dim")
        overrides = dict(current.keybindings)
        for action in configurable_keybinding_actions():
            table.add_row(
                action,
                effective[action],
                "workspace" if action in overrides else "default",
            )
        ctx.console.print(table)
        return
    if parts == ["reset"]:
        update_terminal_preferences(keybindings={})
        _reload_terminal_preferences(ctx)
        ctx.console.print("[success]Terminal message keybindings reset.[/success]")
        return
    action = parts[0]
    if action not in configurable_keybinding_actions():
        ctx.console.print(
            "[error]Unknown keybinding action. Use /keybind list.[/error]"
        )
        return
    if len(parts) == 1:
        value = message_keybindings(current)[action]
        ctx.console.print(f"[info]{action}: {value}[/info]")
        return
    value = " ".join(parts[1:])
    overrides = dict(current.keybindings)
    if value == "default":
        overrides.pop(action, None)
    else:
        overrides[action] = value
    try:
        preferences = update_terminal_preferences(keybindings=overrides)
    except (OSError, ValueError) as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    _reload_terminal_preferences(ctx)
    effective = message_keybindings(preferences)[action]
    ctx.console.print(f"[success]{action}: {effective}[/success]")


def handle_attach(ctx: CommandContext) -> None:
    if ctx.terminal_input is None:
        ctx.console.print("[error]Attachments require the terminal input surface.[/error]")
        return
    try:
        attachment = ctx.terminal_input.queue_attachment(ctx.args)
    except (OSError, ValueError) as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    ctx.console.print(
        f"[success]Attached for next request: {attachment.path} ({attachment.size} bytes).[/success]"
    )


def handle_attachments(ctx: CommandContext) -> None:
    attachments = ctx.terminal_input.attachments() if ctx.terminal_input is not None else ()
    if not attachments:
        ctx.console.print("[info]No files are attached to the next request.[/info]")
        return
    lines = "\n".join(f"- {item.path} ({item.size} bytes)" for item in attachments)
    ctx.console.print(Panel(lines, title="Next-request attachments", border_style="cyan"))


def handle_detach(ctx: CommandContext) -> None:
    if ctx.terminal_input is None:
        ctx.console.print("[error]Attachments require the terminal input surface.[/error]")
        return
    removed = ctx.terminal_input.remove_attachment(ctx.args)
    if removed:
        ctx.console.print(f"[success]Removed {removed} attachment(s).[/success]")
    else:
        ctx.console.print("[info]No matching attachment was queued.[/info]")


async def handle_rename(ctx: CommandContext) -> None:
    title = ctx.args.strip()
    if not title and ctx.terminal_input is not None:
        title = (
            await ctx.terminal_input.prompt_text_async(
                "Session title: ",
                default=str(load_session(ctx.session_id).get("title") or ""),
            )
            or ""
        ).strip()
    if not title:
        ctx.console.print("[error]Usage: /rename TITLE[/error]")
        return
    try:
        renamed = rename_session(ctx.session_id, title)
    except (OSError, ValueError) as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    if "session_title" in ctx.session_state:
        ctx.session_state["session_title"] = renamed
    ctx.console.print(f"[success]Session renamed: {renamed}[/success]")


def handle_copy(ctx: CommandContext) -> None:
    from nz_coder.interface.clipboard import copy_text

    transcript = _current_transcript(ctx)
    try:
        copied = copy_text(transcript)
    except ValueError as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    if copied:
        ctx.console.print("[success]Session transcript copied to clipboard.[/success]")
    else:
        ctx.console.print(
            "[error]No terminal or native clipboard transport is available; "
            "use /export instead.[/error]"
        )


def handle_copy_last(ctx: CommandContext) -> None:
    """Copy only the latest Assistant text, matching InfCode messages_copy."""
    from nz_coder.interface.clipboard import copy_text
    from nz_coder.interface.timeline import latest_assistant_text

    text = latest_assistant_text(ctx.history)
    if not text:
        ctx.console.print("[error]The last assistant message has no text to copy.[/error]")
        return
    try:
        copied = copy_text(text)
    except ValueError as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    if copied:
        ctx.console.print("[success]Last assistant message copied.[/success]")
    else:
        ctx.console.print(
            "[error]No terminal or native clipboard transport is available.[/error]"
        )


def handle_export(ctx: CommandContext) -> None:
    raw = ctx.args.strip() or f"session-{ctx.session_id[:24]}.md"
    try:
        target = _safe_export_path(raw)
        _write_text_atomic(target, _current_transcript(ctx))
    except (OSError, ValueError) as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return
    ctx.console.print(f"[success]Session exported: {target}[/success]")


def handle_skills(ctx: CommandContext) -> None:
    items = (
        ctx.controller.skills()
        if ctx.controller is not None
        else getattr(ctx.agent, "_skill_loader", None).list_skills()
        if getattr(ctx.agent, "_skill_loader", None) is not None
        else []
    )
    if not items:
        ctx.console.print("[info]No skills are available.[/info]")
        return
    lines = [
        f"{item['name']}  [{item['status']}, {item['source']}]  {item['description']}"
        for item in items
    ]
    ctx.console.print(Panel(escape("\n".join(lines)), title="Skills", border_style="cyan"))


def handle_mcps(ctx: CommandContext) -> None:
    from nz_coder.mcp.config import load_mcp_server_configs

    configs = load_mcp_server_configs(workspace=current_workdir())
    statuses = {
        item["name"]: item
        for item in (
            ctx.controller.mcp_status()
            if ctx.controller is not None
            else getattr(ctx.agent, "_mcp_runtime", None).status_summary()
            if getattr(ctx.agent, "_mcp_runtime", None) is not None
            else []
        )
    }
    if not configs and not statuses:
        ctx.console.print("[info]No MCP servers are configured.[/info]")
        return
    lines = []
    for server in configs:
        status = statuses.get(server.name, {})
        state = status.get("status") or ("disabled" if not server.enabled else "not_started")
        tools = int(status.get("tool_count") or 0)
        trust = "trusted" if server.trusted else "untrusted"
        error = f" — {status['error']}" if status.get("error") else ""
        lines.append(
            f"{server.name}  [{state}]  {server.transport} · {server.source} · "
            f"{trust} · {tools} tool(s){error}"
        )
    for name, status in statuses.items():
        if name not in {server.name for server in configs}:
            lines.append(f"{name}  [{status.get('status', 'unknown')}]")
    ctx.console.print(Panel(escape("\n".join(lines)), title="MCP servers", border_style="cyan"))


async def handle_variants(ctx: CommandContext) -> None:
    if ctx.args.strip():
        ctx.ensure_environment_replaceable()
    selection = active_model_selection()
    variants = tuple(getattr(ctx.agent.model_capabilities, "available_variants", ()) or ())
    requested = ctx.args.strip()
    if not requested:
        if not variants:
            ctx.console.print("[info]The current model exposes no selectable variants.[/info]")
            return
        if not _picker_available(ctx):
            return
        selected = await ctx.terminal_input.select_async(
            title="Model variant",
            values=[
                ("default", f"{'●' if not selection.variant else ' '}  default"),
                *[
                    (
                        value,
                        f"{'●' if selection.variant == value else ' '}  {value}",
                    )
                    for value in variants
                ],
            ],
            text="Select a variant for the current model.",
        )
        if selected is None:
            return
        requested = str(selected)
    if requested != "default" and requested not in variants:
        choices = ", ".join(("default", *variants))
        ctx.console.print(f"[error]Unknown variant. Available: {choices}[/error]")
        return
    argument = f"{selection.provider}/{selection.model_id}"
    if requested != "default":
        argument += f" {requested}"
    handle_model(_context_with_args(ctx, argument))


def handle_editor(ctx: CommandContext) -> None:
    if ctx.terminal_input is None or not getattr(ctx.terminal_input, "interactive", False):
        ctx.console.print("[error]The external editor requires an interactive terminal.[/error]")
        return
    ctx.session_state["open_editor"] = True


def handle_exit(ctx: CommandContext) -> None:
    ctx.session_state["exit_requested"] = True


def _current_transcript(ctx: CommandContext) -> str:
    payload = load_session(ctx.session_id)
    return format_transcript(
        ctx.session_id,
        ctx.history,
        title=str(payload.get("title") or ""),
        tool_details=(
            getattr(getattr(ctx.terminal_input, "preferences", None), "tool_details", "compact")
            != "hidden"
        ),
    )


def _safe_export_path(value: str) -> Path:
    root = current_workdir().resolve()
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    if candidate.is_symlink():
        raise ValueError("Export target symlinks are not allowed")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Export path must stay inside the workspace") from exc
    if resolved.exists() and not resolved.is_file():
        raise ValueError("Export target must be a regular file")
    return resolved


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


async def handle_compact(ctx: CommandContext) -> None:
    if ctx.history:
        ctx.history[:] = await ctx.controller.compact(ctx.history, ctx.args or None)
        ctx.console.print("[success]Context compacted.[/success]")
        return
    ctx.console.print("[info]Nothing to compact.[/info]")


def handle_todo(ctx: CommandContext) -> None:
    ctx.console.print(render_todo())


async def handle_lsp(ctx: CommandContext) -> None:
    parts = ctx.args.split()
    try:
        action = parts[0] if parts else 'status'
        if ctx.controller is None:
            ctx.console.print('[error]LSP controls require the active Session controller.[/error]')
            return
        if action == 'status' and len(parts) <= 1:
            result = ctx.controller.lsp_status()
        elif action in {'trust','untrust','restart'} and len(parts)==2:
            result = await to_thread_settled(ctx.controller.lsp_control,parts[1],action)
        else:
            raise ValueError('Usage: /lsp [status | trust|untrust|restart ENTRY_ID]')
        ctx.console.print(escape(json.dumps(result,ensure_ascii=False,indent=2)))
    except (RuntimeError,OSError,ValueError) as exc:
        ctx.console.print('[error]'+escape(to_public_error(exc).message)+'[/error]')


def handle_memory(ctx: CommandContext) -> None:
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO
    import shlex
    from nz_coder.state.memory_cli import build_parser, execute_memory

    diagnostics = StringIO()
    try:
        getter = getattr(ctx.controller, "memory_manager", None)
        manager = getter() if callable(getter) else current_memory_manager()
        if manager is None:
            ctx.console.print("[error]Memory manager is unavailable.[/error]")
            return
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            args = build_parser().parse_args(shlex.split(ctx.args) if ctx.args.strip() else ["list"])
        session_id = str(ctx.session_state.get("id") or "terminal-user")
        context = {"capability": "manage", "scope_id": manager.scope_id,
                   "session_id": session_id, "actor_id": "terminal-user",
                   "operation_namespace": f"{manager.scope_id}:user:{session_id}"}
        result = execute_memory(args, manager, trusted_context=context)
        ctx.console.print(escape(json.dumps(result, ensure_ascii=False, indent=2)))
    except SystemExit:
        ctx.console.print(escape(diagnostics.getvalue()))
    except (RuntimeError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        ctx.console.print(f"[error]Memory action failed: {escape(to_public_error(exc).message)}[/error]")


async def handle_memory_review(ctx: CommandContext) -> None:
    """审阅展示快照；并发修改只能返回冲突，不能偷偷替换为最新版本。"""
    try:
        import uuid
        if not _picker_available(ctx):
            ctx.console.print("[info]Memory review requires an interactive terminal; use nz-coder memory.[/info]")
            return
        control = (ctx.controller.memory_control() if ctx.controller is not None
                   else _memory_control_from_manager(current_memory_manager()))
        while True:
            pending = control.pending() if control is not None else []
            if not pending:
                ctx.console.print("[info]No memory proposals are pending review.[/info]")
                return
            displayed = {item["id"]: item for item in pending}
            selected = await ctx.terminal_input.select_async(
                title="Memory review inbox",
                values=[(item["id"], _memory_proposal_label(item)) for item in pending],
                text="Enter inspects · Ctrl+A approves · Ctrl+R rejects · Esc closes",
                actions=(("c-a", "approve", "approve"), ("c-r", "reject", "reject")),
            )
            if selected is None:
                return
            action = selected.action if isinstance(selected, SelectorActionResult) else "inspect"
            proposal_id = str(selected.value if isinstance(selected, SelectorActionResult) else selected)
            proposal = displayed.get(proposal_id)
            if proposal is None:
                ctx.console.print("[error]Memory proposal was not in the displayed snapshot.[/error]")
                continue
            _render_memory_proposal(ctx, proposal)
            if action == "inspect":
                decision = await ctx.terminal_input.select_async(
                    title=f"Memory proposal rev {proposal['revision']}",
                    values=[("approve", "Approve displayed versions"), ("reject", "Reject proposal"), ("back", "Back")],
                )
                action = str(decision or "back")
            arguments = {"expected_proposal_revision": proposal["revision"],
                         "operation_id": uuid.uuid4().hex, "reviewer": "terminal-user"}
            if action == "approve":
                command = proposal.get("command", {})
                result = control.approve(proposal_id, expected_revision=command.get("expected_revision"),
                                         related_records=command.get("related_records"), **arguments)
            elif action == "reject":
                reason = await ctx.terminal_input.prompt_text_async("Rejection reason: ")
                result = control.reject(proposal_id, reason=str(reason or "rejected by terminal user"), **arguments)
            else:
                continue
            ctx.console.print(escape(json.dumps(result, ensure_ascii=False)))
    except (RuntimeError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        ctx.console.print(f"[error]Memory review failed: {escape(to_public_error(exc).message)}[/error]")


def handle_extensions(ctx: CommandContext) -> None:
    """Render a fresh extension metadata snapshot in the terminal."""
    from nz_coder.extensions.registry import ExtensionRegistry

    parts = ctx.args.strip().split(maxsplit=1)
    operation = parts[0].lower() if parts else "list"
    registry = ExtensionRegistry(workspace=current_workdir())
    if operation == "status" and len(parts) == 2:
        item = registry.get(parts[1].strip())
        if item is None:
            ctx.console.print("[error]Extension was not found.[/error]")
            return
        ctx.console.print(Panel(
            "\n".join(f"{key}: {value}" for key, value in item.to_dict().items()),
            title=item.extension_id,
            border_style="cyan",
        ))
        return
    if operation == "reload":
        results = registry.reload()
        table = Table(title="Extension owner reload")
        table.add_column("Kind")
        table.add_column("Status")
        for result in results:
            table.add_row(str(result.get("kind") or ""), str(result.get("status") or ""))
        ctx.console.print(table)
        ctx.console.print(
            "[success]Extension owners reloaded; restart-only rows remain explicit.[/success]"
        )
        return
    if operation in {"enable", "disable"} and len(parts) == 2:
        try:
            result = registry.set_enabled(parts[1].strip(), operation == "enable")
        except ValueError as exc:
            ctx.console.print(f"[error]Extension lifecycle failed: {escape(str(exc))}[/error]")
            return
        suffix = " · restart required" if result.get("restart_required") else ""
        ctx.console.print(
            f"[success]{result['extension_id']}: {result['status']}{suffix}[/success]"
        )
        return
    if operation != "list":
        ctx.console.print(
            "[error]Usage: /extensions [list|status EXTENSION_ID|reload|"
            "enable EXTENSION_ID|disable EXTENSION_ID][/error]"
        )
        return
    items = registry.snapshot()
    table = Table(title="Extensions")
    table.add_column("ID", style="cyan")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Lifecycle")
    for item in items:
        table.add_row(item.extension_id, item.kind, item.status, item.source, item.lifecycle)
    ctx.console.print(table if items else "[info]No extensions found.[/info]")


def _memory_control_from_manager(manager):
    from nz_coder.state.memory_control import MemoryControlPlane
    return MemoryControlPlane(manager) if manager is not None else None


def _memory_proposal_label(proposal):
    command = proposal.get("command", {})
    target = proposal.get("memory_id")
    target_label = f"{target}@{command.get('expected_revision')}" if target else "new memory"
    source = proposal.get("context", {}).get("session_id", "-")
    return (f"{proposal['id']} · rev {proposal['revision']} · {proposal['action']} · "
            f"{target_label} · {command.get('name', '')} · {source}")


def _render_pending_memories(ctx: CommandContext, proposals) -> None:
    table = Table(title="Memory review inbox")
    table.add_column("Proposal and displayed target versions")
    for proposal in proposals:
        table.add_row(_memory_proposal_label(proposal))
    ctx.console.print(table if proposals else "[info]No memory proposals are pending review.[/info]")


def _render_memory_proposal(ctx: CommandContext, proposal) -> None:
    ctx.console.print(Panel(escape(json.dumps(proposal, ensure_ascii=False, indent=2)),
                            title="Memory proposal", border_style="cyan"))


def handle_profile(ctx: CommandContext) -> None:
    from nz_coder.intelligence.project_profile import project_profile

    ctx.console.print(project_profile(save=True, rebuild=True))


def handle_stats(ctx: CommandContext) -> None:
    from nz_coder.state.session_stats import aggregate_session_stats, render_session_stats

    raw = ctx.args.strip()
    try:
        days = int(raw) if raw else None
        stats = aggregate_session_stats(days)
    except ValueError as exc:
        ctx.console.print(f"[error]Stats error: {escape(str(exc))}[/error]")
        return
    ctx.console.print(render_session_stats(stats))


def handle_status(ctx: CommandContext) -> None:
    ctx.console.print(
        ctx.controller.status_report(ctx.history)
        if ctx.controller is not None
        else status_report(ctx.agent, ctx.history)
    )


def handle_mode(ctx: CommandContext) -> None:
    set_permission_mode(ctx, ctx.args, alias_name="/mode")


def handle_mode_command(ctx: CommandContext):
    """Open a permission-mode picker unless an explicit mode was supplied."""
    if ctx.args.strip():
        return handle_mode(ctx)
    return handle_mode_picker(ctx)


async def handle_mode_picker(ctx: CommandContext) -> None:
    if not _picker_available(ctx):
        return
    current = ctx.agent.permissions.mode
    descriptions = {
        "default": "Ask before risky writes or commands",
        "acceptEdits": "Allow file edits; still ask for risky commands",
        "plan": "Read-only analysis and planning",
        "auto": "Allow all tool operations without asking",
    }
    values = [
        (
            mode,
            f"{'●' if mode == current else ' '}  {mode:<11} {descriptions[mode]}",
        )
        for mode in ("default", "acceptEdits", "plan", "auto")
    ]
    selected = await ctx.terminal_input.select_async(
        title="Permission mode",
        values=values,
        text="Type to filter · Enter select · Esc cancel",
    )
    if selected is not None and str(selected) != current:
        set_permission_mode(ctx, str(selected), alias_name="/mode")


def handle_trace(ctx: CommandContext) -> None:
    if ctx.controller is not None:
        ctx.console.print(ctx.controller.trace_report())
    else:
        path = latest_trace(session_id=ctx.agent.session_id) or latest_trace()
        ctx.console.print(summarize_trace(path))


def handle_diff(ctx: CommandContext) -> None:
    result = (
        ctx.controller.diff()
        if ctx.controller is not None
        else ctx.agent.change_tracker.render_diff()
        if ctx.agent.change_tracker
        else render_latest_diff()
    )
    ctx.console.print(result)


def handle_undo(ctx: CommandContext) -> None:
    from nz_coder.runtime.process.workspace_snapshot import SnapshotError
    try:
        result = ctx.controller.undo(ctx.history)
    except SnapshotError as exc:
        ctx.console.print(f"Refused to undo: {exc}")
        return
    files = ", ".join(result.files) if result.files else "no file changes"
    ctx.console.print(f"Undid message {result.message_id}: {files} ({result.removed_messages} message(s) removed)")


def handle_redo(ctx: CommandContext) -> None:
    from nz_coder.runtime.process.workspace_snapshot import SnapshotError
    try:
        result = ctx.controller.redo(ctx.history)
    except SnapshotError as exc:
        ctx.console.print(f"Refused to redo: {exc}")
        return
    files = ", ".join(result.files) if result.files else "no file changes"
    ctx.console.print(f"Redid message {result.message_id}: {files} ({result.removed_messages} message(s) restored)")


def handle_save_session(ctx: CommandContext) -> None:
    from contextlib import ExitStack
    from nz_coder.runtime.session.model import SessionIdentity
    ctx.ensure_environment_replaceable()
    target_session_id = ctx.args or ctx.agent.session_id
    store = ctx.agent.runtime_services.session_runtime.store
    with ExitStack() as ownership:
        for session_id in dict.fromkeys((ctx.agent.session_id, target_session_id)):
            lease = store.acquire(SessionIdentity(session_id), ctx.agent.workdir)
            ownership.callback(lease.close_hold)
        path = save_session(
            ctx.history,
            mode=ctx.agent.permissions.mode,
            session_id=target_session_id,
            activate=(target_session_id == ctx.agent.session_id),
        )
    ctx.console.print(f"[success]Session saved: {path}[/success]")


def handle_sessions(ctx: CommandContext) -> None:
    width = int(getattr(ctx.console, "width", 100) or 100)
    ctx.console.print(
        _render_session_options(session_options())
        if width < 100
        else render_sessions()
    )


def _render_session_options(options):  # noqa: ANN001, ANN202
    """Use stacked Session cards where a seven-column table cannot stay legible."""
    cards = []
    for option in options:
        marker = "● " if option.active else ""
        body = (
            f"[bold cyan]{marker}{escape(option.session_id)}[/bold cyan]\n"
            f"{escape(option.title or '(untitled)')}\n"
            f"[dim]{escape(option.timestamp)} · {option.message_count} messages · "
            f"{escape(option.mode)}\n{escape(option.model)}[/dim]"
        )
        cards.append(Panel(body, border_style="cyan", expand=True, padding=(0, 1)))
    if not cards:
        cards.append(Panel("No saved sessions.", border_style="cyan"))
    return Group(*cards)


def handle_processes(ctx: CommandContext) -> None:
    """Product view over the shared workspace ProcessService."""
    controller = ctx.controller
    if controller is None or not callable(getattr(controller, "processes", None)):
        ctx.console.print("[error]Persistent process control is unavailable.[/error]")
        return
    parts = ctx.args.split(maxsplit=1)
    operation = parts[0].lower() if parts else "list"
    process_id = parts[1].strip() if len(parts) > 1 else ""
    try:
        if operation in {"list", "ls"} and not process_id:
            values = controller.processes()
            if not values:
                ctx.console.print("[info]No persistent processes for this Session.[/info]")
                return
            ctx.console.print(_render_processes(
                values,
                width=int(getattr(ctx.console, "width", 100) or 100),
            ))
            return
        if operation in {"inspect", "status"} and process_id:
            item = next(
                (value for value in controller.processes() if value.get("process_id") == process_id),
                None,
            )
            if item is None:
                raise ValueError("process was not found for this Session")
            ctx.console.print(Panel(
                "\n".join(f"{key}: {item.get(key)}" for key in (
                    "process_id", "command", "cwd", "pid", "started_at", "status",
                    "exit_code", "owner_session_id", "owner_agent_id", "buffer_bytes", "pty_tier",
                )),
                title="Persistent process",
                border_style="cyan",
            ))
            return
        if operation in {"logs", "read"} and process_id:
            result = controller.process_read(process_id, tail_bytes=8192)
            ctx.console.print(Panel(
                str(result.get("output") or "(no output)"),
                title=f"Process logs · {process_id}",
                border_style="cyan",
            ))
            return
        if operation == "follow" and process_id:
            cursor = -1
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                result = controller.process_read(
                    process_id,
                    cursor=cursor,
                    tail_bytes=None,
                    max_bytes=8192,
                    wait_seconds=1.0,
                )
                cursor = int(result.get("next_cursor") or cursor)
                output = str(result.get("output") or "")
                if output:
                    ctx.console.print(output, markup=False, highlight=False)
                if result.get("status") not in {"starting", "running"}:
                    break
            return
        if operation == "kill" and process_id:
            result = controller.process_kill(process_id)
            ctx.console.print(
                f"[success]Process {process_id}: {str(result.get('status') or 'killed').upper()}[/success]"
            )
            return
        ctx.console.print("[error]Usage: /processes [list|inspect|logs|follow|kill] [PROCESS_ID][/error]")
    except (OSError, RuntimeError, ValueError, PermissionError) as exc:
        ctx.console.print(f"[error]Process operation failed: {exc}[/error]")


def _process_uptime(started_at: object) -> str:
    try:
        seconds = max(0, int(time.time() - float(started_at)))
    except (TypeError, ValueError):
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}"


def _render_processes(values: list[dict], *, width: int = 100):
    """Render complete process identities even in an 80-column terminal."""
    if width < 100:
        cards = []
        for item in values:
            process_id = str(item.get("process_id") or "")
            status = str(item.get("status") or "unknown").upper()
            command = str(item.get("command") or "")
            cwd = str(item.get("cwd") or "")
            exit_code = item.get("exit_code")
            pty = str(item.get("pty_tier") or ("pty" if item.get("tty") else "pipe"))
            body = (
                f"[bold cyan]{escape(process_id)}[/bold cyan]  [bold]{escape(status)}[/bold]\n"
                f"{escape(command or '(no command)')}\n"
                f"[dim]cwd {escape(cwd or '-')} · {_process_uptime(item.get('started_at'))}"
                f" · exit {escape(str(exit_code if exit_code is not None else '-'))} · {escape(pty)}[/dim]"
            )
            cards.append(Panel(body, border_style="cyan", expand=True, padding=(0, 1)))
        return Group(*cards)
    table = Table(title="Persistent processes", show_lines=False, expand=True)
    table.add_column("Process", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Command")
    table.add_column("CWD")
    table.add_column("Uptime", no_wrap=True)
    table.add_column("Exit", no_wrap=True)
    table.add_column("Owner", no_wrap=True)
    table.add_column("PTY", no_wrap=True)
    for item in values:
        table.add_row(
            str(item.get("process_id") or ""),
            str(item.get("status") or "unknown").upper(),
            str(item.get("command") or ""),
            str(item.get("cwd") or ""),
            _process_uptime(item.get("started_at")),
            str(item.get("exit_code") if item.get("exit_code") is not None else "-"),
            str(item.get("owner_session_id") or "-"),
            str(item.get("pty_tier") or ("pty" if item.get("tty") else "pipe")),
        )
    return table


def handle_timeline(ctx: CommandContext) -> None:
    try:
        limit = int(ctx.args) if ctx.args else 20
        if limit < 1 or limit > 100:
            raise ValueError
    except ValueError:
        ctx.console.print("[error]Usage: /timeline [LIMIT between 1 and 100][/error]")
        return
    ctx.console.print(render_timeline(ctx.history, limit=limit))


def handle_message_navigation(ctx: CommandContext) -> None:
    """Route palette/slash navigation through the persistent transcript owner."""
    action = ctx.command_name.removeprefix("message-")
    navigate = getattr(ctx.terminal_input, "navigate_message", None)
    if not callable(navigate) or not navigate(action):
        ctx.console.print("[info]No matching visible message.[/info]")


async def handle_message_detail(ctx: CommandContext) -> None:
    """Open one complete user/Assistant turn without changing Session state."""
    turns = conversation_turns(ctx.history)
    if not turns:
        ctx.console.print("[info]No historical turns are available.[/info]")
        return
    raw = ctx.args.strip()
    if raw:
        try:
            number = int(raw)
        except ValueError:
            number = 0
    elif _picker_available(ctx):
        selected = await ctx.terminal_input.select_async(
            title="Inspect message",
            values=[
                (turn.number, f"Turn {turn.number}: {_choice_preview(turn.user_text)}")
                for turn in reversed(turns)
            ],
            text="Enter opens full ToolPart details · Esc returns",
        )
        if selected is None:
            return
        number = int(selected)
    else:
        number = turns[-1].number
    if number < 1 or number > len(turns):
        ctx.console.print(
            f"[error]Turn must be between 1 and {len(turns)}.[/error]"
        )
        return
    turn = turns[number - 1]
    transcript = format_transcript(
        ctx.session_id,
        ctx.history[turn.start:turn.end],
        title=f"Turn {number}",
        tool_details=True,
    )
    ctx.console.print(Markdown(transcript))


def handle_agents(ctx: CommandContext) -> None:
    """Render the same read-only Agent catalog exposed by the daemon."""
    from nz_coder.interface.agent_catalog import agent_catalog

    table = Table(title="Agent definitions", expand=True)
    for heading in ("Name", "Role", "Model", "Tools", "Permissions", "Description"):
        table.add_column(heading)
    for item in agent_catalog(ctx.agent, current_workdir()):
        table.add_row(
            str(item.get("name") or ""),
            str(item.get("role") or ""),
            str(item.get("model") or "-"),
            str(item.get("tools") or "-"),
            str(item.get("permissions") or "-"),
            str(item.get("description") or ""),
        )
    ctx.console.print(table)


async def handle_subagents(ctx: CommandContext) -> None:
    """Select and render a child Session without replacing the parent Agent."""
    from nz_coder.runtime.agent.subagent import (
        list_subagent_sessions,
        load_subagent_session,
    )

    sessions = list_subagent_sessions(ctx.session_id, current_workdir())
    requested = ctx.args.strip()
    if not requested:
        if not sessions:
            ctx.console.print("[info]No child Agent sessions for this conversation.[/info]")
            return
        if _picker_available(ctx):
            selected = await ctx.terminal_input.select_async(
                title="Child Agent sessions",
                values=[
                    (
                        item["session_id"],
                        f"{item['session_id']} · {item['agent_type']} · {item['status']} "
                        f"· {item['message_count']} messages · {item['model_id'] or '-'}",
                    )
                    for item in sessions
                ],
                text="Enter opens a read-only child transcript · Esc returns to parent",
            )
            if selected is None:
                return
            requested = str(selected)
        else:
            table = Table(title="Child Agent sessions")
            for column in ("Session", "Agent", "Status", "Messages", "Model"):
                table.add_column(column)
            for item in sessions:
                table.add_row(
                    item["session_id"], item["agent_type"], item["status"],
                    str(item["message_count"]), item["model_id"] or "-",
                )
            ctx.console.print(table)
            return
    state = load_subagent_session(ctx.session_id, requested, current_workdir())
    if not state:
        ctx.console.print(f"[error]Unknown child Agent session: {escape(requested)}[/error]")
        return
    messages = state.get("messages") if isinstance(state.get("messages"), list) else []
    header = (
        f"{state.get('agent_type', 'unknown')} · {state.get('status', 'unknown')} · "
        f"{state.get('model_id') or '-'} · {len(messages)} messages"
    )
    ctx.console.print(Panel(header, title=str(state.get("session_id") or requested), border_style="cyan"))
    transcript = format_transcript(
        str(state.get("session_id") or requested),
        messages,
        title=f"Child Agent · {state.get('agent_type', 'unknown')}",
        tool_details=True,
    )
    ctx.console.print(Markdown(transcript))


async def handle_subagent_route(ctx: CommandContext) -> None:
    """Continue one child without replacing the parent Session runtime."""
    from nz_coder.runtime.agent.subagent import (
        list_subagent_sessions,
        load_subagent_session,
        run_subagent_async,
        scoped_parent_context,
    )

    parts = ctx.args.strip().split(maxsplit=1)
    child_id = parts[0] if parts else ""
    prompt = parts[1] if len(parts) > 1 else ""
    if not child_id:
        sessions = list_subagent_sessions(ctx.session_id, current_workdir())
        if not sessions:
            ctx.console.print("[info]No child Agent sessions for this conversation.[/info]")
            return
        if not _picker_available(ctx):
            ctx.console.print("[error]Usage: /subagent ID [PROMPT][/error]")
            return
        selected = await ctx.terminal_input.select_async(
            title="Continue child Agent",
            values=[
                (
                    item["session_id"],
                    f"{item['session_id']} · {item['agent_type']} · {item['status']}",
                )
                for item in sessions
            ],
            text="Enter selects a child · Esc returns to parent",
        )
        if selected is None:
            return
        child_id = str(selected)
    state = load_subagent_session(ctx.session_id, child_id, current_workdir())
    if not state:
        ctx.console.print(f"[error]Unknown child Agent session: {escape(child_id)}[/error]")
        return
    if str(state.get("status") or "") in {"queued", "running", "cancel_requested"}:
        ctx.console.print(f"[error]Child Agent {escape(child_id)} is still active.[/error]")
        return
    if not prompt and ctx.terminal_input is not None:
        prompt = (
            await ctx.terminal_input.prompt_text_async(
                f"Continue {child_id}: ",
            )
            or ""
        ).strip()
    if not prompt:
        ctx.console.print("[error]A follow-up prompt is required.[/error]")
        return
    ctx.console.print(f"[info]Continuing child Agent {escape(child_id)}...[/info]")
    with scoped_parent_context(
        session_id=ctx.session_id,
        tracer=getattr(ctx.agent, "tracer", None),
        agent_id=str(getattr(ctx.agent, "agent_id", "") or ""),
        trace_id=str(getattr(getattr(ctx.agent, "tracer", None), "trace_id", "") or ""),
        model_id=str(getattr(ctx.agent, "model_id", "") or ""),
    ):
        result = await run_subagent_async(
            prompt,
            agent_type=str(state.get("agent_type") or "explore"),
            session_id=child_id,
            allowed_tools=list(state.get("allowed_tools") or []),
            target_paths=list(state.get("claimed_paths") or []),
        )
    ctx.console.print(Panel(
        str(result),
        title=f"Child Agent · {child_id}",
        border_style="cyan" if not str(result).startswith("Error:") else "red",
    ))


def handle_fork(ctx: CommandContext) -> None:
    ctx.ensure_environment_replaceable()
    turns = conversation_turns(ctx.history)
    if not turns:
        ctx.console.print("[error]No user turns are available to fork.[/error]")
        return
    try:
        turn_number = int(ctx.args) if ctx.args else turns[-1].number
        forked = fork_history(ctx.history, turn_number)
    except ValueError as exc:
        ctx.console.print(f"[error]Error: {exc}[/error]")
        return

    from nz_coder.runtime.agent.subagent import clone_referenced_subagents, discard_cloned_subagents
    from nz_coder.runtime.session.model import Session, SessionIdentity
    from nz_coder.runtime.session.store import LegacyJsonSessionStore, SessionHistoryUnconfirmed
    from nz_coder.state.sessions import session_artifact_dir
    import shutil

    old_session_id = ctx.session_id
    store = LegacyJsonSessionStore()
    lease = store.acquire(SessionIdentity(old_session_id), current_workdir())
    new_agent = None
    clones = None
    new_session_id = None
    def discard_preparation():
        if clones is not None:
            discard_cloned_subagents(clones)
        if new_session_id is not None:
            artifact = session_artifact_dir(new_session_id)
            if artifact.exists():
                shutil.rmtree(artifact)
    try:
        save_session(ctx.history, mode=ctx.agent.permissions.mode, session_id=old_session_id,
                     activate=False, require_aliases=False)
        original = load_session(old_session_id)
        fork_title = forked_session_title(str(original.get("title") or "New Session"))
        new_session_id = create_session_id("fork")
        forked = rebind_fork_history(forked, new_session_id)
        from nz_coder.runtime.session.session_revert import copy_fork_snapshots
        copy_fork_snapshots(current_workdir(), old_session_id, new_session_id, forked)
        new_agent = ctx.build_agent(ctx.system_prompt, ctx.renderer, new_session_id,
                                    permission_mode=ctx.agent.permissions.mode, activate_session_on_build=False)
        clones = clone_referenced_subagents(old_session_id, new_session_id, forked,
                    parent_agent_id=new_agent.agent_id, workspace_root=current_workdir())
        candidate = Session.create(new_session_id, forked, workspace=current_workdir(),
                                   parent_session_id=old_session_id,
                                   metadata={"title": fork_title, "permission_mode": new_agent.permissions.mode})
        write = store.prepare_history_transition(candidate)
        write.commit(candidate)
    except SessionHistoryUnconfirmed:
        # 未知提交保留新父及子凭据，不能把可能已提交的目标清掉。
        write.rollback = discard_preparation
        if new_agent is not None:
            new_agent.close()
        raise
    except Exception as exc:
        if new_agent is not None:
            new_agent.close()
        discard_preparation()
        ctx.console.print(f"[error]Error: could not fork session: {exc}[/error]")
        return
    finally:
        lease.close_hold()

    try:
        ctx.replace_agent(new_session_id, new_agent)
    except Exception as exc:
        ctx.console.print(f"[error]Fork saved as {new_session_id}, but activation failed: {exc}. Resume this ID to retry.[/error]")
        return
    ctx.history[:] = candidate.transcript
    if "session_title" in ctx.session_state:
        ctx.session_state["session_title"] = fork_title
    ctx.console.print(
        f"[success]Forked turn {turn_number} into {new_session_id} "
        f"({len(ctx.history)} messages). Workspace files were not changed.[/success]"
    )


async def handle_fork_picker(ctx: CommandContext) -> None:
    turns = conversation_turns(ctx.history)
    if not turns:
        ctx.console.print("[error]No user turns are available to fork.[/error]")
        return
    if not _picker_available(ctx):
        return
    selected = await ctx.terminal_input.select_async(
        title="Fork session",
        values=[
            (turn.number, f"Turn {turn.number}: {_choice_preview(turn.user_text)}")
            for turn in turns
        ],
        text="Fork through the selected completed user turn. Workspace files stay shared.",
    )
    if selected is not None:
        handle_fork(_context_with_args(ctx, str(selected)))


def handle_new_session(ctx: CommandContext) -> None:
    ctx.ensure_environment_replaceable()
    new_session_id = create_session_id()
    new_agent = ctx.build_agent(
        ctx.system_prompt, ctx.renderer, new_session_id,
        permission_mode=ctx.agent.permissions.mode, activate_session_on_build=False,
    )
    ctx.replace_agent(new_session_id, new_agent)
    ctx.history.clear()
    ctx.console.print(f"[success]Started new session {new_session_id}.[/success]")


def handle_resume(ctx: CommandContext) -> None:
    _resume_session(ctx, ctx.args or "latest")


async def handle_session_picker(ctx: CommandContext) -> None:
    if not _picker_available(ctx):
        return
    pending_delete = ""
    while True:
        all_options = session_options(limit=100)
        for option in all_options:
            if option.unavailable:
                ctx.console.print(f"[error]Session {escape(option.session_id)} unavailable: {escape(option.unavailable)}[/error]")
        options = [option for option in all_options if not option.unavailable]
        if not options:
            ctx.console.print("[info]No saved sessions are available.[/info]")
            return
        selected = await ctx.terminal_input.select_async(
            title="Resume session",
            values=[
                (
                    option.session_id,
                    f"{'DELETE? ' if option.session_id == pending_delete else ''}"
                    f"{'●' if option.active else ' '}  {option.session_id}  "
                    f"· {option.message_count} messages · {option.model} · {option.mode}",
                )
                for option in options
            ],
            text=(
                "Enter resumes · Ctrl+D deletes · press Ctrl+D twice on the same "
                "Session to confirm · Esc cancels."
            ),
            actions=(("c-d", "delete", "delete"),),
        )
        if isinstance(selected, SelectorActionResult):
            selected_id = str(selected.value or "")
            if selected.action != "delete" or not selected_id:
                continue
            if pending_delete != selected_id:
                pending_delete = selected_id
                continue
            _delete_session_and_transition(ctx, selected_id)
            pending_delete = ""
            continue
        if selected is None:
            return
        selected_id = str(selected)
        if selected_id == ctx.session_id:
            ctx.console.print(f"[info]Session {selected_id} is already active.[/info]")
            return
        _resume_session(ctx, selected_id)
        return


async def handle_delete_session(ctx: CommandContext) -> None:
    """Delete a persisted Session after an explicit typed confirmation."""
    session_id = ctx.args.strip()
    if not session_id:
        await handle_session_picker(ctx)
        return
    if ctx.terminal_input is None or not ctx.terminal_input.interactive:
        ctx.console.print(
            "[error]Session deletion requires an interactive confirmation; use /session.[/error]"
        )
        return
    confirmed = await ctx.terminal_input.prompt_text_async(
        f"Type {session_id} to delete it and its artifacts: "
    )
    if confirmed != session_id:
        ctx.console.print("[info]Session deletion cancelled.[/info]")
        return
    _delete_session_and_transition(ctx, session_id)


def _delete_session_and_transition(ctx: CommandContext, session_id: str) -> bool:
    """Delete one Session, moving the active CLI to a fresh owner first."""
    deleting_current = session_id == ctx.session_id
    if deleting_current:
        ctx.ensure_environment_replaceable()
        new_session_id = create_session_id()
        try:
            new_agent = ctx.build_agent(
                ctx.system_prompt,
                ctx.renderer,
                new_session_id,
                permission_mode=ctx.agent.permissions.mode,
                activate_session_on_build=False,
            )
            ctx.replace_agent(new_session_id, new_agent)
            ctx.history.clear()
        except Exception as exc:
            ctx.console.print(
                f"[error]Could not create a replacement Session: {type(exc).__name__}: {exc}[/error]"
            )
            return False
    try:
        deleted = delete_session(session_id)
    except (OSError, RuntimeError, ValueError) as exc:
        ctx.console.print(f"[error]Session deletion failed: {exc}[/error]")
        return False
    if not deleted:
        ctx.console.print(f"[error]Unknown Session: {session_id}[/error]")
        return False
    suffix = f"; started {ctx.session_id}" if deleting_current else ""
    ctx.console.print(f"[success]Deleted Session {session_id}{suffix}.[/success]")
    return True


def _resume_session(ctx: CommandContext, selector: str) -> None:
    ctx.ensure_environment_replaceable()
    payload = load_session(selector)
    if not payload:
        ctx.console.print("[error]No saved session found.[/error]")
        return
    if payload.get("workspace") and payload["workspace"] != str(current_workdir()):
        ctx.console.print(f"[error]Session workspace differs: {payload['workspace']}[/error]")
        return
    resumed_session_id = payload.get("session_id") or create_session_id()
    mode = payload.get("mode")
    if mode not in format_mode_usage(valid_only=True):
        mode = ctx.agent.permissions.mode
    new_agent = ctx.build_agent(
        ctx.system_prompt,
        ctx.renderer,
        resumed_session_id,
        permission_mode=mode,
        activate_session_on_build=False,
    )
    ctx.replace_agent(resumed_session_id, new_agent)
    ctx.history[:] = payload.get("messages", [])
    if "session_title" in ctx.session_state:
        ctx.session_state["session_title"] = str(payload.get("title") or "")
    ctx.console.print(
        f"[success]Resumed session {resumed_session_id} ({len(ctx.history)} messages).[/success]"
    )


def _picker_available(ctx: CommandContext) -> bool:
    if ctx.terminal_input is not None and getattr(ctx.terminal_input, "interactive", False):
        return True
    ctx.console.print("[error]This picker requires an interactive terminal.[/error]")
    return False


def _reload_terminal_preferences(ctx: CommandContext) -> None:
    reload_preferences = getattr(ctx.terminal_input, "reload_preferences", None)
    if callable(reload_preferences):
        reload_preferences()


def _context_with_args(ctx: CommandContext, args: str) -> CommandContext:
    from dataclasses import replace

    return replace(ctx, args=args)


def _choice_preview(value: str, limit: int = 72) -> str:
    compact = " ".join(str(value).split())
    if len(compact) > limit:
        return f"{compact[:limit - 1]}…"
    return compact or "(empty)"


def handle_clear(ctx: CommandContext) -> None:
    # 历史与任务状态按会话恢复；只清本地列表会在下一轮回灌旧内容。
    # 切换身份复用新会话流程，旧历史和工作记忆仍可显式恢复。
    previous_session_id = ctx.session_id
    handle_new_session(ctx)
    ctx.console.print(f"[dim]Previous session ID: {previous_session_id}[/dim]")
