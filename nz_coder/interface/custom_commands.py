"""Discover and expand inert Markdown prompt commands for product surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, TYPE_CHECKING

import yaml

from nz_coder.interface.commands.registry import Command, CommandRegistry

if TYPE_CHECKING:
    from nz_coder.foundation.project_control import ProjectControlSnapshot
    from nz_coder.foundation.workspace_trust import ConfigSnapshot


_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_COMMAND_BYTES = 256 * 1024
_ALLOWED_FRONTMATTER = {"description", "allowed_tools", "model", "arguments"}


class CommandParseError(ValueError):
    """Describe one command file that was ignored without breaking startup."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class PromptCommand:
    """One immutable prompt shortcut loaded from a trusted local scope."""

    name: str
    description: str
    template: str
    allowed_tools: tuple[str, ...]
    model: str | None
    source: str
    path: Path


@dataclass(frozen=True)
class ExpandedCommand:
    """Normal user prompt plus policy restrictions derived from a command."""

    name: str
    prompt: str
    allowed_tools: tuple[str, ...]
    model: str | None
    source: str


class CommandCatalog:
    """Precedence-aware command catalog with no executable extension hooks."""

    def __init__(
        self,
        commands: Iterable[PromptCommand] = (),
        errors: Iterable[CommandParseError] = (),
    ) -> None:
        self._commands = {command.name: command for command in commands}
        self.errors = tuple(errors)

    @classmethod
    def discover(
        cls,
        *,
        project_dir: Path | str | None = None,
        user_dir: Path | str | None = None,
        bundled_dir: Path | str | None = None,
        project_files: Iterable[Path] | None = None,
        project_trusted: bool = False,
        project_control_snapshot: ProjectControlSnapshot | None = None,
    ) -> "CommandCatalog":
        """Load bundled < user < project, so later scopes replace by name."""
        commands: dict[str, PromptCommand] = {}
        errors: list[CommandParseError] = []
        for source, directory in (
            ("bundled", bundled_dir),
            ("user", user_dir),
        ):
            if directory is None:
                continue
            root = Path(directory).expanduser()
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.md")):
                try:
                    command = _parse_command(path, source)
                except CommandParseError as exc:
                    errors.append(exc)
                    continue
                commands[command.name] = command
        if (
            project_control_snapshot is not None
            and project_control_snapshot.trusted
        ):
            root = Path(project_dir).expanduser() if project_dir is not None else None
            for item in project_control_snapshot.files_for_kind("command"):
                path = (
                    root / Path(item.relative_path).name
                    if root is not None
                    else Path(item.relative_path)
                )
                try:
                    command = _parse_command_bytes(path, "project", item.content)
                except CommandParseError as exc:
                    errors.append(exc)
                    continue
                commands[command.name] = command
        return cls(commands.values(), errors)

    def get(self, name: str) -> PromptCommand | None:
        return self._commands.get(_normalize_name(name))

    def list(self) -> tuple[PromptCommand, ...]:
        return tuple(self._commands[name] for name in sorted(self._commands))

    def expand(self, name: str, raw_args: str = "") -> ExpandedCommand:
        command = self.get(name)
        if command is None:
            raise KeyError(_normalize_name(name))
        args = str(raw_args)
        words = args.split()
        prompt = command.template.replace("$ARGUMENTS", args)
        for index in range(9, 0, -1):
            value = words[index - 1] if len(words) >= index else ""
            prompt = prompt.replace(f"${index}", value)
        return ExpandedCommand(
            name=command.name,
            prompt=prompt.strip(),
            allowed_tools=command.allowed_tools,
            model=command.model,
            source=command.source,
        )

    def expand_invocation(self, value: str) -> ExpandedCommand | None:
        text = str(value).strip()
        if not text.startswith("/"):
            return None
        head, separator, args = text.partition(" ")
        command = self.get(head)
        return self.expand(command.name, args if separator else "") if command else None


