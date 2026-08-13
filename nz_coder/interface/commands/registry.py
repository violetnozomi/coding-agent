"""Slash-command registry and dispatch for the terminal CLI."""
from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
from typing import Any, Callable


CommandHandler = Callable[["CommandContext"], Any]
BuildAgentFunc = Callable[[str, Any, str, str | None], Any]

_COMMON_COMMANDS = (
    "status",
    "diff",
    "session",
    "processes",
    "attach",
    "help",
    "exit",
)
_PRODUCT_CATEGORY_BY_LEGACY = {
    "General": "Essentials",
    "Input": "Files",
    "Changes": "Files",
    "Permissions": "Settings",
    "Terminal": "Settings",
}
_PRODUCT_CATEGORY_BY_COMMAND = {
    "memory": "Memory",
    "memory-review": "Memory",
    "extensions": "Extensions",
    "skills": "Extensions",
    "mcps": "Extensions",
    "processes": "Processes",
}


@dataclass(frozen=True)
class Command:
    """A single slash command exposed by the CLI."""

    name: str
    description: str
    usage: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    category: str = "General"
    keybind: str = ""
    suggested: bool = False
    hidden: bool = False


@dataclass
class CommandContext:
    """Mutable runtime state available to command handlers."""

    history: list
    session_state: dict
    system_prompt: str
    renderer: Any
    console: Any
    build_agent: BuildAgentFunc
    terminal_input: Any = None
    registry: CommandRegistry | None = None
    raw_command: str = ""
    command_name: str = ""
    args: str = ""

    @property
    def agent(self) -> Any:
        return self.session_state["agent"]

    @property
    def controller(self) -> Any:
        """Return the explicit product control API when the host provides it."""
        return self.session_state.get("controller")

    @property
    def session_id(self) -> str:
        return self.session_state["id"]

    def replace_agent(self, session_id: str, agent: Any) -> None:
        previous = self.session_state.get("agent")
        self.session_state["id"] = session_id
        self.session_state["agent"] = agent
        if "session_title" in self.session_state:
            self.session_state["session_title"] = ""
        controller = self.session_state.get("controller")
        replace_environment = getattr(controller, "replace_environment", None)
        if callable(replace_environment):
            replace_environment(agent)
        if previous is agent:
            return
        close = getattr(previous, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # Session replacement must not fail after the new Agent exists.
                pass


class CommandRegistry:
    """Registry for slash commands and aliases."""

    def __init__(self) -> None:
        self._lookup: dict[str, Command] = {}
        self._commands: list[Command] = []

    def register(self, command: Command) -> None:
        keys = [command.name, *command.aliases]
        normalized = [self._normalize(key) for key in keys]
        for key in normalized:
            if key in self._lookup:
                raise ValueError(f"Command already registered: {key}")
        self._commands.append(command)
        for key in normalized:
            self._lookup[key] = command

    def dispatch(self, raw_command: str, context: CommandContext) -> bool:
        resolved = self._resolve(raw_command, context)
        if resolved is None:
            return False
        command, command_context = resolved
        result = command.handler(command_context)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise RuntimeError(
                f"Command /{command.name} requires asynchronous dispatch"
            )
        return True

    async def dispatch_async(self, raw_command: str, context: CommandContext) -> bool:
        """Dispatch sync and async handlers from the terminal event loop."""
        resolved = self._resolve(raw_command, context)
        if resolved is None:
            return False
        command, command_context = resolved
        result = command.handler(command_context)
        if inspect.isawaitable(result):
            await result
        return True

    def _resolve(
        self,
        raw_command: str,
        context: CommandContext,
    ) -> tuple[Command, CommandContext] | None:
        parts = raw_command.strip().split(maxsplit=1)
        if not parts:
            return None
        command = self._lookup.get(self._normalize(parts[0]))
        if command is None:
            return None
        command_context = replace(
            context,
            registry=self,
            raw_command=raw_command,
            command_name=command.name,
            args=parts[1] if len(parts) > 1 else "",
        )
        return command, command_context

    def iter_commands(self) -> tuple[Command, ...]:
        return tuple(self._commands)

    def visible_commands(self) -> tuple[Command, ...]:
        """Return commands exposed by slash completion and the command palette."""
        return tuple(command for command in self._commands if not command.hidden)

    def palette_commands(self, *, recent: tuple[str, ...] = ()) -> tuple[Command, ...]:
        """Order palette rows by product usefulness instead of registration."""
        recent_rank = {self._normalize(name): index for index, name in enumerate(recent)}
        common_rank = {name: index for index, name in enumerate(_COMMON_COMMANDS)}

        def rank(command: Command) -> tuple[int, int, str, str]:
            if command.suggested:
                group, position = 0, 0
            elif command.name in recent_rank:
                group, position = 1, recent_rank[command.name]
            elif command.name in common_rank:
                group, position = 2, common_rank[command.name]
            else:
                group, position = 3, 0
            return group, position, product_command_category(command), command.name

        return tuple(sorted(self.visible_commands(), key=rank))

    def get(self, name: str) -> Command | None:
        """Return one command for product-adapter precedence checks."""
        return self._lookup.get(self._normalize(name))

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().lstrip("/")


def product_command_category(command: Command | None) -> str:
    """Return stable user-facing command categories without breaking adapters."""
    if command is None:
        return "Essentials"
    return _PRODUCT_CATEGORY_BY_COMMAND.get(
        command.name,
        _PRODUCT_CATEGORY_BY_LEGACY.get(command.category, command.category),
    )
