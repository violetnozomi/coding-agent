"""Multi-language repository structure map with incremental indexing."""
# pyright: reportUnknownVariableType=false
from __future__ import annotations

from pathlib import Path

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.protocol.public_error import format_public_error
from nz_coder.intelligence.code_index import (
    AmbiguousSymbolError,
    FileEntry,
    PersistentCodeIndex,
    SymbolEntry,
)
from nz_coder.intelligence.service import workspace_repo_intelligence
from nz_coder.lsp.workspace_symbols import (
    collect_workspace_symbols,
    format_workspace_symbols,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tools import register
from nz_coder.tools.repo_languages import is_supported_source
from nz_coder.tools.repo_ranking import MatchRank, rank_repo_symbol


def _safe_path(path: str) -> tuple[Path, Path]:
    workspace = current_workdir().resolve()
    target = WorkspacePathPolicy(workspace).validate_model_read(path or ".")
    return workspace, target


def _build_index(
    workspace: Path,
    base: Path,
    *,
    max_files: int,
    refresh: bool,
) -> tuple[list[FileEntry], int, int]:
    existing_service = workspace_repo_intelligence(workspace, create=False)
    service = existing_service or workspace_repo_intelligence(
        workspace, max_files=max_files,
    )
    index = service.index if service is not None else PersistentCodeIndex(workspace)
    was_ready = bool(
        existing_service is not None and service is not None
        and service.state.status == "ready"
    )
    if service is not None and not refresh and service.state.status == "warming":
        try:
            service.wait_ready(timeout=0.1)
        except TimeoutError:
            pass
    if service is not None and not refresh and service.state.status == "ready":
        snapshot = index.snapshot()
        entries = list(snapshot.files)
        if base != workspace:
            relative = base.relative_to(workspace).as_posix()
            entries = [
                entry for entry in entries
                if entry.path == relative or entry.path.startswith(relative.rstrip("/") + "/")
            ]
        omitted = max(0, len(entries) - max_files)
        return (
            entries[:max_files], len(entries[:max_files]) if was_ready else 0,
            max(omitted, service.state.files_omitted),
        )
    entries, stats = index.scan(
        base,
        max_files=max_files,
        refresh=refresh,
    )
    return entries, stats.reused, stats.omitted


def code_references(
    symbol: str,
    path: str = ".",
    max_results: int = 100,
    refresh: bool = False,
) -> str:
    """Find exact Python identifier references in the persistent code index."""
    try:
        name = symbol.strip()
        valid_query = (
            name.startswith("symbol:")
            or all(part.isidentifier() for part in name.split("."))
        )
        if not name or not valid_query:
            return "Error: symbol must be a symbol_id, identifier, or qualified name"
        workspace, base = _safe_path(path)
        if not base.exists():
            return f"Error: path not found: {path}"
        service = workspace_repo_intelligence(
            workspace, max_files=max(1, min(current_run_settings().repo_map_max_files, 500)),
        )
        index = service.index if service is not None else PersistentCodeIndex(workspace)
        entries, reused, _omitted = _build_index(
            workspace, base,
            max_files=max(1, min(current_run_settings().repo_map_max_files, 500)),
            refresh=bool(refresh),
        )
        limit = max(1, min(int(max_results), 1000))
        references = index.references(name, base, limit)
        if not references:
            return f"No indexed references found for {name!r} under {path!r}"
        rows = [
            f"References for {name!r} (results: {len(references)}, cache_hits: {reused})"
        ]
        rows.extend(
            f"{item.path}:{item.line}:{item.column + 1} | {item.context}"
            for item in references
        )
        if len(references) >= limit:
            rows.append(f"notice: output limited to max_results={limit}")
        return "\n".join(rows)
    except AmbiguousSymbolError as exc:
        alternatives = ", ".join(
            str(item["symbol_id"]) for item in exc.alternatives[:8]
        )
        return format_public_error(exc, context=f"Candidates: {alternatives}. ")
    except ValueError as exc:
        return format_public_error(exc)
    except Exception as exc:
        return format_public_error(exc)


def _rank_symbol(
    entry: FileEntry,
    symbol: SymbolEntry,
    query: str,
) -> MatchRank | None:
    return rank_repo_symbol(
        path=entry.path,
        symbol_name=symbol.name,
        qualified_name=symbol.qualified_name,
        signature=symbol.signature,
        query=query,
    )


def repo_map(
    path: str = ".",
    query: str = "",
    max_files: int = 0,
    max_symbols: int = 0,
    refresh: bool = False,
    semantic: bool = False,
) -> str:
    """Return a compact multi-language repository structure map."""
    try:
        workspace, base = _safe_path(path)
        if not base.exists():
            return f"Error: path not found: {path}"
        if base.is_file() and not is_supported_source(base):
            return f"Error: repo_map does not support source file: {path}"

        settings = current_run_settings()
        file_limit = int(max_files or settings.repo_map_max_files)
        symbol_limit = int(max_symbols or settings.repo_map_max_symbols)
        file_limit = max(1, min(file_limit, 500))
        symbol_limit = max(1, min(symbol_limit, 5000))
        entries, reused, omitted = _build_index(
            workspace,
            base,
            max_files=file_limit,
            refresh=bool(refresh),
        )
        if not entries:
            return f"No supported source files found under {path!r}"

        ranked_files: list[
            tuple[MatchRank, FileEntry, list[tuple[MatchRank, SymbolEntry]]]
        ] = []
        parse_errors: list[str] = []
        for entry in entries:
            if entry.parse_error:
                parse_errors.append(f"{entry.path}: {entry.parse_error}")
                continue
            module_prefix = entry.path.rsplit(".", 1)[0].replace("/", ".") + "."
            ranked_symbols = []
            for symbol in entry.symbols:
                local_name = symbol.qualified_name.removeprefix(module_prefix)
                if "function" in symbol.kind and "." in local_name:
                    continue
                rank = _rank_symbol(entry, symbol, query)
                if rank is not None:
                    ranked_symbols.append((rank, symbol))
            if not ranked_symbols:
                continue
            ranked_symbols.sort(
                key=lambda item: (
                    item[0],
                    item[1].qualified_name.casefold(),
                    item[1].line,
                )
            )
            ranked_files.append((ranked_symbols[0][0], entry, ranked_symbols))
        ranked_files.sort(key=lambda item: (item[0], item[1].path.casefold()))

        rows: list[str] = []
        shown_symbols = 0
        matched_files = 0
        truncated = False
        for _, entry, ranked_symbols in ranked_files:
            matched_files += 1
            rows.append(f"\n{entry.path}:")
            for _, symbol in ranked_symbols:
                if shown_symbols >= symbol_limit:
                    truncated = True
                    break
                module_prefix = entry.path.rsplit(".", 1)[0].replace("/", ".") + "."
                display_name = symbol.qualified_name.removeprefix(module_prefix)
                indent = "  " if "." in display_name else ""
                row = (
                    f"  {symbol.line}-{symbol.end_line} | {indent}"
                    f"{symbol.kind} {display_name}: {symbol.signature}"
                )
                rows.append(row)
                shown_symbols += 1
            if truncated:
                break

        semantic_rows: list[str] = []
        if semantic:
            probe_entry = ranked_files[0][1] if ranked_files else entries[0]
            semantic_result = collect_workspace_symbols(
                probe=workspace / probe_entry.path,
                workspace=workspace,
                base=base,
                query=query,
                limit=symbol_limit,
            )
            semantic_rows = format_workspace_symbols(semantic_result)

        if not rows and not semantic_rows:
            suffix = f" matching {query!r}" if query else ""
            return f"No source definitions found under {path!r}{suffix}"

        languages = sorted({entry.language for entry in entries})
        title = (
            "Python repository map"
            if languages == ["python"]
            else "Source repository map"
        )
        header = [
            title,
            (
                f"files_scanned: {len(entries)}, files_matched: {matched_files}, "
                f"symbols: {shown_symbols}, cache_hits: {reused}"
            ),
            f"languages: {', '.join(languages)}",
        ]
        if query:
            header.append(f"query: {query}")
        if omitted:
            header.append(
                f"notice: skipped {omitted} file(s) beyond max_files={file_limit}"
            )
        if truncated:
            header.append(f"notice: output truncated at max_symbols={symbol_limit}")
        if parse_errors:
            header.append(
                f"notice: skipped {len(parse_errors)} unparsable/oversized file(s)"
            )
            header.extend(f"  - {item}" for item in parse_errors[:5])
        return "\n".join(header + rows + semantic_rows)
    except ValueError as exc:
        return format_public_error(exc)
    except Exception as exc:
        return format_public_error(exc)


register(
    name="repo_map",
    description=(
        "Build a compact cross-file source map. Python uses the standard-library "
        "AST; TypeScript/JavaScript, Go, Rust, Java, Kotlin, C/C++, Ruby, PHP, "
        "Lua, and shell use conservative declaration extraction. Optionally "
        "supplement it with installed-LSP workspace symbols."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative supported source file or directory.",
            },
            "query": {
                "type": "string",
                "description": "Optional space-separated filter for paths or definitions.",
            },
            "max_files": {
                "type": "integer",
                "description": "Maximum supported source files to scan.",
            },
            "max_symbols": {
                "type": "integer",
                "description": "Maximum structural definitions to return.",
            },
            "refresh": {
                "type": "boolean",
                "description": "Force reparsing instead of reusing unchanged cached entries.",
            },
            "semantic": {
                "type": "boolean",
                "description": (
                    "Add best-effort LSP workspace symbols. Default: false; "
                    "unavailable servers do not fail the structural map."
                ),
            },
        },
    },
    handler=repo_map,
    execution="read",
)

register(
    name="code_references",
    description=(
        "Find exact structural identifier uses from the workspace-persistent SQLite "
        "code index. The index is reused across NZ-Coder process restarts and "
        "refreshed incrementally after committed writes and watcher events. Python "
        "uses AST references; supported non-Python languages use explicit lower-confidence lexical references."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Exact Python identifier to find.",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative source file or directory.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum references to return.",
            },
            "refresh": {
                "type": "boolean",
                "description": "Force a complete reparse of files in path.",
            },
        },
        "required": ["symbol"],
    },
    handler=code_references,
    execution="read",
)
