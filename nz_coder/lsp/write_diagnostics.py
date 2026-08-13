"""Best-effort LSP diagnostics for files changed by a committed write batch."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

from nz_coder import config

from .manager import get_client_for_file

_SEVERITY_NAMES = {
    1: "error",
    2: "warning",
    3: "information",
    4: "hint",
}


def _safe_targets(paths: Iterable[str], workspace: Path) -> list[tuple[str, Path]]:
    root = workspace.resolve()
    targets: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for raw_path in paths:
        value = str(raw_path or "").strip()
        if not value:
            continue
        candidate = Path(value)
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        if target in seen:
            continue
        seen.add(target)
        targets.append((relative.as_posix(), target))
    return targets


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, object], value)


def _format_diagnostic(path: str, item: Mapping[str, object]) -> str:
    range_data = _mapping(item.get("range"))
    start = _mapping(range_data.get("start"))
    try:
        line = max(0, int(str(start.get("line", 0)))) + 1
    except (TypeError, ValueError):
        line = 1
    try:
        character = max(0, int(str(start.get("character", 0)))) + 1
    except (TypeError, ValueError):
        character = 1
    try:
        severity_value = int(str(item.get("severity", 0) or 0))
    except (TypeError, ValueError):
        severity_value = 0
    severity = _SEVERITY_NAMES.get(severity_value, "diagnostic")
    message = " ".join(str(item.get("message") or "").split())
    source = str(item.get("source") or "").strip()
    code = str(item.get("code") or "").strip()
    origin = "/".join(part for part in (source, code) if part)
    suffix = f" ({origin})" if origin else ""
    return f"- {path}:{line}:{character} [{severity}] {message}{suffix}"


def collect_write_diagnostics(paths: Iterable[str], workspace: Path) -> str:
    """Synchronize committed changes and return a compact diagnostic block.

    Missing servers, unsupported files, protocol errors, and diagnostics timeouts
    are ignored so an optional LSP can never fail a file write.
    """
    if not config.LSP_ENABLED or not config.LSP_WRITE_DIAGNOSTICS_ENABLED:
        return ""

    targets = _safe_targets(paths, workspace)
    max_files = max(1, int(config.LSP_WRITE_DIAGNOSTIC_MAX_FILES or 1))
    omitted = max(0, len(targets) - max_files)
    targets = targets[:max_files]
    rows: list[str] = []

    for relative, target in targets:
        try:
            client = get_client_for_file(target, workspace)
            if client is None:
                continue
            if not target.is_file():
                client.close_document(target)
                continue
            diagnostics = client.diagnostics(target)
        except Exception:
            continue
        for item in diagnostics:
            rows.append(_format_diagnostic(relative, item))

    if not rows:
        return ""
    if omitted:
        rows.append(f"- ... skipped diagnostics for {omitted} additional changed file(s)")

    heading = (
        "<lsp-diagnostics>\n"
        "Diagnostics reported after the write transaction committed:\n"
    )
    closing = "\n</lsp-diagnostics>"
    body = "\n".join(rows)
    limit = max(500, int(config.LSP_MAX_OUTPUT_CHARS or 500))
    available = max(0, limit - len(heading) - len(closing))
    if len(body) > available:
        marker = "\n... [LSP diagnostics truncated]"
        body = body[:max(0, available - len(marker))] + marker
    return heading + body + closing
