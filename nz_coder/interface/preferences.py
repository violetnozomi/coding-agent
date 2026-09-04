"""Workspace-owned terminal preferences and model interaction history."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.theme import Theme

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.user_paths import prepare_user_storage


_STATE_PATH = Path("terminal/preferences.json")
_MAX_STATE_BYTES = 64_000
_MAX_RECENT_MODELS = 20
_MAX_FAVORITE_MODELS = 100
_MESSAGE_KEYBIND_ACTIONS = {
    "messages_first",
    "messages_last",
    "messages_next",
    "messages_previous",
    "messages_last_user",
}
_DEFAULT_MESSAGE_KEYBINDS = {
    "messages_first": "home",
    "messages_last": "end",
    "messages_next": "c-x j",
    "messages_previous": "c-x k",
    "messages_last_user": "c-x h",
}

_THEMES = {
    "nzcoder": {
        "accent": "#00a7c4",
        "border": "#596579",
        "background": "#20242b",
        "muted_background": "#292e37",
        "text": "#d8dee9",
        "muted": "#9aa5b4",
        "success": "green",
        "warning": "yellow",
        "error": "red",
    },
    "opencode": {
        "accent": "#fab283",
        "border": "#5c6370",
        "background": "#1e1e1e",
        "muted_background": "#2a2a2a",
        "text": "#eeeeee",
        "muted": "#a0a0a0",
        "success": "#9ece6a",
        "warning": "#e0af68",
        "error": "#f7768e",
    },
    "catppuccin": {
        "accent": "#89b4fa",
        "border": "#585b70",
        "background": "#1e1e2e",
        "muted_background": "#313244",
        "text": "#cdd6f4",
        "muted": "#a6adc8",
        "success": "#a6e3a1",
        "warning": "#f9e2af",
        "error": "#f38ba8",
    },
    "nord": {
        "accent": "#88c0d0",
        "border": "#4c566a",
        "background": "#2e3440",
        "muted_background": "#3b4252",
        "text": "#eceff4",
        "muted": "#d8dee9",
        "success": "#a3be8c",
        "warning": "#ebcb8b",
        "error": "#bf616a",
    },
    "gruvbox": {
        "accent": "#d79921",
        "border": "#665c54",
        "background": "#282828",
        "muted_background": "#3c3836",
        "text": "#ebdbb2",
        "muted": "#bdae93",
        "success": "#98971a",
        "warning": "#d79921",
        "error": "#cc241d",
    },
    "colorblind": {
        "accent": "#56b4e9",
        "border": "#6b7280",
        "background": "#111827",
        "muted_background": "#1f2937",
        "text": "#f9fafb",
        "muted": "#d1d5db",
        "success": "#009e73",
        "warning": "#f0e442",
        "error": "#d55e00",
    },
    "monochrome": {
        "accent": "#ffffff",
        "border": "#808080",
        "background": "#000000",
        "muted_background": "#262626",
        "text": "#ffffff",
        "muted": "#b3b3b3",
        "success": "white",
        "warning": "bright_white",
        "error": "bold white",
    },
}


@dataclass(frozen=True)
class TerminalPreferences:
    """Validated terminal state persisted inside one workspace."""

    theme: str = "nzcoder"
    tool_details: str = "normal"
    mouse: bool = True
    paste_summary: bool = True
    sidebar: str = "auto"
    recent_models: tuple[str, ...] = ()
    favorite_models: tuple[str, ...] = ()
    keybindings: tuple[tuple[str, str], ...] = ()


def theme_names() -> tuple[str, ...]:
    """Return stable user-selectable theme names."""
    return tuple(_THEMES)


def configurable_keybinding_actions() -> tuple[str, ...]:
    """Return terminal actions whose bindings can currently be overridden."""
    return tuple(sorted(_MESSAGE_KEYBIND_ACTIONS))


def message_keybindings(preferences: TerminalPreferences) -> dict[str, str]:
    """Merge validated workspace overrides onto product defaults."""
    return {**_DEFAULT_MESSAGE_KEYBINDS, **dict(preferences.keybindings)}


def command_keybinding(
    command_name: str, default: str, preferences: TerminalPreferences
) -> str:
    """Return the effective palette label for a configurable command."""
    action = "messages_" + str(command_name).removeprefix("message-").replace("-", "_")
    if action not in _MESSAGE_KEYBIND_ACTIONS:
        return default
    value = message_keybindings(preferences)[action]
    return "" if value == "none" else value


def load_terminal_preferences(workspace: Path | None = None) -> TerminalPreferences:
    """Load one workspace's validated terminal preferences."""
    path = _preference_path(workspace)
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            return TerminalPreferences()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return TerminalPreferences()
    if not isinstance(payload, dict):
        return TerminalPreferences()
    theme = str(payload.get("theme") or "nzcoder")
    if theme not in _THEMES:
        theme = "nzcoder"
    details = str(payload.get("tool_details") or "normal")
    if details not in {"hidden", "compact", "normal", "detailed", "full"}:
        details = "normal"
    sidebar = str(payload.get("sidebar") or "auto")
    if sidebar not in {"auto", "show", "hide"}:
        sidebar = "auto"
    return TerminalPreferences(
        theme=theme,
        tool_details=details,
        mouse=bool(payload.get("mouse", True)),
        paste_summary=bool(payload.get("paste_summary", True)),
        sidebar=sidebar,
        recent_models=_model_list(payload.get("recent_models"), _MAX_RECENT_MODELS),
        favorite_models=_model_list(payload.get("favorite_models"), _MAX_FAVORITE_MODELS),
        keybindings=_keybinding_list(payload.get("keybindings"), strict=False),
    )


