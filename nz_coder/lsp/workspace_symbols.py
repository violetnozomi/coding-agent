"""Best-effort LSP workspace-symbol enrichment for repository maps."""
# pyright: reportAny=false, reportUnknownMemberType=false
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nz_coder.lsp.client import uri_to_path
from nz_coder.lsp.manager import get_client_for_file
from nz_coder.tools.repo_ranking import MatchRank, rank_repo_symbol


_SYMBOL_KIND_NAMES = {
    5: "class",
    6: "method",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    23: "struct",
}

_WORKSPACE_SYMBOL_MAX = 10
_WORKSPACE_SYMBOL_WARMUP_SECONDS = 0.5


@dataclass(frozen=True)
class WorkspaceSymbolEntry:
    """One useful workspace symbol returned by a language server."""

    path: str
    name: str
    kind: str
    line: int
    character: int
    container: str = ""


@dataclass(frozen=True)
class WorkspaceSymbolResult:
    """A bounded semantic-symbol result with a non-fatal status notice."""

    source: str
    symbols: tuple[WorkspaceSymbolEntry, ...]
    notice: str = ""


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast("Mapping[str, object]", value)


def _position(location: Mapping[str, object]) -> tuple[int, int] | None:
    range_value = _mapping(location.get("range"))
    if range_value is None:
        return None
    start = _mapping(range_value.get("start"))
    if start is None:
        return None
    line = start.get("line")
    character = start.get("character")
    if not isinstance(line, int) or not isinstance(character, int):
        return None
    if line < 0 or character < 0:
        return None
    return line + 1, character + 1


def _relative_symbol_path(
    uri: object,
    *,
    workspace: Path,
    base: Path,
) -> str | None:
    if not isinstance(uri, str):
        return None
    path = uri_to_path(uri)
    if path is None:
        return None
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(workspace).as_posix()
        if base.is_file():
            if resolved != base:
                return None
        else:
            _ = resolved.relative_to(base)
    except (OSError, ValueError):
        return None
    return relative


def _parse_symbol(
    value: object,
    *,
    workspace: Path,
    base: Path,
) -> WorkspaceSymbolEntry | None:
    symbol = _mapping(value)
    if symbol is None:
        return None
    name = symbol.get("name")
    kind = symbol.get("kind")
    location = _mapping(symbol.get("location"))
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(kind, int)
        or kind not in _SYMBOL_KIND_NAMES
        or location is None
    ):
        return None
    path = _relative_symbol_path(
        location.get("uri"),
        workspace=workspace,
        base=base,
    )
    position = _position(location)
    if path is None or position is None:
        return None
    container = symbol.get("containerName")
    return WorkspaceSymbolEntry(
        path=path,
        name=name.strip(),
        kind=_SYMBOL_KIND_NAMES[kind],
        line=position[0],
        character=position[1],
        container=container.strip() if isinstance(container, str) else "",
    )


def _rank(entry: WorkspaceSymbolEntry, query: str) -> MatchRank:
    qualified_name = (
        f"{entry.container}.{entry.name}"
        if entry.container
        else entry.name
    )
    rank = rank_repo_symbol(
        path=entry.path,
        symbol_name=entry.name,
        qualified_name=qualified_name,
        signature="",
        query=query,
    )
    # The language server already filtered workspace/symbol results. Preserve
    # unmatched server candidates as a weakest fallback, matching InfCode.
    return rank if rank is not None else (8, 8, 1)


def collect_workspace_symbols(
    *,
    probe: Path,
    workspace: Path,
    base: Path,
    query: str,
    limit: int,
) -> WorkspaceSymbolResult:
    """Collect bounded, in-scope workspace symbols without failing Repo Map."""
    try:
        client = get_client_for_file(probe, workspace)
        if client is None:
            return WorkspaceSymbolResult(
                source="",
                symbols=(),
                notice="LSP semantic enrichment unavailable",
            )
        from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

        relative = probe.resolve().relative_to(workspace.resolve()).as_posix()
        text, identity = WorkspaceFileAccess(workspace).read_text_with_identity(
            relative, errors="replace",
        )
        _ = client.open_document(probe, text, identity)
        response = cast(
            object,
            client.request("workspace/symbol", {"query": query.strip()}),
        )
        if isinstance(response, list) and not response and query.strip():
            # A freshly started server can accept requests before its index is
            # ready. Retry once so the first semantic Repo Map is useful.
            time.sleep(_WORKSPACE_SYMBOL_WARMUP_SECONDS)
            response = cast(
                object,
                client.request("workspace/symbol", {"query": query.strip()}),
            )
        if not isinstance(response, list):
            return WorkspaceSymbolResult(
                source=f"lsp/{client.server_id}",
                symbols=(),
                notice="language server returned no workspace symbols",
            )

        unique: dict[
            tuple[str, str, str, int, int],
            WorkspaceSymbolEntry,
        ] = {}
        for value in cast("list[object]", response):
            entry = _parse_symbol(value, workspace=workspace, base=base)
            if entry is None:
                continue
            key = (
                entry.path,
                entry.name,
                entry.kind,
                entry.line,
                entry.character,
            )
            _ = unique.setdefault(key, entry)

        ordered = sorted(
            unique.values(),
            key=lambda entry: (
                _rank(entry, query),
                entry.path.casefold(),
                entry.name.casefold(),
                entry.line,
                entry.character,
            ),
        )
        bounded_limit = max(1, min(int(limit), _WORKSPACE_SYMBOL_MAX))
        truncated = len(ordered) > bounded_limit
        symbols = tuple(ordered[:bounded_limit])
        notice = (
            f"LSP symbols truncated at {bounded_limit}"
            if truncated
            else ""
        )
        return WorkspaceSymbolResult(
            source=f"lsp/{client.server_id}",
            symbols=symbols,
            notice=notice,
        )
    except Exception:
        return WorkspaceSymbolResult(
            source="",
            symbols=(),
            notice="LSP semantic enrichment unavailable",
        )


def format_workspace_symbols(result: WorkspaceSymbolResult) -> list[str]:
    """Format a semantic supplement for the text repository map."""
    rows: list[str] = []
    if result.source:
        rows.extend([
            "",
            "LSP workspace symbols",
            f"semantic_source: {result.source}, symbols: {len(result.symbols)}",
        ])
        for symbol in result.symbols:
            qualified_name = (
                f"{symbol.container}.{symbol.name}"
                if symbol.container
                else symbol.name
            )
            rows.append(
                f"  {symbol.path}:{symbol.line}:{symbol.character} | "
                + f"{symbol.kind} {qualified_name}"
            )
    if result.notice:
        rows.append(f"semantic_notice: {result.notice}")
    return rows
