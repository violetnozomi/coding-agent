"""Persistent identity-based repository symbol, reference, and call index."""
from __future__ import annotations

import ast as _ast
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from nz_coder import config
from nz_coder.intelligence.analyzers import (
    AnalysisResult,
    AnalyzerRegistry,
    CapabilityTier,
    ImportRecord,
    SymbolRecord,
    discover_module_boundaries,
    module_id_for_path,
    module_name_for_path,
)
from nz_coder.lsp.servers import language_for_path


EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".mypy_cache", ".nz-coder", ".nz-coder-runs",
    ".pytest_cache", ".ruff_cache", ".svn", ".tox", ".venv",
    "__pycache__", "build", "dist", "node_modules", "site-packages", "venv",
})
SCHEMA_VERSION = 3
# Compatibility hook: analyzers share this stdlib module object and historical
# tests/extensions patch ``code_index.ast.parse`` to observe AST cache reuse.
ast = _ast
_SUPPORTED_LANGUAGES = frozenset({
    "python", "typescript", "javascript", "go", "rust", "java", "kotlin",
    "cpp", "ruby", "php", "lua", "bash",
})


def intent_tokens(value: str) -> tuple[str, ...]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    return tuple(dict.fromkeys(
        item.casefold() for item in re.findall(r"[A-Za-z0-9]+", expanded.replace("_", " "))
        if len(item) > 1
    ))


def structural_match_score(query: str, *candidates: str | None) -> float:
    normalized_query = " ".join(intent_tokens(query))
    query_terms = intent_tokens(query)
    if not normalized_query or not query_terms:
        return 0.0
    best = 0.0
    for candidate in candidates:
        if not candidate:
            continue
        normalized = " ".join(intent_tokens(str(candidate)))
        if not normalized:
            continue
        if normalized == normalized_query:
            score = 1.0
        elif normalized.startswith(normalized_query):
            score = 0.92
        elif normalized_query in normalized:
            score = 0.82
        else:
            matched = sum(term in normalized for term in query_terms)
            if not matched:
                continue
            coverage = matched / len(query_terms)
            score = 0.38 + coverage * 0.38
            if all(term in normalized for term in query_terms):
                score += 0.08
        best = max(best, min(1.0, score))
    return round(best, 4)


def _is_supported_source(path: Path) -> bool:
    return (language_for_path(path) or "") in _SUPPORTED_LANGUAGES