def update_terminal_preferences(
    *,
    workspace: Path | None = None,
    **updates,
) -> TerminalPreferences:
    """Atomically update validated fields while preserving unrelated state."""
    current = load_terminal_preferences(workspace)
    payload = asdict(current)
    payload.update(updates)
    next_state = _validate_preferences(payload)
    _write_preferences(_preference_path(workspace), next_state)
    return next_state


def record_recent_model(model_id: str, workspace: Path | None = None) -> TerminalPreferences:
    """Move one provider/model identifier to the front of recent history."""
    value = _validate_model_id(model_id)
    current = load_terminal_preferences(workspace)
    recent = (value, *(item for item in current.recent_models if item != value))
    return update_terminal_preferences(
        workspace=workspace,
        recent_models=recent[:_MAX_RECENT_MODELS],
    )


def toggle_favorite_model(model_id: str, workspace: Path | None = None) -> bool:
    """Toggle one favorite and return its resulting favorite state."""
    value = _validate_model_id(model_id)
    current = load_terminal_preferences(workspace)
    if value in current.favorite_models:
        favorites = tuple(item for item in current.favorite_models if item != value)
        selected = False
    else:
        favorites = (value, *current.favorite_models)[:_MAX_FAVORITE_MODELS]
        selected = True
    update_terminal_preferences(workspace=workspace, favorite_models=favorites)
    return selected


def cycle_model_id(
    *,
    favorites: bool = False,
    reverse: bool = False,
    current: str = "",
    workspace: Path | None = None,
) -> str | None:
    """Return the next recent/favorite model without mutating active selection."""
    prefs = load_terminal_preferences(workspace)
    values = prefs.favorite_models if favorites else prefs.recent_models
    if not values:
        return None
    if current not in values:
        return values[-1] if reverse else values[0]
    offset = -1 if reverse else 1
    return values[(values.index(current) + offset) % len(values)]


def prompt_style(theme_name: str) -> Style:
    """Build prompt-toolkit composer styles for one selected theme."""
    palette = _palette(theme_name)
    return Style.from_dict({
        "prompt": f"bold {palette['accent']}",
        "prompt.continuation": palette["accent"],
        "composer.border": palette["border"],
        "status": palette["muted"],
        "transcript": palette["text"],
        "bottom-toolbar": f"bg:{palette['muted_background']} {palette['muted']}",
        "sidebar": f"bg:{palette['muted_background']} {palette['muted']}",
        "completion-menu.completion": f"bg:{palette['background']} {palette['text']}",
        "completion-menu.completion.current": f"bg:{palette['accent']} #000000 bold",
        "completion-menu.meta.completion": f"bg:{palette['muted_background']} {palette['muted']}",
        "completion-menu.meta.completion.current": f"bg:{palette['accent']} #000000",
    })


def selector_style(theme_name: str) -> Style:
    """Build prompt-toolkit dialog styles for one selected theme."""
    palette = _palette(theme_name)
    return Style.from_dict({
        "frame.label": f"{palette['accent']} bold",
        "frame.border": palette["border"],
        "selector.hint": palette["muted"],
        "selector.rule": palette["border"],
        "selector.search": f"{palette['text']} bg:{palette['background']}",
        "selector.option": palette["text"],
        "selector.selected": f"#000000 bg:{palette['accent']} bold",
        "selector.empty": f"{palette['muted']} italic",
    })