def default_command_catalog(
    workspace: Path | str,
    *,
    config_snapshot: ConfigSnapshot | None = None,
) -> CommandCatalog:
    """Resolve the standard runtime-owned command scopes."""
    from nz_coder.foundation.workspace_trust import load_config_snapshot

    root = Path(workspace).resolve()
    snapshot = config_snapshot or load_config_snapshot(root)
    return CommandCatalog.discover(
        project_dir=root / ".nz-coder" / "commands",
        project_trusted=snapshot.control_plane_trusted,
        project_control_snapshot=snapshot.project_control,
        user_dir=Path.home() / ".nz-coder" / "commands",
        bundled_dir=Path(__file__).resolve().parent.parent / "bundled_commands",
    )


def register_command_completion(
    registry: CommandRegistry,
    catalog: CommandCatalog,
) -> None:
    """Project prompt commands into slash completion without owning dispatch."""
    for prompt_command in catalog.list():
        try:
            registry.register(Command(
                prompt_command.name,
                prompt_command.description or "Run a custom prompt command",
                f"/{prompt_command.name} [arguments]",
                _completion_only,
                category="Custom",
            ))
        except ValueError:
            # Built-in product controls always win over prompt shortcuts.
            continue


def _completion_only(_context) -> None:  # noqa: ANN001
    return None


def _parse_command(path: Path, source: str) -> PromptCommand:
    if path.is_symlink():
        raise CommandParseError(path, "command files must not be symlinks")
    name = path.stem.lower()
    if not _NAME.fullmatch(name):
        raise CommandParseError(path, "filename must match [a-z0-9][a-z0-9_-]*.md")
    try:
        size = path.stat().st_size
        if size > _MAX_COMMAND_BYTES:
            raise CommandParseError(path, "command exceeds 256 KiB")
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandParseError(path, str(exc)) from exc
    return _parse_command_text(path, source, raw)


def _parse_command_bytes(path: Path, source: str, payload: bytes) -> PromptCommand:
    if len(payload) > _MAX_COMMAND_BYTES:
        raise CommandParseError(path, "command exceeds 256 KiB")
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommandParseError(path, "command is not valid UTF-8") from exc
    return _parse_command_text(path, source, raw)


def _parse_command_text(path: Path, source: str, raw: str) -> PromptCommand:
    name = path.stem.lower()
    if not _NAME.fullmatch(name):
        raise CommandParseError(path, "filename must match [a-z0-9][a-z0-9_-]*.md")
    metadata, template = _frontmatter(path, raw)
    unknown = sorted(set(metadata) - _ALLOWED_FRONTMATTER)
    if unknown:
        raise CommandParseError(path, f"unsupported frontmatter keys: {', '.join(unknown)}")
    description = metadata.get("description", "")
    if not isinstance(description, str):
        raise CommandParseError(path, "description must be a string")
    allowed = metadata.get("allowed_tools", [])
    if not isinstance(allowed, list) or any(
        not isinstance(item, str) or not item.strip() for item in allowed
    ):
        raise CommandParseError(path, "allowed_tools must be a list of tool names")
    model = metadata.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise CommandParseError(path, "model must be a non-empty string")
    if not template.strip():
        raise CommandParseError(path, "prompt template must not be empty")
    return PromptCommand(
        name=name,
        description=description.strip()[:500],
        template=template.strip(),
        allowed_tools=tuple(dict.fromkeys(item.strip() for item in allowed)),
        model=model.strip() if isinstance(model, str) else None,
        source=source,
        path=path.absolute(),
    )


def _frontmatter(path: Path, raw: str) -> tuple[dict, str]:
    if not raw.startswith("---\n"):
        return {}, raw
    marker = raw.find("\n---\n", 4)
    if marker < 0:
        raise CommandParseError(path, "frontmatter is not terminated")
    try:
        parsed = yaml.safe_load(raw[4:marker]) or {}
    except yaml.YAMLError as exc:
        raise CommandParseError(path, "invalid YAML frontmatter") from exc
    if not isinstance(parsed, dict):
        raise CommandParseError(path, "frontmatter must be a mapping")
    return parsed, raw[marker + 5:]


def _normalize_name(value: str) -> str:
    return str(value).strip().lower().lstrip("/")


__all__ = [
    "CommandCatalog",
    "CommandParseError",
    "ExpandedCommand",
    "PromptCommand",
    "default_command_catalog",
    "register_command_completion",
]
