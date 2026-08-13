"""Semantic, color-independent terminal presentation primitives."""
from __future__ import annotations

import ntpath
from pathlib import Path
import re
import unicodedata


STATUS_LABELS = {
    "idle": "IDLE",
    "running": "RUNNING",
    "waiting": "WAITING",
    "error": "ERROR",
    "interrupted": "INTERRUPTED",
}


def responsive_band(width: int) -> str:
    """Return the stable responsive layout band for a terminal width."""
    value = int(width)
    return "narrow" if value < 80 else "normal" if value <= 120 else "wide"


def _is_cluster_suffix(character: str) -> bool:
    value = ord(character)
    return (
        bool(unicodedata.combining(character))
        or 0xFE00 <= value <= 0xFE0F
        or 0xE0100 <= value <= 0xE01EF
        or 0x1F3FB <= value <= 0x1F3FF
    )


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _terminal_clusters(text: str):  # noqa: ANN202
    """Yield conservative grapheme-like units without a browser-sized engine."""
    current = ""
    for character in str(text):
        if not current:
            current = character
            continue
        regional_pair = (
            len(current) == 1
            and _is_regional_indicator(current)
            and _is_regional_indicator(character)
        )
        if (
            _is_cluster_suffix(character)
            or character == "\u200d"
            or current.endswith("\u200d")
            or regional_pair
        ):
            current += character
            continue
        yield current
        current = character
    if current:
        yield current


def _character_columns(character: str) -> int:
    if character in {"\u200c", "\u200d"} or _is_cluster_suffix(character):
        return 0
    category = unicodedata.category(character)
    if category.startswith("C"):
        return 0
    if _is_regional_indicator(character) or ord(character) >= 0x1F300:
        return 2
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def terminal_text_width(text: str) -> int:
    """Return the conservative terminal-column width of Unicode text."""
    total = 0
    for cluster in _terminal_clusters(str(text)):
        widths = [_character_columns(character) for character in cluster]
        if "\u200d" in cluster or (
            len(cluster) == 2 and all(_is_regional_indicator(char) for char in cluster)
        ):
            total += max(widths, default=0)
        else:
            total += sum(widths)
    return total


def clip_terminal_text(text: str, max_columns: int) -> str:
    """Clip text by terminal columns without splitting a combining/ZWJ cluster."""
    value = str(text or "")
    limit = max(0, int(max_columns))
    if limit == 0:
        return ""
    if terminal_text_width(value) <= limit:
        return value
    if limit == 1:
        return "…"
    selected: list[str] = []
    used = 0
    content_limit = limit - 1
    for cluster in _terminal_clusters(value):
        width = terminal_text_width(cluster)
        if used + width > content_limit:
            break
        selected.append(cluster)
        used += width
    return "".join(selected) + "…"


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return clip_terminal_text(text, limit)


def _workspace_label(value: str) -> str:
    selected = str(value or "").rstrip("/\\")
    if not selected:
        return "-"
    if "\\" in selected or ntpath.splitdrive(selected)[0]:
        return ntpath.basename(selected) or selected
    return Path(selected).name or selected


def _session_label(state: dict) -> str:
    session_id = str(state.get("session") or "-")
    tail = next((part for part in reversed(re.split(r"[-_]", session_id)) if part), session_id)
    short_id = tail[-4:] if len(tail) > 4 else tail
    title = " ".join(str(state.get("session_title") or "").split())
    return f"{title} · {short_id}" if title else short_id


def build_header(state: dict, *, width: int) -> str:
    """Build a compact header with explicit execution location and state."""
    band = responsive_band(width)
    status = STATUS_LABELS.get(str(state.get("run_state") or "idle").lower(), "IDLE")
    location = str(state.get("location") or "LOCAL")
    model = str(state.get("model") or "-")
    mode = str(state.get("mode") or "-")
    session = _session_label(state)
    workspace = _workspace_label(str(state.get("workspace") or ""))
    if band == "narrow":
        raw = f" NZ-Coder · {status} · {location} · {model} "
    elif band == "normal":
        raw = f" NZ-Coder · {status} · {location} · {workspace} · {model} · {mode} · {session} "
    else:
        raw = f" NZ-Coder · {status} · {location} · {workspace} · {model} · {mode} · {session} "
    return _clip(raw, max(20, width))


def compact_status(state: dict, *, width: int) -> str:
    """Build the input-adjacent status line, hiding secondary narrow metadata."""
    values = [str(state.get("model") or "-")]
    if state.get("context"):
        values.append(str(state["context"]))
    if responsive_band(width) != "narrow":
        if state.get("branch"):
            values.append(str(state["branch"]))
        if state.get("changed") not in {None, "", "0", 0}:
            values.append(f"{state['changed']} changed")
        if state.get("processes") not in {None, "", "0", 0}:
            values.append(f"{state['processes']} processes")
    return _clip(" · ".join(values), max(20, width))


def build_empty_state(state: dict) -> str:
    """Build short first-use guidance or the provider connection recovery path."""
    workspace = str(state.get("workspace") or Path.cwd())
    model = str(state.get("model") or "-")
    lines = ["NZ-Coder", "", "Working in:", workspace, "", "Model:", model, ""]
    if not bool(state.get("provider_configured", True)):
        lines.extend(("No model provider configured.", "Run /connect to connect a provider."))
    else:
        lines.extend((
            "Try:",
            "> Fix the failing tests",
            "",
            "/help  Commands   @  Files   Ctrl+K  Command palette",
        ))
    return "\n".join(lines)


def attachment_chips(paths, *, width: int) -> str:  # noqa: ANN001
    """Render bounded text chips; file paths never become hidden temp paths."""
    chips: list[str] = []
    for raw in paths:
        value = str(getattr(raw, "path", raw))
        normalized = value.replace("\\", "/")
        label = (
            "clipboard image"
            if "/.nz-coder/attachments/clipboard-" in f"/{normalized}"
            else normalized
        )
        chip = f"[{label}]"
        candidate = " ".join((*chips, chip))
        if terminal_text_width(candidate) > max(12, int(width)):
            if not chips:
                return _clip(chip, max(12, int(width)))
            break
        chips.append(chip)
    return " ".join(chips)


def activity_label(tool: str, title: str = "") -> str:
    """Map internal tool names to user-understandable Agent activity."""
    name = str(tool or "").lower()
    subject = _clip(title, 100)
    if any(value in name for value in ("grep", "search", "glob", "repo_map", "symbol")):
        return "Searching codebase..."
    if name.startswith(("read", "list", "lsp")):
        return f"Reading · {subject}" if subject else "Reading..."
    if any(value in name for value in ("edit", "write", "patch", "replace")):
        return f"Editing · {subject}" if subject else "Editing..."
    if name in {"verify_changed_files", "verification", "diagnostics"}:
        return f"Verifying · {subject}" if subject else "Verifying..."
    if name in {"process_read", "task", "workflow_wait"}:
        return f"Waiting for process · {subject}" if subject else "Waiting for child..."
    if name in {"bash", "shell", "process"} and any(
        token in subject.lower() for token in ("pytest", "test", "ruff", "mypy", "npm test")
    ):
        return f"Running tests · {subject}"
    if name in {"bash", "shell", "process"}:
        return f"Running · {subject}" if subject else "Running command..."
    return f"Thinking · {subject}" if subject else "Thinking..."


__all__ = [
    "STATUS_LABELS",
    "activity_label",
    "attachment_chips",
    "build_empty_state",
    "build_header",
    "compact_status",
    "clip_terminal_text",
    "responsive_band",
    "terminal_text_width",
]