def rich_theme(theme_name: str) -> Theme:
    """Build semantic Rich styles used by the scrolling terminal renderer."""
    palette = _palette(theme_name)
    return Theme({
        "tool": f"bold {palette['warning']}",
        "info": f"bold {palette['accent']}",
        "error": f"bold {palette['error']}",
        "success": f"bold {palette['success']}",
    })


def _validate_preferences(payload: dict) -> TerminalPreferences:
    theme = str(payload.get("theme") or "nzcoder")
    if theme not in _THEMES:
        raise ValueError(f"Unknown terminal theme '{theme}'")
    details = str(payload.get("tool_details") or "normal")
    if details not in {"hidden", "compact", "normal", "detailed", "full"}:
        raise ValueError("tool_details must be compact, normal, or detailed")
    sidebar = str(payload.get("sidebar") or "auto")
    if sidebar not in {"auto", "show", "hide"}:
        raise ValueError("sidebar must be auto, show, or hide")
    return TerminalPreferences(
        theme=theme,
        tool_details=details,
        mouse=bool(payload.get("mouse", True)),
        paste_summary=bool(payload.get("paste_summary", True)),
        sidebar=sidebar,
        recent_models=_model_list(payload.get("recent_models"), _MAX_RECENT_MODELS),
        favorite_models=_model_list(payload.get("favorite_models"), _MAX_FAVORITE_MODELS),
        keybindings=_keybinding_list(payload.get("keybindings"), strict=True),
    )


def _keybinding_list(value, *, strict: bool) -> tuple[tuple[str, str], ...]:
    if value in (None, (), []):
        return ()
    if isinstance(value, (list, tuple)):
        try:
            value = dict(value)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(
                    "keybindings must map action names to key sequences"
                ) from None
            return ()
    if not isinstance(value, dict):
        if strict:
            raise ValueError("keybindings must map action names to key sequences")
        return ()
    result = []
    for raw_action, raw_sequence in value.items():
        action = str(raw_action).strip().lower()
        sequence = " ".join(str(raw_sequence).strip().lower().split())
        try:
            _validate_keybinding(action, sequence)
        except ValueError:
            if strict:
                raise
            continue
        result.append((action, sequence))
    return tuple(sorted(result))


def _validate_keybinding(action: str, sequence: str) -> None:
    if action not in _MESSAGE_KEYBIND_ACTIONS:
        raise ValueError(f"Unknown configurable keybinding action '{action}'")
    if sequence == "none":
        return
    tokens = sequence.split()
    if not tokens or len(tokens) > 3 or len(sequence) > 64:
        raise ValueError("keybinding must contain one to three key tokens")
    for token in tokens:
        if not re.fullmatch(r"(?:c-|s-|a-)?[a-z0-9]+", token):
            raise ValueError(f"Invalid key token '{token}'")
    try:
        KeyBindings().add(*tokens)(lambda _event: None)
    except ValueError as exc:
        raise ValueError(f"Invalid key sequence '{sequence}'") from exc


def _model_list(value, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for item in value:
        try:
            model_id = _validate_model_id(item)
        except ValueError:
            continue
        if model_id not in result:
            result.append(model_id)
        if len(result) >= limit:
            break
    return tuple(result)


def _validate_model_id(value: object) -> str:
    model_id = str(value or "").strip()
    if "/" not in model_id or model_id.startswith("/") or model_id.endswith("/"):
        raise ValueError("Model must use PROVIDER/MODEL")
    if len(model_id) > 512 or any(character in model_id for character in "\r\n\0"):
        raise ValueError("Invalid model identifier")
    return model_id


def _palette(theme_name: str) -> dict[str, str]:
    return _THEMES.get(theme_name, _THEMES["nzcoder"])


def _preference_path(workspace: Path | None) -> Path:
    root = (workspace or current_workdir()).resolve()
    return prepare_user_storage(root).workspace_state / _STATE_PATH


def _write_preferences(path: Path, state: TerminalPreferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    harden_private_path(path.parent)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, **asdict(state)}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        temp_path.replace(path)
        path.chmod(0o600)
        harden_private_path(path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