def _index_language(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if suffix in {".ts", ".tsx", ".mts", ".cts"}:
        return "typescript"
    return language_for_path(path) or "unknown"


@dataclass(frozen=True)
class SymbolEntry:
    """A stable repository declaration (legacy fields remain first)."""

    kind: str
    name: str
    qualified_name: str
    line: int
    end_line: int
    signature: str | None
    symbol_id: str = ""
    file_path: str = ""
    module_id: str = ""
    language: str = ""
    exported: bool | None = None
    confidence: float = 0.0
    source: str = ""
    capability_tier: str = CapabilityTier.LEXICAL_FALLBACK.value


@dataclass(frozen=True)
class ReferenceEntry:
    source_file: str
    source_symbol_id: str | None
    raw_name: str
    target_symbol_id: str | None
    qualifier: str
    line: int
    column: int
    context: str
    resolution_kind: str
    confidence: float
    source: str
    candidates: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.source_file

    @property
    def name(self) -> str:
        return self.raw_name

    @property
    def unresolved_target(self) -> dict | None:
        if self.target_symbol_id:
            return None
        return {
            "raw_name": self.raw_name, "qualifier": self.qualifier,
            "candidates": list(self.candidates), "confidence": self.confidence,
        }

    def to_dict(self) -> dict:
        value = asdict(self)
        value["path"] = self.source_file
        value["name"] = self.raw_name
        value["unresolved_target"] = self.unresolved_target
        return value


class AmbiguousSymbolError(LookupError):
    """Raised when an identity query receives a non-unique name."""

    def __init__(self, identifier: str, alternatives: tuple[dict, ...]) -> None:
        self.identifier = identifier
        self.alternatives = alternatives
        super().__init__(
            f"Ambiguous symbol {identifier!r}; use symbol_id or qualified name"
        )


@dataclass(frozen=True)
class ImportEntry:
    path: str
    module: str
    binding: str
    imported_name: str | None
    alias: str | None
    line: int
    kind: str
    confidence: float


@dataclass(frozen=True)
class UnresolvedCallTarget:
    raw_name: str
    qualifier: str = ""
    candidates: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class CallEdge:
    """A resolved identity edge or a retained unresolved call site."""

    caller: str
    callee: str
    path: str
    line: int
    confidence: float = 1.0
    source: str = "python-ast"
    caller_symbol_id: str = ""
    callee_symbol_id: str | None = None
    unresolved_target: UnresolvedCallTarget | None = None
    qualifier: str = ""
    resolution_kind: str = "unresolved"


@dataclass(frozen=True)
class FileEntry:
    path: str
    language: str
    fingerprint: tuple[int, int]
    symbols: tuple[SymbolEntry, ...]
    parse_error: str = ""
    module_id: str = ""
    capability_tier: str = CapabilityTier.LEXICAL_FALLBACK.value
    confidence: float = 0.0
    source: str = ""
    imports: tuple[ImportEntry, ...] = ()


@dataclass(frozen=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    reused: int = 0
    removed: int = 0
    omitted: int = 0
    generation: int = 0
    calls_resolved: int = 0
    references_resolved: int = 0


@dataclass(frozen=True)
class IndexSnapshot:
    generation: int
    files: tuple[FileEntry, ...]
    calls: tuple[CallEdge, ...]
    created_at: float


@dataclass(frozen=True)
class ProcessStep:
    kind: str
    symbol_id: str | None
    symbol: str
    file_path: str
    line: int | None = None
    call_site_file: str | None = None
    resolution_kind: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProcessCapsule:
    process_id: str
    label: str
    entry_symbol_id: str
    entry_file: str
    module_ids: tuple[str, ...]
    steps: tuple[ProcessStep, ...]
    related_tests: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict:
        value = asdict(self)
        value["module_ids"] = list(value["module_ids"])
        value["steps"] = [item.to_dict() for item in self.steps]
        value["related_tests"] = list(value["related_tests"])
        return value


@dataclass(frozen=True)
class CallResolutionRequest:
    call_id: int
    file_path: str
    line: int
    raw_name: str
    qualifier: str
    caller_symbol_id: str


@dataclass(frozen=True)
class ResolvedCallLocation:
    file_path: str
    line: int
    symbol_id: str | None = None
    name: str = ""
    confidence: float = 0.9
    source: str = "lsp-definition"


class CallTargetResolver(Protocol):
    def resolve(self, request: CallResolutionRequest) -> ResolvedCallLocation | None: ...


@dataclass(frozen=True)
class CallAugmentationStats:
    attempted: int = 0
    resolved: int = 0
    failed: int = 0
    truncated: bool = False
    generation: int = 0
    duration_ms: float = 0.0


_LOCK_GUARD = threading.Lock()
_DB_LOCKS: dict[Path, threading.RLock] = {}


def _database_lock(path: Path) -> threading.RLock:
    with _LOCK_GUARD:
        return _DB_LOCKS.setdefault(path, threading.RLock())


class PersistentCodeIndex:
    """SQLite-backed index isolated to one resolved workspace."""

    def __init__(self, workspace: Path, *, analyzers: AnalyzerRegistry | None = None):
        self.workspace = Path(workspace).resolve()
        state_dir = self.workspace / ".nz-coder"
        if state_dir.exists():
            try:
                state_dir.resolve().relative_to(self.workspace)
            except ValueError as exc:
                raise ValueError("Code index path escapes workspace") from exc
        state_dir.mkdir(parents=False, exist_ok=True)
        index_dir = state_dir / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        try:
            index_dir.resolve().relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError("Code index path escapes workspace") from exc
        self.database_path = index_dir.resolve() / "code-index.sqlite3"
        self._lock = _database_lock(self.database_path)
        self.analyzers = analyzers or AnalyzerRegistry()
        self._module_boundaries = discover_module_boundaries(self.workspace)
        with self._connect() as connection:
            self._create_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode = WAL")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if 0 < version < SCHEMA_VERSION:
            # This database is a disposable derived cache.  A schema rebuild is
            # safer than preserving name-only edges as if they were identities.
            connection.executescript(
                "DROP TABLE IF EXISTS calls; DROP TABLE IF EXISTS refs; "
                "DROP TABLE IF EXISTS imports; DROP TABLE IF EXISTS symbols; "
                "DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS metadata;"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                language TEXT NOT NULL,
                module_id TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                parse_error TEXT NOT NULL DEFAULT '',
                capability_tier TEXT NOT NULL,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                indexed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS symbols (
                symbol_id TEXT PRIMARY KEY,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                module_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                signature TEXT,
                exported INTEGER,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                capability_tier TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS refs (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                source_symbol_id TEXT,
                raw_name TEXT NOT NULL,
                qualifier TEXT NOT NULL DEFAULT '',
                target_symbol_id TEXT,
                line INTEGER NOT NULL,
                column_no INTEGER NOT NULL,
                context TEXT NOT NULL,
                resolution_kind TEXT NOT NULL DEFAULT 'unresolved',
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                candidates_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                module TEXT NOT NULL,
                binding TEXT NOT NULL,
                imported_name TEXT,
                alias TEXT,
                line INTEGER NOT NULL,
                kind TEXT NOT NULL,
                confidence REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                caller_symbol_id TEXT NOT NULL,
                caller_name TEXT NOT NULL,
                raw_name TEXT NOT NULL,
                qualifier TEXT NOT NULL DEFAULT '',
                callee_symbol_id TEXT,
                line INTEGER NOT NULL,
                resolution_kind TEXT NOT NULL DEFAULT 'unresolved',
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                candidates_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name);
            CREATE INDEX IF NOT EXISTS symbols_qualified_idx ON symbols(qualified_name);
            CREATE INDEX IF NOT EXISTS symbols_path_idx ON symbols(path);
            CREATE INDEX IF NOT EXISTS symbols_module_idx ON symbols(module_id);
            CREATE INDEX IF NOT EXISTS refs_name_idx ON refs(raw_name);
            CREATE INDEX IF NOT EXISTS refs_path_idx ON refs(path);
            CREATE INDEX IF NOT EXISTS refs_source_id_idx ON refs(source_symbol_id);
            CREATE INDEX IF NOT EXISTS refs_target_id_idx ON refs(target_symbol_id);
            CREATE INDEX IF NOT EXISTS imports_path_idx ON imports(path);
            CREATE INDEX IF NOT EXISTS calls_caller_id_idx ON calls(caller_symbol_id);
            CREATE INDEX IF NOT EXISTS calls_callee_id_idx ON calls(callee_symbol_id);
            CREATE INDEX IF NOT EXISTS calls_raw_idx ON calls(raw_name);
            CREATE INDEX IF NOT EXISTS calls_path_idx ON calls(path);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('generation', '0')"
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise ValueError(f"Path escapes workspace: {path}") from exc

    def _source_files(self, base: Path, max_files: int) -> tuple[list[Path], int]:
        candidates: list[Path] = []
        if base.is_file():
            candidates.append(base)
        else:
            for root, dir_names, file_names in os.walk(base, followlinks=False):
                dir_names[:] = sorted(name for name in dir_names if name not in EXCLUDED_DIRS)
                candidates.extend(Path(root) / name for name in sorted(file_names))
        files: list[Path] = []
        omitted = 0
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                relative = resolved.relative_to(self.workspace)
            except (OSError, ValueError):
                continue
            if (
                not resolved.is_file()
                or not _is_supported_source(resolved)
                or any(part in EXCLUDED_DIRS for part in relative.parts)
            ):
                continue
            if len(files) >= max_files:
                omitted += 1
            else:
                files.append(resolved)
        return files, omitted

    def _parse(self, path: Path) -> tuple[FileEntry, AnalysisResult]:
        stat = path.stat()
        relative = self._relative(path)
        language = _index_language(path)
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if stat.st_size > max(1, int(config.REPO_MAP_MAX_FILE_BYTES)):
            empty = AnalysisResult(
                language, CapabilityTier.LEXICAL_FALLBACK.value, 0.0, "size-limit",
                (), (), (), (), f"file exceeds {config.REPO_MAP_MAX_FILE_BYTES} bytes",
            )
        else:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                empty = self.analyzers.analyze_file(
                    path=path, relative=relative, source=source, language=language,
                )
            except (OSError, SyntaxError, ValueError, RuntimeError) as exc:
                empty = AnalysisResult(
                    language, CapabilityTier.LEXICAL_FALLBACK.value, 0.0, "parse-error",
                    (), (), (), (), f"{type(exc).__name__}: {exc}",
                )
        module_id = module_id_for_path(
            relative, boundary_roots=self._module_boundaries,
        )
        if empty.symbols and any(item.module_id != module_id for item in empty.symbols):
            empty = replace(
                empty,
                symbols=tuple(replace(item, module_id=module_id) for item in empty.symbols),
            )
        symbols = tuple(self._symbol_entry(item) for item in empty.symbols)
        imports = tuple(self._import_entry(item) for item in empty.imports)
        entry = FileEntry(
            relative, language, fingerprint, symbols, empty.parse_error,
            module_id, empty.capability_tier, empty.confidence,
            empty.source, imports,
        )
        return entry, empty

    @staticmethod
    def _symbol_entry(record: SymbolRecord) -> SymbolEntry:
        return SymbolEntry(
            record.kind, record.name, record.qualified_name, record.line,
            record.end_line, record.signature, record.symbol_id, record.file_path,
            record.module_id, record.language, record.exported, record.confidence,
            record.source, record.capability_tier,
        )

    @staticmethod
    def _import_entry(record: ImportRecord) -> ImportEntry:
        return ImportEntry(
            record.file_path, record.module, record.binding, record.imported_name,
            record.alias, record.line, record.kind, record.confidence,
        )

    @staticmethod
    def _replace(
        connection: sqlite3.Connection, entry: FileEntry, analysis: AnalysisResult,
    ) -> None:
        connection.execute("DELETE FROM files WHERE path = ?", (entry.path,))
        connection.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.path, entry.language, entry.module_id, entry.fingerprint[0],
                entry.fingerprint[1], entry.parse_error, entry.capability_tier,
                entry.confidence, entry.source, time.time(),
            ),
        )
        connection.executemany(
            "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.symbol_id, item.file_path, item.module_id, item.kind, item.name,
                    item.qualified_name, item.line, item.end_line, item.signature,
                    None if item.exported is None else int(item.exported),
                    item.confidence, item.source, item.capability_tier,
                )
                for item in analysis.symbols
            ],
        )
        connection.executemany(
            "INSERT INTO refs(path, source_symbol_id, raw_name, qualifier, target_symbol_id, "
            "line, column_no, context, resolution_kind, confidence, source, candidates_json) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, 'unresolved', ?, ?, '[]')",
            [
                (
                    item.source_file, item.source_symbol_id, item.raw_name,
                    item.qualifier, item.line, item.column, item.context,
                    item.confidence, item.source,
                )
                for item in analysis.references
            ],
        )
        connection.executemany(
            "INSERT INTO imports(path, module, binding, imported_name, alias, line, kind, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.file_path, item.module, item.binding, item.imported_name,
                    item.alias, item.line, item.kind, item.confidence,
                )
                for item in analysis.imports
            ],
        )
        connection.executemany(
            "INSERT INTO calls(path, caller_symbol_id, caller_name, raw_name, qualifier, "
            "callee_symbol_id, line, resolution_kind, confidence, source, candidates_json) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, 'unresolved', ?, ?, '[]')",
            [
                (
                    item.call_site_file, item.caller_symbol_id, item.caller_name,
                    item.raw_name, item.qualifier, item.line, item.confidence, item.source,
                )
                for item in analysis.calls
            ],
        )

    @staticmethod
    def _generation(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'generation'"
        ).fetchone()
        return int(row[0]) if row else 0

    @classmethod
    def _advance_generation(cls, connection: sqlite3.Connection) -> int:
        generation = cls._generation(connection) + 1
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('generation', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(generation),),
        )
        return generation

    def scan(
        self, base: Path, *, max_files: int, refresh: bool = False,
    ) -> tuple[list[FileEntry], IndexStats]:
        resolved_base = base.resolve()
        self._relative(resolved_base)
        self._module_boundaries = discover_module_boundaries(self.workspace)
        files, omitted = self._source_files(resolved_base, max_files)
        relative_paths = [self._relative(path) for path in files]
        reused = indexed = removed = 0
        affected_paths: set[str] = set()
        affected_names: set[str] = set()
        affected_ids: set[str] = set()
        with self._lock, self._connect() as connection:
            known = {
                row["path"]: (int(row["mtime_ns"]), int(row["size"]))
                for row in connection.execute("SELECT path, mtime_ns, size FROM files")
            }
            for path, relative in zip(files, relative_paths):
                stat = path.stat()
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                if not refresh and known.get(relative) == fingerprint:
                    reused += 1
                    continue
                for row in connection.execute(
                    "SELECT symbol_id, name FROM symbols WHERE path = ?", (relative,),
                ):
                    affected_ids.add(str(row["symbol_id"]))
                    affected_names.add(str(row["name"]))
                entry, analysis = self._parse(path)
                self._replace(connection, entry, analysis)
                affected_paths.add(relative)
                affected_ids.update(item.symbol_id for item in analysis.symbols)
                affected_names.update(item.name for item in analysis.symbols)
                indexed += 1
            if omitted == 0:
                prefix = self._relative(resolved_base)
                stale = []
                for stored in known:
                    in_scope = stored == prefix if resolved_base.is_file() else (
                        prefix in ("", ".") or stored.startswith(prefix.rstrip("/") + "/")
                    )
                    if in_scope and stored not in relative_paths:
                        stale.append(stored)
                for stored in stale:
                    for row in connection.execute(
                        "SELECT symbol_id, name FROM symbols WHERE path = ?", (stored,),
                    ):
                        affected_ids.add(str(row["symbol_id"]))
                        affected_names.add(str(row["name"]))
                    connection.execute("DELETE FROM files WHERE path = ?", (stored,))
                    affected_paths.add(stored)
                removed = len(stale)
            if indexed or removed:
                calls_resolved = self._resolve_calls(
                    connection, paths=affected_paths, raw_names=affected_names,
                    callee_ids=affected_ids,
                )
                references_resolved = self._resolve_references(
                    connection, paths=affected_paths, raw_names=affected_names,
                    target_ids=affected_ids,
                )
                generation = self._advance_generation(connection)
            else:
                calls_resolved = 0
                references_resolved = 0
                generation = self._generation(connection)
            connection.commit()
            entries = self._load(connection, relative_paths)
        return entries, IndexStats(
            len(files), indexed, reused, removed, omitted, generation, calls_resolved,
            references_resolved,
        )

    def update_paths(self, paths: list[str]) -> IndexStats:
        """Update explicit paths and remove deleted/renamed identities atomically."""
        unique = tuple(dict.fromkeys(str(item) for item in paths))
        indexed = removed = 0
        affected_paths: set[str] = set()
        affected_names: set[str] = set()
        affected_ids: set[str] = set()
        with self._lock, self._connect() as connection:
            for value in unique:
                target = (self.workspace / value).resolve()
                relative = self._relative(target)
                affected_paths.add(relative)
                for row in connection.execute(
                    "SELECT symbol_id, name FROM symbols WHERE path = ?", (relative,),
                ):
                    affected_ids.add(str(row["symbol_id"]))
                    affected_names.add(str(row["name"]))
                if not target.is_file() or not _is_supported_source(target):
                    cursor = connection.execute("DELETE FROM files WHERE path = ?", (relative,))
                    removed += max(0, cursor.rowcount)
                    continue
                entry, analysis = self._parse(target)
                self._replace(connection, entry, analysis)
                affected_ids.update(item.symbol_id for item in analysis.symbols)
                affected_names.update(item.name for item in analysis.symbols)
                indexed += 1
            if indexed or removed:
                calls_resolved = self._resolve_calls(
                    connection, paths=affected_paths, raw_names=affected_names,
                    callee_ids=affected_ids,
                )
                references_resolved = self._resolve_references(
                    connection, paths=affected_paths, raw_names=affected_names,
                    target_ids=affected_ids,
                )
                generation = self._advance_generation(connection)
            else:
                calls_resolved = 0
                references_resolved = 0
                generation = self._generation(connection)
            connection.commit()
        return IndexStats(
            len(unique), indexed, 0, removed, 0, generation, calls_resolved,
            references_resolved,
        )

    def augment_call_targets(
        self, resolver: CallTargetResolver, *, paths: list[str] | None = None,
        max_calls: int = 50, time_budget_ms: float = 250.0,
    ) -> CallAugmentationStats:
        """Upgrade unresolved structural calls with a bounded semantic resolver."""
        started = time.perf_counter()
        attempted = resolved = failed = 0
        truncated = False
        with self._lock, self._connect() as connection:
            query = (
                "SELECT id, path, line, raw_name, qualifier, caller_symbol_id FROM calls "
                "WHERE callee_symbol_id IS NULL"
            )
            params: list[object] = []
            normalized_paths = tuple(dict.fromkeys(
                str(item).replace("\\", "/").lstrip("./") for item in (paths or ())
            ))
            if normalized_paths:
                query += " AND path IN (" + ",".join("?" for _ in normalized_paths) + ")"
                params.extend(normalized_paths)
            query += " ORDER BY path, line, id LIMIT ?"
            params.append(max(1, int(max_calls)) + 1)
            rows = list(connection.execute(query, params))
            if len(rows) > max(1, int(max_calls)):
                truncated = True
                rows = rows[: max(1, int(max_calls))]
            for row in rows:
                if (time.perf_counter() - started) * 1000 >= max(1.0, time_budget_ms):
                    truncated = True
                    break
                attempted += 1
                request = CallResolutionRequest(
                    int(row["id"]), str(row["path"]), int(row["line"]),
                    str(row["raw_name"]), str(row["qualifier"] or ""),
                    str(row["caller_symbol_id"]),
                )
                try:
                    location = resolver.resolve(request)
                except Exception:
                    failed += 1
                    continue
                if location is None:
                    continue
                target = None
                if location.symbol_id:
                    target = connection.execute(
                        "SELECT symbol_id FROM symbols WHERE symbol_id = ?",
                        (location.symbol_id,),
                    ).fetchone()
                if target is None:
                    target = connection.execute(
                        "SELECT symbol_id FROM symbols WHERE path = ? "
                        "AND (? = '' OR name = ?) "
                        "ORDER BY ABS(line - ?), line LIMIT 1",
                        (
                            location.file_path, location.name, location.name,
                            max(1, int(location.line)),
                        ),
                    ).fetchone()
                if target is None:
                    continue
                connection.execute(
                    "UPDATE calls SET callee_symbol_id = ?, resolution_kind = ?, "
                    "confidence = ?, source = ?, candidates_json = '[]' WHERE id = ?",
                    (
                        str(target["symbol_id"]), "lsp-definition",
                        max(0.0, min(1.0, float(location.confidence))),
                        str(location.source or "lsp-definition"), int(row["id"]),
                    ),
                )
                resolved += 1
            generation = (
                self._advance_generation(connection) if resolved
                else self._generation(connection)
            )
            connection.commit()
        return CallAugmentationStats(
            attempted, resolved, failed, truncated, generation,
            round((time.perf_counter() - started) * 1000, 3),
        )

    @staticmethod
    def _module_lookup(paths: set[str]) -> dict[str, tuple[str, ...]]:
        lookup: dict[str, list[str]] = {}
        for path in paths:
            module = module_name_for_path(path)
            lookup.setdefault(module, []).append(path)
        return {key: tuple(sorted(value)) for key, value in lookup.items()}

    @staticmethod
    def _absolute_import(current_path: str, raw: str) -> str:
        if not raw.startswith("."):
            return raw
        level = len(raw) - len(raw.lstrip("."))
        suffix = raw[level:]
        current = module_name_for_path(current_path).split(".")
        if Path(current_path).name != "__init__.py" and current:
            current.pop()
        for _ in range(max(0, level - 1)):
            if current:
                current.pop()
        return ".".join([*current, *([suffix] if suffix else [])]).strip(".")

    @classmethod
    def _resolve_import_files(
        cls, current_path: str, raw: str, paths: set[str], modules: dict[str, tuple[str, ...]],
    ) -> tuple[str, ...]:
        if raw.startswith(".") and not raw.startswith("./"):
            return modules.get(cls._absolute_import(current_path, raw), ())
        if raw.startswith("."):
            base = (Path(current_path).parent / raw).as_posix()
        else:
            base = raw.replace(".", "/")
        candidates: list[str] = []
        for suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"):
            candidates.extend((base + suffix, base.rstrip("/") + "/index" + suffix))
        candidates.append(base.rstrip("/") + "/__init__.py")
        result = tuple(item for item in candidates if item in paths)
        if result:
            return result
        module_matches = modules.get(raw.replace("/", "."), ())
        if module_matches:
            return module_matches
        if Path(current_path).suffix == ".go":
            package = raw.rstrip("/").rsplit("/", 1)[-1]
            return tuple(sorted(
                path for path in paths
                if Path(path).suffix == ".go" and Path(path).parent.name == package
            ))
        return ()

    @classmethod
    def _resolve_relations(
        cls, connection: sqlite3.Connection, *, relation: str,
        paths: set[str] | None = None, raw_names: set[str] | None = None,
        target_ids: set[str] | None = None,
    ) -> int:
        if relation not in {"calls", "refs"}:
            raise ValueError(f"Unsupported relation table: {relation}")
        target_column = "callee_symbol_id" if relation == "calls" else "target_symbol_id"
        owner_column = "caller_symbol_id" if relation == "calls" else "source_symbol_id"
        indexed_paths = {
            str(row[0]) for row in connection.execute("SELECT path FROM files")
        }
        modules = cls._module_lookup(indexed_paths)
        filters = (
            ("path", paths), ("raw_name", raw_names), (target_column, target_ids),
        )
        clauses: list[str] = []
        params: list[str] = []
        for column, values in filters:
            if not values:
                continue
            # SQLite builds commonly cap bound variables at 999.  A cold build
            # should resolve the whole table instead of constructing a huge IN.
            if len(values) > 300:
                clauses = []
                params = []
                break
            clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
            params.extend(sorted(values))
        if any(value is not None for _column, value in filters) and not clauses and not params:
            if not any(values and len(values) > 300 for _column, values in filters):
                return 0
        query = (
            f"SELECT id, path, {owner_column} AS owner_symbol_id, raw_name, qualifier, "
            f"confidence, source FROM {relation}"
        )
        if clauses:
            query += " WHERE " + " OR ".join(clauses)
        call_rows = list(connection.execute(query, params))
        if not call_rows:
            return 0

        call_paths = {str(row["path"]) for row in call_rows}
        call_names = {str(row["raw_name"]) for row in call_rows}
        owner_ids = {
            str(row["owner_symbol_id"]) for row in call_rows if row["owner_symbol_id"]
        }
        imports: dict[str, list[dict]] = {}
        target_paths: set[str] = set()
        pending_import_paths = set(call_paths)
        loaded_import_paths: set[str] = set()
        # One extra import hop resolves common re-export barrels without turning
        # relation resolution into unbounded dependency traversal.
        for _depth in range(2):
            pending = sorted(pending_import_paths - loaded_import_paths)
            if not pending:
                break
            loaded_import_paths.update(pending)
            import_rows = connection.execute(
                "SELECT path, module, binding, imported_name, alias, kind FROM imports "
                "WHERE path IN (" + ",".join("?" for _ in pending) + ")",
                tuple(pending),
            )
            discovered: set[str] = set()
            for row in import_rows:
                item = dict(row)
                imports.setdefault(str(row["path"]), []).append(item)
                discovered.update(cls._resolve_import_files(
                    str(row["path"]), str(row["module"]), indexed_paths, modules,
                ))
            target_paths.update(discovered)
            pending_import_paths.update(discovered)

        symbol_paths = call_paths | target_paths
        symbol_filters = (
            ("symbols.path", symbol_paths),
            ("symbols.name", call_names),
            ("symbols.symbol_id", owner_ids),
        )
        symbol_clauses: list[str] = []
        symbol_params: list[str] = []
        load_all_symbols = any(len(values) > 300 for _column, values in symbol_filters)
        if not load_all_symbols:
            for column, values in symbol_filters:
                if not values:
                    continue
                symbol_clauses.append(
                    f"{column} IN ({','.join('?' for _ in values)})"
                )
                symbol_params.extend(sorted(values))
        symbol_query = (
            "SELECT symbols.symbol_id, symbols.path, symbols.module_id, symbols.kind, "
            "symbols.name, symbols.qualified_name, files.language FROM symbols "
            "JOIN files ON files.path = symbols.path"
        )
        if symbol_clauses and not load_all_symbols:
            symbol_query += " WHERE " + " OR ".join(symbol_clauses)
        symbols = [
            dict(row) for row in connection.execute(symbol_query, symbol_params)
        ]
        by_id = {item["symbol_id"]: item for item in symbols}
        by_name: dict[str, list[dict]] = {}
        by_path: dict[str, list[dict]] = {}
        for item in symbols:
            by_name.setdefault(item["name"], []).append(item)
            by_path.setdefault(item["path"], []).append(item)

        resolved_count = 0
        for row in call_rows:
            raw_name = str(row["raw_name"])
            qualifier = str(row["qualifier"] or "")
            path = str(row["path"])
            owner = by_id.get(str(row["owner_symbol_id"] or ""))
            target: dict | None = None
            kind = "unresolved"
            source = str(row["source"] or "")
            base_confidence = (
                0.98 if source == "python-ast"
                else 0.9 if source.startswith("tree-sitter-")
                else 0.45
            )
            confidence = base_confidence

            local = by_path.get(path, [])
            if owner and qualifier.split(".", 1)[0] in {"self", "cls"}:
                owner_name = str(owner["qualified_name"]).rsplit(".", 1)[0]
                matches = [
                    item for item in local
                    if item["qualified_name"] == f"{owner_name}.{raw_name}"
                ]
                if len(matches) == 1:
                    target, kind, confidence = matches[0], "self-method", 0.99

            if target is None and qualifier:
                root = qualifier.split(".", 1)[0]
                matches = [
                    item for item in local
                    if item["qualified_name"].endswith(f".{qualifier}.{raw_name}")
                ]
                if len(matches) == 1:
                    target, kind, confidence = matches[0], "qualified-same-module", 0.97
                if target is None:
                    for imported in imports.get(path, []):
                        if imported["binding"] != root:
                            continue
                        target_files = cls._resolve_import_files(
                            path, str(imported["module"]), indexed_paths, modules,
                        )
                        imported_prefix = str(imported["imported_name"] or "")
                        remainder = qualifier.split(".")[1:]
                        qparts = [item for item in (imported_prefix, *remainder, raw_name) if item]
                        matches = [
                            item for file_path in target_files for item in by_path.get(file_path, [])
                            if item["name"] == raw_name
                            and (not qparts or item["qualified_name"].endswith("." + ".".join(qparts)))
                        ]
                        if len(matches) == 1:
                            target, kind, confidence = matches[0], "qualified-import-member", 0.96
                            break

            if target is None and not qualifier:
                matches = [item for item in local if item["name"] == raw_name]
                top_level = [
                    item for item in matches
                    if item["qualified_name"].count(".") == module_name_for_path(path).count(".") + 1
                ]
                preferred = top_level or matches
                if len(preferred) == 1:
                    target, kind, confidence = preferred[0], "exact-same-module", 0.99

            if target is None and not qualifier:
                for imported in imports.get(path, []):
                    if imported["binding"] != raw_name or imported["kind"] != "from-import":
                        continue
                    target_files = cls._resolve_import_files(
                        path, str(imported["module"]), indexed_paths, modules,
                    )
                    imported_name = str(imported["imported_name"] or raw_name)
                    matches = [
                        item for file_path in target_files for item in by_path.get(file_path, [])
                        if item["name"] == imported_name
                    ]
                    if len(matches) == 1:
                        target, kind, confidence = matches[0], "imported-binding", 0.98
                        break

                    # Re-export: the imported file binds the requested name from
                    # another module but does not declare a duplicate symbol.
                    reexport_matches: list[dict] = []
                    for target_file in target_files:
                        for forwarded in imports.get(target_file, []):
                            if forwarded["binding"] != imported_name:
                                continue
                            forwarded_files = cls._resolve_import_files(
                                target_file, str(forwarded["module"]),
                                indexed_paths, modules,
                            )
                            forwarded_name = str(
                                forwarded["imported_name"] or imported_name
                            )
                            reexport_matches.extend(
                                item for file_path in forwarded_files
                                for item in by_path.get(file_path, [])
                                if item["name"] == forwarded_name
                            )
                    if len(reexport_matches) == 1:
                        target, kind, confidence = (
                            reexport_matches[0], "re-exported-binding", 0.94,
                        )
                        break

            candidates = by_name.get(raw_name, [])
            # A dynamic ``object.method()`` is not made exact merely because the
            # repository currently has one method with that spelling.
            if target is None and not qualifier and len(candidates) == 1:
                target, kind, confidence = candidates[0], "unique-repository-symbol", 0.8
            candidate_ids = tuple(sorted(item["symbol_id"] for item in candidates)[:12])
            if target is None and candidate_ids:
                kind, confidence = "heuristic-candidates", min(confidence, 0.4)
            elif target is None:
                confidence = min(confidence, 0.2)
            connection.execute(
                f"UPDATE {relation} SET {target_column} = ?, resolution_kind = ?, "
                "confidence = ?, candidates_json = ? WHERE id = ?",
                (
                    target["symbol_id"] if target else None, kind, confidence,
                    json.dumps(candidate_ids), int(row["id"]),
                ),
            )
            resolved_count += 1
        return resolved_count

    @classmethod
    def _resolve_calls(
        cls, connection: sqlite3.Connection, *, paths: set[str] | None = None,
        raw_names: set[str] | None = None, callee_ids: set[str] | None = None,
    ) -> int:
        return cls._resolve_relations(
            connection, relation="calls", paths=paths, raw_names=raw_names,
            target_ids=callee_ids,
        )

    @classmethod
    def _resolve_references(
        cls, connection: sqlite3.Connection, *, paths: set[str] | None = None,
        raw_names: set[str] | None = None, target_ids: set[str] | None = None,
    ) -> int:
        return cls._resolve_relations(
            connection, relation="refs", paths=paths, raw_names=raw_names,
            target_ids=target_ids,
        )

    @staticmethod
    def _load(connection: sqlite3.Connection, paths: list[str]) -> list[FileEntry]:
        entries: list[FileEntry] = []
        for path in paths:
            row = connection.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
            if row is None:
                continue
            symbols = tuple(
                PersistentCodeIndex._row_symbol(item)
                for item in connection.execute(
                    "SELECT symbols.*, files.language FROM symbols JOIN files "
                    "ON files.path = symbols.path WHERE symbols.path = ? "
                    "ORDER BY line, symbol_id", (path,),
                )
            )
            imports = tuple(
                ImportEntry(
                    item["path"], item["module"], item["binding"], item["imported_name"],
                    item["alias"], int(item["line"]), item["kind"], float(item["confidence"]),
                )
                for item in connection.execute(
                    "SELECT * FROM imports WHERE path = ? ORDER BY line, id", (path,),
                )
            )
            entries.append(FileEntry(
                path, row["language"], (int(row["mtime_ns"]), int(row["size"])),
                symbols, row["parse_error"], row["module_id"], row["capability_tier"],
                float(row["confidence"]), row["source"], imports,
            ))
        return entries

    @staticmethod
    def _row_symbol(row: sqlite3.Row) -> SymbolEntry:
        exported = row["exported"]
        return SymbolEntry(
            row["kind"], row["name"], row["qualified_name"], int(row["line"]),
            int(row["end_line"]), row["signature"], row["symbol_id"], row["path"],
            row["module_id"], row["language"] if "language" in row.keys() else "",
            None if exported is None else bool(exported), float(row["confidence"]),
            row["source"], row["capability_tier"],
        )

    @staticmethod
    def _row_edge(row: sqlite3.Row) -> CallEdge:
        candidates = tuple(json.loads(row["candidates_json"] or "[]"))
        unresolved = None
        if not row["callee_symbol_id"]:
            unresolved = UnresolvedCallTarget(
                row["raw_name"], row["qualifier"], candidates, float(row["confidence"]),
            )
        return CallEdge(
            row["caller_name"], row["callee_name"] or row["raw_name"], row["path"],
            int(row["line"]), float(row["confidence"]), row["source"],
            row["caller_symbol_id"], row["callee_symbol_id"], unresolved,
            row["qualifier"], row["resolution_kind"],
        )

    @staticmethod
    def _edge_select() -> str:
        return (
            "SELECT calls.*, target.name AS callee_name FROM calls "
            "LEFT JOIN symbols AS target ON target.symbol_id = calls.callee_symbol_id "
        )

    def generation(self) -> int:
        with self._lock, self._connect() as connection:
            return self._generation(connection)

    def snapshot(self, paths: list[str] | None = None) -> IndexSnapshot:
        """Load a consistent persistent snapshot without touching the filesystem."""
        with self._lock, self._connect() as connection:
            selected = (
                list(dict.fromkeys(paths)) if paths is not None else [
                    str(row[0]) for row in connection.execute("SELECT path FROM files ORDER BY path")
                ]
            )
            files = tuple(self._load(connection, selected))
            call_query = self._edge_select()
            call_params: tuple[object, ...] = ()
            if paths is not None:
                if selected:
                    call_query += "WHERE calls.path IN (" + ",".join("?" for _ in selected) + ") "
                    call_params = tuple(selected)
                else:
                    call_query += "WHERE 0 "
            call_query += "ORDER BY calls.path, calls.line, calls.id"
            calls = tuple(
                self._row_edge(row)
                for row in connection.execute(call_query, call_params)
            )
            return IndexSnapshot(self._generation(connection), files, calls, time.time())

    @staticmethod
    def _row_reference(row: sqlite3.Row) -> ReferenceEntry:
        return ReferenceEntry(
            str(row["path"]),
            str(row["source_symbol_id"]) if row["source_symbol_id"] else None,
            str(row["raw_name"]),
            str(row["target_symbol_id"]) if row["target_symbol_id"] else None,
            str(row["qualifier"] or ""), int(row["line"]), int(row["column_no"]),
            str(row["context"]), str(row["resolution_kind"]),
            float(row["confidence"]), str(row["source"]),
            tuple(json.loads(row["candidates_json"] or "[]")),
        )

    @staticmethod
    def _reference_select() -> str:
        return (
            "SELECT path, source_symbol_id, raw_name, target_symbol_id, qualifier, "
            "line, column_no, context, resolution_kind, confidence, source, "
            "candidates_json FROM refs "
        )

    def references(
        self, identifier: str, base: Path | None = None, limit: int = 100,
    ) -> list[ReferenceEntry]:
        """Return exact identity references; ambiguous names must be disambiguated."""
        selected_base = (base or self.workspace).resolve()
        relative = self._relative(selected_base)
        with self._lock, self._connect() as connection:
            symbol_id = self._unique_symbol_id(connection, identifier)
            if symbol_id is None:
                query = self._reference_select() + (
                    "WHERE target_symbol_id IS NULL AND raw_name = ?"
                )
                params: list[object] = [identifier.rsplit(".", 1)[-1]]
            else:
                query = self._reference_select() + "WHERE target_symbol_id = ?"
                params = [symbol_id]
            if selected_base.is_file():
                query += " AND path = ?"
                params.append(relative)
            elif relative not in ("", "."):
                query += " AND path LIKE ?"
                params.append(relative.rstrip("/") + "/%")
            query += " ORDER BY path, line, column_no, id LIMIT ?"
            params.append(max(1, limit))
            return [
                self._row_reference(row)
                for row in connection.execute(query, tuple(params))
            ]

    def _symbol_ids(self, connection: sqlite3.Connection, identifier: str) -> tuple[str, ...]:
        exact = connection.execute(
            "SELECT symbol_id FROM symbols WHERE symbol_id = ?", (identifier,),
        ).fetchone()
        if exact:
            return (str(exact[0]),)
        rows = connection.execute(
            "SELECT symbol_id FROM symbols WHERE qualified_name = ? OR name = ? "
            "ORDER BY path, line", (identifier, identifier),
        )
        return tuple(str(row[0]) for row in rows)

    def _symbol_alternatives(
        self, connection: sqlite3.Connection, ids: tuple[str, ...],
    ) -> tuple[dict, ...]:
        if not ids:
            return ()
        rows = connection.execute(
            "SELECT symbols.*, files.language FROM symbols JOIN files "
            "ON files.path = symbols.path WHERE symbols.symbol_id IN ("
            + ",".join("?" for _ in ids) + ") ORDER BY symbols.path, symbols.line",
            ids,
        )
        return tuple(self._symbol_dict(row) for row in rows)

    def _unique_symbol_id(
        self, connection: sqlite3.Connection, identifier: str,
    ) -> str | None:
        ids = self._symbol_ids(connection, identifier)
        if len(ids) > 1:
            raise AmbiguousSymbolError(
                identifier, self._symbol_alternatives(connection, ids),
            )
        return ids[0] if ids else None

    def callers(self, identifier: str, limit: int = 100) -> list[CallEdge]:
        with self._lock, self._connect() as connection:
            symbol_id = self._unique_symbol_id(connection, identifier)
            if symbol_id is None:
                return []
            rows = connection.execute(
                self._edge_select() + "WHERE calls.callee_symbol_id = ? "
                "ORDER BY calls.path, calls.line, calls.id LIMIT ?",
                (symbol_id, max(1, limit)),
            )
            return [self._row_edge(row) for row in rows]

    def callees(self, identifier: str, limit: int = 100) -> list[CallEdge]:
        with self._lock, self._connect() as connection:
            symbol_id = self._unique_symbol_id(connection, identifier)
            if symbol_id is None:
                return []
            rows = connection.execute(
                self._edge_select() + "WHERE calls.caller_symbol_id = ? "
                "ORDER BY calls.path, calls.line, calls.id LIMIT ?",
                (symbol_id, max(1, limit)),
            )
            return [self._row_edge(row) for row in rows]

    def file_symbols(self, path: str, limit: int = 100) -> list[dict]:
        relative = self._relative((self.workspace / path).resolve())
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT symbols.*, files.language FROM symbols JOIN files ON files.path = symbols.path "
                "WHERE symbols.path = ? ORDER BY line, symbol_id LIMIT ?",
                (relative, max(1, limit)),
            )
            return [self._symbol_dict(row) for row in rows]

    def file_imports(self, path: str, limit: int = 100) -> list[dict]:
        relative = self._relative((self.workspace / path).resolve())
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT module, binding, imported_name, alias, line, kind, confidence "
                "FROM imports WHERE path = ? ORDER BY line, id LIMIT ?",
                (relative, max(1, limit)),
            )]

    def file_calls(self, path: str, limit: int = 100) -> list[CallEdge]:
        relative = self._relative((self.workspace / path).resolve())
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                self._edge_select() + "WHERE calls.path = ? ORDER BY calls.line, calls.id LIMIT ?",
                (relative, max(1, limit)),
            )
            return [self._row_edge(row) for row in rows]

    @staticmethod
    def _symbol_dict(row: sqlite3.Row) -> dict:
        exported = row["exported"]
        return {
            "symbol_id": row["symbol_id"], "name": row["name"],
            "qualified_name": row["qualified_name"], "kind": row["kind"],
            "path": row["path"], "file_path": row["path"],
            "module_id": row["module_id"], "language": row["language"],
            "line": int(row["line"]), "end_line": int(row["end_line"]),
            "signature": row["signature"],
            "exported": None if exported is None else bool(exported),
            "confidence": float(row["confidence"]), "source": row["source"],
            "capability_tier": row["capability_tier"],
        }

    @staticmethod
    def _edge_dict(edge: CallEdge) -> dict:
        unresolved = None
        if edge.unresolved_target is not None:
            unresolved = {
                "raw_name": edge.unresolved_target.raw_name,
                "qualifier": edge.unresolved_target.qualifier,
                "candidates": list(edge.unresolved_target.candidates),
                "confidence": edge.unresolved_target.confidence,
            }
        return {
            "caller": edge.caller, "callee": edge.callee, "path": edge.path,
            "call_site_file": edge.path, "line": edge.line,
            "caller_symbol_id": edge.caller_symbol_id,
            "callee_symbol_id": edge.callee_symbol_id,
            "unresolved_target": unresolved, "qualifier": edge.qualifier,
            "resolution_kind": edge.resolution_kind,
            "confidence": edge.confidence, "source": edge.source,
        }

    def symbol_context(
        self, identifier: str, limit: int = 30, *, enrich: bool = True,
    ) -> dict:
        with self._lock, self._connect() as connection:
            ids = self._symbol_ids(connection, identifier)
            if ids:
                rows = list(connection.execute(
                    "SELECT symbols.*, files.language FROM symbols JOIN files "
                    "ON files.path = symbols.path WHERE symbols.symbol_id IN ("
                    + ",".join("?" for _ in ids) + ") ORDER BY symbols.path, line LIMIT ?",
                    (*ids, max(1, limit)),
                ))
            else:
                rows = []
        definitions = [self._symbol_dict(row) for row in rows]
        ambiguous = len(definitions) > 1
        definition = definitions[0] if len(definitions) == 1 else None
        if ambiguous:
            references: list[ReferenceEntry] = []
            callers: list[dict] = []
            callees: list[dict] = []
            related_tests: list[str] = []
        elif definition is not None:
            lookup_id = str(definition["symbol_id"])
            references = self.references(lookup_id, self.workspace, limit)
            callers = [self._edge_dict(item) for item in self.callers(lookup_id, limit)]
            callees = [self._edge_dict(item) for item in self.callees(lookup_id, limit)]
            related_tests = self.related_tests((lookup_id,), limit=limit) if enrich else []
        else:
            references, callers, callees, related_tests = [], [], [], []
        confidence = min(
            [float(item["confidence"]) for item in definitions]
            + [float(item["confidence"]) for item in callers + callees]
            + [1.0]
        )
        tiers = {str(item.get("capability_tier")) for item in definitions}
        legacy_confidence = (
            "structural" if not tiers or tiers == {CapabilityTier.AST_NATIVE.value}
            else "mixed-structural"
        )
        return {
            "symbol": identifier,
            "symbol_id": definition["symbol_id"] if definition else None,
            "qualified_name": definition["qualified_name"] if definition else None,
            "definition": definition, "definitions": definitions,
            "ambiguous": ambiguous,
            "alternatives": definitions if ambiguous else [],
            "module": definition["module_id"] if definition else None,
            "signature": definition["signature"] if definition else None,
            "references": [item.to_dict() for item in references],
            "callers": callers, "callees": callees,
            "imports": self.file_imports(definition["path"], limit) if definition else [],
            "exports": [definition] if definition and definition["exported"] else [],
            "related_tests": related_tests,
            "changed": enrich and self._paths_changed(
                [str(item["path"]) for item in definitions]
            ),
            "freshness": "indexed", "confidence": legacy_confidence,
            "confidence_score": round(confidence, 3),
            "source": "persistent-identity-code-index", "capsule_version": 3,
            "warnings": [
                "Symbol name is ambiguous; definition and identity relations were withheld. "
                "Retry with symbol_id or qualified_name."
            ] if ambiguous else [],
        }

    @staticmethod
    def _is_test_path(path: str) -> bool:
        value = Path(path)
        return (
            any(part in {"test", "tests", "__tests__", "spec"} for part in value.parts)
            or value.name.startswith("test_")
            or any(token in value.name for token in (".test.", ".spec."))
            or value.name.endswith("_test.go")
        )

    def _paths_changed(self, paths: list[str]) -> bool:
        selected = sorted({path for path in paths if path})
        if not selected or not (self.workspace / ".git").exists():
            return False
        try:
            completed = subprocess.run(
                [
                    "git", "status", "--porcelain", "--untracked-files=all",
                    "--", *selected,
                ],
                cwd=self.workspace, capture_output=True, text=True,
                timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and bool(completed.stdout.strip())

    def related_tests(
        self, symbol_ids: list[str] | tuple[str, ...] | set[str], *, limit: int = 100,
    ) -> list[str]:
        """Infer tests from indexed calls/references and file-name conventions."""
        ids = tuple(sorted({str(item) for item in symbol_ids if item}))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as connection:
            symbols = list(connection.execute(
                f"SELECT symbol_id, name, path FROM symbols WHERE symbol_id IN ({placeholders})",
                ids,
            ))
            candidates = {
                str(row["path"]) for row in connection.execute(
                    f"SELECT DISTINCT path FROM calls WHERE callee_symbol_id IN ({placeholders})",
                    ids,
                )
            }
            candidates.update(str(row["path"]) for row in connection.execute(
                f"SELECT DISTINCT path FROM refs WHERE target_symbol_id IN ({placeholders})",
                ids,
            ))
            all_test_paths = [str(row["path"]) for row in connection.execute(
                "SELECT path FROM files WHERE "
                "path LIKE 'tests/%' OR path LIKE '%/tests/%' OR "
                "path LIKE 'test/%' OR path LIKE '%/test/%' OR "
                "path LIKE '%/__tests__/%' OR path LIKE 'test_%' OR "
                "path LIKE '%.test.%' OR path LIKE '%.spec.%' OR "
                "path LIKE '%_test.go'"
            )]
        source_stems = {
            Path(str(row["path"])).stem.removeprefix("test_").removesuffix("_test")
            for row in symbols
        }
        for path in all_test_paths:
            stem = Path(path).stem.removeprefix("test_").removesuffix("_test")
            stem = stem.split(".", 1)[0]
            if stem in source_stems:
                candidates.add(path)
        return sorted(
            path for path in candidates if self._is_test_path(path)
        )[: max(0, int(limit))]

    def entrypoints(
        self, limit: int = 100, *, module_id: str | None = None,
    ) -> list[dict]:
        with self._lock, self._connect() as connection:
            query = (
                "SELECT symbols.*, files.language FROM symbols JOIN files "
                "ON files.path = symbols.path"
            )
            params: tuple[object, ...] = ()
            if module_id is not None:
                query += " WHERE symbols.module_id = ?"
                params = (module_id,)
            query += " ORDER BY symbols.path, line"
            rows = list(connection.execute(query, params))
        ranked: list[tuple[int, str, int, dict]] = []
        for row in rows:
            item = self._symbol_dict(row)
            classified = self.entrypoint_kind(item)
            if classified is None:
                continue
            kind, rank = classified
            item["entry_kind"] = kind
            ranked.append((rank, str(item["path"]), int(item["line"]), item))
        ranked.sort(key=lambda value: value[:3])
        return [item for _rank, _path, _line, item in ranked[: max(1, limit)]]

    @staticmethod
    def entrypoint_kind(item: dict) -> tuple[str, int] | None:
        """Classify a symbol as an entrypoint without another index query."""
        path = Path(str(item["path"]))
        name = str(item["name"])
        is_test = "tests" in path.parts or path.name.startswith("test_")
        if name in {"main", "__main__", "cli", "app", "run"}:
            return "cli-or-application", {
                "main": 0, "__main__": 1, "cli": 2, "app": 2, "run": 3,
            }[name]
        if name.startswith(("get_", "post_", "put_", "delete_", "route_")):
            return "http-route", 1
        if is_test and name.startswith("test"):
            return "test", 3
        if item.get("exported") and path.name in {
            "__init__.py", "index.ts", "index.js", "lib.rs",
        }:
            return "public-api", 2
        return None

    def top_symbols(self, module_id: str, *, limit: int = 12) -> list[dict]:
        """Rank module symbols using one aggregate query."""
        with self._lock, self._connect() as connection:
            rows = list(connection.execute(
                "SELECT symbols.*, files.language, "
                "COALESCE(ref_counts.count, 0) AS refs_count, "
                "COALESCE(call_counts.count, 0) AS callers_count "
                "FROM symbols JOIN files ON files.path = symbols.path "
                "LEFT JOIN (SELECT target_symbol_id, count(*) AS count FROM refs "
                "WHERE target_symbol_id IS NOT NULL GROUP BY target_symbol_id) ref_counts "
                "ON ref_counts.target_symbol_id = symbols.symbol_id "
                "LEFT JOIN (SELECT callee_symbol_id, count(*) AS count FROM calls "
                "WHERE callee_symbol_id IS NOT NULL GROUP BY callee_symbol_id) call_counts "
                "ON call_counts.callee_symbol_id = symbols.symbol_id "
                "WHERE symbols.module_id = ?",
                (module_id,),
            ))
        ranked: list[tuple[int, int, str, dict]] = []
        for row in rows:
            item = self._symbol_dict(row)
            entrypoint = self.entrypoint_kind(item)
            references = int(row["refs_count"])
            callers = int(row["callers_count"])
            score = (
                (20 if item.get("exported") else 0)
                + (25 if entrypoint is not None else 0)
                + min(20, references) + min(20, callers * 2)
                + (5 if not str(item["name"]).startswith("_") else 0)
            )
            ranked.append((-score, int(item["line"]), str(item["name"]), item))
        ranked.sort(key=lambda value: value[:3])
        return [item for _score, _line, _name, item in ranked[: max(1, int(limit))]]

    def search_symbols(self, query: str, limit: int = 30) -> list[dict]:
        terms = intent_tokens(query)
        if not terms:
            return []
        with self._lock, self._connect() as connection:
            rows = list(connection.execute(
                "SELECT symbols.*, files.language, "
                "COALESCE(ref_counts.count, 0) AS refs_count, "
                "COALESCE(call_counts.count, 0) AS callers_count "
                "FROM symbols JOIN files ON files.path = symbols.path "
                "LEFT JOIN (SELECT target_symbol_id, count(*) AS count FROM refs "
                "WHERE target_symbol_id IS NOT NULL GROUP BY target_symbol_id) ref_counts "
                "ON ref_counts.target_symbol_id = symbols.symbol_id "
                "LEFT JOIN (SELECT callee_symbol_id, count(*) AS count FROM calls "
                "WHERE callee_symbol_id IS NOT NULL GROUP BY callee_symbol_id) call_counts "
                "ON call_counts.callee_symbol_id = symbols.symbol_id "
                "ORDER BY symbols.path, line LIMIT 20000"
            ))
        ranked = []
        for row in rows:
            item = self._symbol_dict(row)
            base_score = structural_match_score(
                query, str(item["name"]), str(item["qualified_name"]),
                str(item["signature"] or ""), str(item["path"]),
                str(item["module_id"]),
            )
            if base_score <= 0:
                continue
            importance = min(
                0.06,
                (0.015 if item.get("exported") else 0.0)
                + min(0.025, int(row["callers_count"]) * 0.005)
                + min(0.02, int(row["refs_count"]) * 0.002),
            )
            score = round(min(1.0, base_score + importance), 4)
            item["search_source"] = "structural-symbol-search"
            item["match_score"] = score
            item["importance"] = {
                "exported": bool(item.get("exported")),
                "references": int(row["refs_count"]),
                "callers": int(row["callers_count"]),
            }
            ranked.append((-score, str(item["path"]), int(item["line"]), item))
        ranked.sort(key=lambda value: value[:3])
        return [item for _score, _path, _line, item in ranked[: max(1, limit)]]

    def process_context(
        self, entry: str, *, max_depth: int = 3, limit: int = 50,
        max_nodes: int | None = None, time_budget_ms: float = 100.0,
        confidence_threshold: float = 0.0,
    ) -> dict:
        """Discover a bounded identity-based process capsule on demand."""
        started = time.perf_counter()
        context = self.symbol_context(entry, limit=max(1, limit), enrich=False)
        if context.get("ambiguous"):
            return {
                "process_id": None, "label": entry, "entry_symbol_id": None,
                "entry_file": None, "module_ids": [], "steps": [],
                "related_tests": [], "confidence": 0.0, "truncated": False,
                "ambiguous": True,
                "alternatives": list(context.get("alternatives") or ()),
                "warnings": [
                    "Process entry is ambiguous; retry with symbol_id or qualified_name."
                ],
                "freshness": "indexed", "source": "identity-call-graph",
                "capsule_version": 3,
            }
        definition = context.get("definition")
        discovered_from_query = False
        if definition is None:
            matches = self.search_symbols(entry, limit=5)
            if matches:
                top_score = matches[0].get("match_score")
                tied = [item for item in matches if item.get("match_score") == top_score]
                if len(tied) > 1:
                    return {
                        "process_id": None, "label": entry, "entry_symbol_id": None,
                        "entry_file": None, "module_ids": [], "steps": [],
                        "related_tests": [], "confidence": 0.0,
                        "truncated": False, "ambiguous": True,
                        "alternatives": tied,
                        "warnings": [
                            "Process intent has tied entry candidates; retry with symbol_id."
                        ],
                        "freshness": "indexed", "source": "identity-call-graph",
                        "capsule_version": 3,
                    }
                context = self.symbol_context(
                    str(matches[0]["symbol_id"]), limit=max(1, limit), enrich=False,
                )
                definition = context.get("definition")
                discovered_from_query = definition is not None
        if definition is None:
            return {
                "process_id": None, "label": entry, "entry_symbol_id": None,
                "entry_file": None, "module_ids": [], "steps": [],
                "related_tests": [], "confidence": 0.0, "truncated": False,
                "ambiguous": False, "alternatives": [],
                "freshness": "indexed", "source": "identity-call-graph",
                "capsule_version": 3,
            }
        entry_id = str(definition["symbol_id"])
        node_limit = max(1, int(max_nodes or limit))
        queue = [(entry_id, 0)]
        visited = {entry_id}
        edges: list[dict] = []
        steps = [ProcessStep(
            "entry", entry_id, str(definition["qualified_name"]),
            str(definition["path"]), int(definition["line"]),
            confidence=float(definition["confidence"]),
        )]
        modules = {str(definition["module_id"])}
        truncated = False
        while queue:
            if (time.perf_counter() - started) * 1000 >= max(1.0, time_budget_ms):
                truncated = True
                break
            caller_id, depth = queue.pop(0)
            if depth >= max(0, int(max_depth)):
                continue
            for edge in self.callees(caller_id, limit=node_limit):
                if edge.confidence < confidence_threshold:
                    continue
                if len(edges) >= node_limit:
                    truncated = True
                    queue.clear()
                    break
                payload = self._edge_dict(edge)
                edges.append(payload)
                kind = "call" if edge.callee_symbol_id else "unresolved-call"
                target = None
                if edge.callee_symbol_id and edge.callee_symbol_id not in visited:
                    visited.add(edge.callee_symbol_id)
                    target = self.symbol_context(
                        edge.callee_symbol_id, 1, enrich=False,
                    ).get("definition")
                    if target:
                        modules.add(str(target["module_id"]))
                        if target["module_id"] != definition["module_id"]:
                            steps.append(ProcessStep(
                                "module-transition", edge.callee_symbol_id,
                                str(target["qualified_name"]), str(target["path"]),
                                int(target["line"]), call_site_file=edge.path,
                                resolution_kind=edge.resolution_kind,
                                confidence=edge.confidence,
                            ))
                    queue.append((edge.callee_symbol_id, depth + 1))
                elif edge.callee_symbol_id:
                    target = self.symbol_context(
                        edge.callee_symbol_id, 1, enrich=False,
                    ).get("definition")
                steps.append(ProcessStep(
                    kind, edge.callee_symbol_id, edge.callee,
                    str(target["path"]) if target else edge.path,
                    int(target["line"]) if target else edge.line,
                    call_site_file=edge.path, resolution_kind=edge.resolution_kind,
                    confidence=edge.confidence,
                ))
        incoming = [self._edge_dict(item) for item in self.callers(entry_id, limit)]
        visited.update(
            str(item["caller_symbol_id"]) for item in incoming
            if item.get("caller_symbol_id")
        )
        nodes = [
            value for symbol_id in sorted(visited)
            if (
                value := self.symbol_context(symbol_id, 1, enrich=False).get("definition")
            ) is not None
        ]
        related_tests = self.related_tests(visited, limit=node_limit)
        confidence = min([float(item.get("confidence", 1.0)) for item in edges] + [float(definition["confidence"])])
        capsule = ProcessCapsule(
            process_id=f"process:{entry_id}:depth={max_depth}",
            label=str(definition["qualified_name"]), entry_symbol_id=entry_id,
            entry_file=str(definition["path"]), module_ids=tuple(sorted(modules)),
            steps=tuple(steps), related_tests=tuple(related_tests),
            confidence=round(confidence, 3),
        )
        return {
            **capsule.to_dict(), "entry": entry,
            "ambiguous": False, "alternatives": [],
            "discovered_from_query": discovered_from_query,
            "symbols": sorted(visited), "edges": edges, "incoming_edges": incoming,
            "nodes": nodes,
            "truncated": truncated, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "freshness": "indexed", "source": "identity-call-graph",
            "capsule_version": 3,
        }

    def metrics(self) -> dict:
        with self._lock, self._connect() as connection:
            return {
                "generation": self._generation(connection),
                "files_indexed": int(connection.execute("SELECT count(*) FROM files").fetchone()[0]),
                "symbols_indexed": int(connection.execute("SELECT count(*) FROM symbols").fetchone()[0]),
                "call_edges": int(connection.execute("SELECT count(*) FROM calls").fetchone()[0]),
                "references": int(connection.execute("SELECT count(*) FROM refs").fetchone()[0]),
                "resolved_references": int(connection.execute(
                    "SELECT count(*) FROM refs WHERE target_symbol_id IS NOT NULL"
                ).fetchone()[0]),
                "unresolved_calls": int(connection.execute(
                    "SELECT count(*) FROM calls WHERE callee_symbol_id IS NULL"
                ).fetchone()[0]),
            }


def update_code_index_after_write(paths: list[str], workspace: Path) -> IndexStats:
    """Best-effort write hook; the shared service watcher observes the same generation."""
    return PersistentCodeIndex(workspace).update_paths(paths)


__all__ = [
    "AmbiguousSymbolError", "CallAugmentationStats", "CallEdge", "CallResolutionRequest",
    "CallTargetResolver", "FileEntry", "ImportEntry", "IndexSnapshot", "IndexStats",
    "PersistentCodeIndex", "ProcessCapsule", "ProcessStep", "ReferenceEntry",
    "ResolvedCallLocation", "SymbolEntry", "UnresolvedCallTarget",
    "ast", "intent_tokens", "structural_match_score", "update_code_index_after_write",
]
