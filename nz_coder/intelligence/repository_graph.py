"""Snapshot-driven module graph and bounded repository impact intelligence."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from nz_coder.intelligence.code_index import (
    IndexSnapshot,
    IndexStats,
    PersistentCodeIndex,
    structural_match_score,
)


_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec"})


@dataclass(frozen=True)
class GraphBuildStats:
    scanned: int
    indexed: int
    reused: int
    removed: int
    omitted: int
    duration_ms: float
    generation: int = 0
    relationships_updated: int = 0


@dataclass(frozen=True)
class ModuleCapsule:
    module_id: str
    root: str
    label: str
    kind: str
    languages: tuple[str, ...]
    files: tuple[str, ...]
    entry_files: tuple[str, ...]
    top_symbols: tuple[dict, ...]
    dependencies: tuple[str, ...]
    dependents: tuple[str, ...]
    related_tests: tuple[str, ...]
    process_ids: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict:
        value = asdict(self)
        for key in (
            "languages", "files", "entry_files", "top_symbols", "dependencies",
            "dependents", "related_tests", "process_ids",
        ):
            value[key] = list(value[key])
        return value


class RepositoryGraph:
    """Persistent module graph derived only from a code-index snapshot."""

    def __init__(self, workspace: Path, *, index: PersistentCodeIndex | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("RepositoryGraph workspace must be a directory")
        state = self.workspace / ".nz-coder" / "index"
        state.mkdir(parents=True, exist_ok=True)
        if self.workspace not in state.resolve().parents:
            raise ValueError("RepositoryGraph cache escapes workspace")
        self.cache_path = state / "repository-graph-v2.json"
        self.journal_path = state / "repository-graph-v2.journal.jsonl"
        self.index = index or PersistentCodeIndex(self.workspace)
        self._records: dict[str, dict] = {}
        self._graph: dict[str, tuple[str, ...]] = {}
        self._reverse_graph: dict[str, set[str]] = {}
        self._importers: dict[str, set[str]] = {}
        self._generation = 0
        self._journal_updates = 0
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or int(payload.get("version", 0)) != 2:
                return
            records = payload.get("modules")
            if isinstance(records, dict):
                self._records = {
                    str(name): dict(value) for name, value in records.items()
                    if isinstance(value, dict)
                }
                self._generation = int(payload.get("generation", 0))
        except (OSError, ValueError, TypeError):
            self._records = {}
            self._generation = 0
        try:
            for raw in self.journal_path.read_text(encoding="utf-8").splitlines():
                update = json.loads(raw)
                if not isinstance(update, dict):
                    continue
                for path in update.get("deleted", ()):
                    self._records.pop(str(path), None)
                for path, record in dict(update.get("upsert") or {}).items():
                    if isinstance(record, dict):
                        self._records[str(path)] = dict(record)
                self._generation = max(
                    self._generation, int(update.get("generation") or 0),
                )
                self._journal_updates += 1
        except (OSError, ValueError, TypeError):
            pass
        self._resolve_graph()

    @staticmethod
    def _record_from_file(entry) -> dict:
        return {
            "module_id": entry.module_id or f"module:{entry.path}",
            "path": entry.path,
            "root": str(Path(entry.path).parent.as_posix()),
            "label": Path(entry.path).stem,
            "language": entry.language,
            "fingerprint": list(entry.fingerprint),
            "imports": [item.module for item in entry.imports],
            "capability_tier": entry.capability_tier,
            "confidence": entry.confidence,
            "source": entry.source,
        }

    def build(
        self, *, max_files: int = 5000, snapshot: IndexSnapshot | None = None,
    ) -> GraphBuildStats:
        """Cold/warm materialization; scan exactly once when no snapshot is supplied."""
        started = time.perf_counter()
        if snapshot is None:
            _entries, stats = self.index.scan(
                self.workspace, max_files=max(1, int(max_files)),
            )
            snapshot = self.index.snapshot()
        else:
            old = self._records
            current = {entry.path: entry for entry in snapshot.files}
            indexed = sum(
                1 for path, entry in current.items()
                if old.get(path, {}).get("fingerprint") != list(entry.fingerprint)
            )
            reused = len(current) - indexed
            stats = IndexStats(
                scanned=len(current), indexed=indexed, reused=reused,
                removed=len(set(old) - set(current)), generation=snapshot.generation,
            )
        self.update(snapshot)
        return GraphBuildStats(
            stats.scanned, stats.indexed, stats.reused, stats.removed, stats.omitted,
            round((time.perf_counter() - started) * 1000, 3), snapshot.generation,
        )

    def update(self, snapshot: IndexSnapshot) -> None:
        """Materialize all graph records from one consistent index generation."""
        self._records = {
            entry.path: self._record_from_file(entry) for entry in snapshot.files
        }
        self._generation = snapshot.generation
        self._resolve_graph()
        self._save()

    def update_paths(
        self, changed: list[str] | tuple[str, ...],
        deleted: list[str] | tuple[str, ...] = (),
        *, snapshot: IndexSnapshot | None = None,
    ) -> GraphBuildStats:
        """Refresh changed records without parsing or rebuilding unaffected records."""
        started = time.perf_counter()
        snapshot = snapshot or self.index.snapshot()
        selected = {entry.path: entry for entry in snapshot.files}
        changed_set = {str(item).replace("\\", "/").lstrip("./") for item in changed}
        deleted_set = {str(item).replace("\\", "/").lstrip("./") for item in deleted}
        indexed = removed = 0
        target_paths = deleted_set | changed_set
        affected_sources = set(changed_set)
        for target in target_paths:
            for key in self._path_import_keys(target):
                affected_sources.update(self._importers.get(key, ()))
        for source, dependencies in self._graph.items():
            if set(dependencies) & target_paths:
                affected_sources.add(source)
        for path in deleted_set | {path for path in changed_set if path not in selected}:
            previous = self._records.get(path)
            if previous is not None:
                self._remove_importer(path, previous.get("imports", []))
            removed += int(self._records.pop(path, None) is not None)
            for target in self._graph.pop(path, ()):
                reverse = self._reverse_graph.get(target)
                if reverse is not None:
                    reverse.discard(path)
                    if not reverse:
                        self._reverse_graph.pop(target, None)
            self._reverse_graph.pop(path, None)
        for path in changed_set:
            entry = selected.get(path)
            if entry is None:
                continue
            record = self._record_from_file(entry)
            if self._records.get(path) != record:
                previous = self._records.get(path)
                if previous is not None:
                    self._remove_importer(path, previous.get("imports", []))
                self._records[path] = record
                self._add_importer(path, record.get("imports", []))
                indexed += 1
        for target in target_paths:
            for key in self._path_import_keys(target):
                affected_sources.update(self._importers.get(key, ()))
        self._generation = snapshot.generation
        relationships_updated = self._resolve_graph_paths(affected_sources)
        self._append_update(changed_set, deleted_set)
        return GraphBuildStats(
            len(changed_set | deleted_set), indexed, 0, removed, 0,
            round((time.perf_counter() - started) * 1000, 3), snapshot.generation,
            relationships_updated,
        )

    def _resolve_graph(self) -> None:
        names = set(self._records)
        self._importers = {}
        for source, record in self._records.items():
            self._add_importer(source, record.get("imports", []))
        self._graph = {
            module: tuple(sorted({
                target for raw in record.get("imports", [])
                for target in [self._resolve_import(module, str(raw), names)]
                if target and target != module
            }))
            for module, record in self._records.items()
        }
        self._reverse_graph = {}
        for source, targets in self._graph.items():
            for target in targets:
                self._reverse_graph.setdefault(target, set()).add(source)

    def _resolve_graph_paths(self, sources: set[str]) -> int:
        names = set(self._records)
        updated = 0
        for source in sorted(sources):
            record = self._records.get(source)
            if record is None:
                continue
            previous = self._graph.get(source, ())
            for target in previous:
                reverse = self._reverse_graph.get(target)
                if reverse is not None:
                    reverse.discard(source)
                    if not reverse:
                        self._reverse_graph.pop(target, None)
            targets = tuple(sorted({
                target for raw in record.get("imports", [])
                for target in [self._resolve_import(source, str(raw), names)]
                if target and target != source
            }))
            self._graph[source] = targets
            for target in targets:
                self._reverse_graph.setdefault(target, set()).add(source)
            updated += 1
        return updated

    @staticmethod
    def _import_keys(raw: str) -> set[str]:
        value = str(raw).strip().strip("\"'").replace("\\", "/")
        dotted = value.lstrip(".").replace(".", "/")
        tail = dotted.rstrip("/").rsplit("/", 1)[-1]
        return {item for item in (value, dotted, tail) if item}

    @staticmethod
    def _path_import_keys(path: str) -> set[str]:
        value = Path(path)
        without_suffix = value.with_suffix("").as_posix()
        if value.name == "__init__.py":
            without_suffix = value.parent.as_posix()
        keys = {
            path, without_suffix, without_suffix.replace("/", "."),
            value.stem, value.parent.name,
        }
        if value.stem == "index":
            keys.add(value.parent.as_posix())
            keys.add(value.parent.name)
        return {item for item in keys if item and item != "."}

    def _add_importer(self, source: str, imports: list[str]) -> None:
        for raw in imports:
            for key in self._import_keys(str(raw)):
                self._importers.setdefault(key, set()).add(source)

    def _remove_importer(self, source: str, imports: list[str]) -> None:
        for raw in imports:
            for key in self._import_keys(str(raw)):
                sources = self._importers.get(key)
                if sources is None:
                    continue
                sources.discard(source)
                if not sources:
                    self._importers.pop(key, None)

    @staticmethod
    def _resolve_import(source: str, raw: str, modules: set[str]) -> str | None:
        source_path = Path(source)
        suffixes = (source_path.suffix, ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs")
        if raw.startswith("."):
            base = (source_path.parent / raw).as_posix()
        else:
            base = raw.replace(".", "/")
        candidates = []
        for suffix in suffixes:
            candidates.extend((
                base if base.endswith(suffix) else base + suffix,
                base.rstrip("/") + "/index" + suffix,
            ))
        candidates.append(base.rstrip("/") + "/__init__.py")
        for candidate in candidates:
            if candidate in modules:
                return candidate
        tail = raw.rstrip("/").split("/")[-1]
        if source_path.suffix == ".go":
            for module in sorted(modules):
                if Path(module).parent.name == tail and module.endswith(".go"):
                    return module
        return None

    def _save(self) -> None:
        payload = {
            "version": 2, "generation": self._generation,
            "updated_at": time.time(), "modules": self._records,
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix="repository-graph-", suffix=".tmp", dir=str(self.cache_path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.cache_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        try:
            self.journal_path.unlink()
        except FileNotFoundError:
            pass
        self._journal_updates = 0

    def _append_update(self, changed: set[str], deleted: set[str]) -> None:
        payload = {
            "generation": self._generation,
            "deleted": sorted(deleted | {path for path in changed if path not in self._records}),
            "upsert": {
                path: self._records[path] for path in sorted(changed) if path in self._records
            },
        }
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._journal_updates += 1
        try:
            oversized = self.journal_path.stat().st_size > 4 * 1024 * 1024
        except OSError:
            oversized = False
        if self._journal_updates >= 500 or oversized:
            self._save()

    def modules(self) -> tuple[str, ...]:
        return tuple(sorted(self._graph))

    def module_ids(self) -> tuple[str, ...]:
        return tuple(sorted({str(item["module_id"]) for item in self._records.values()}))

    def overview(self, *, limit: int = 50) -> dict:
        languages: dict[str, int] = {}
        tiers: dict[str, int] = {}
        for record in self._records.values():
            language = str(record.get("language") or "unknown")
            languages[language] = languages.get(language, 0) + 1
            tier = str(record.get("capability_tier") or "unknown")
            tiers[tier] = tiers.get(tier, 0) + 1
        ranked = sorted(self._graph, key=lambda name: (-len(self._graph[name]), name))
        return {
            "generation": self._generation, "module_count": len(self.module_ids()),
            "file_module_count": len(self._graph),
            "dependency_count": sum(len(items) for items in self._graph.values()),
            "languages": dict(sorted(languages.items())),
            "capability_tiers": dict(sorted(tiers.items())),
            "cycle_count": len(self.cycles()), "modules": ranked[: max(0, int(limit))],
            "source": "index-snapshot",
        }

    def search_modules(
        self, query: str, *, limit: int = 20,
        symbol_candidates: list[dict] | None = None,
        entry_candidates: list[dict] | None = None,
    ) -> list[dict]:
        """Rank first-class module identities from indexed structural evidence."""
        grouped: dict[str, list[str]] = {}
        for path, record in self._records.items():
            grouped.setdefault(str(record["module_id"]), []).append(path)
        symbol_matches: dict[str, list[dict]] = {}
        symbols = (
            symbol_candidates if symbol_candidates is not None
            else self.index.search_symbols(query, limit=max(100, limit * 8))
        )
        for symbol in symbols:
            symbol_matches.setdefault(str(symbol["module_id"]), []).append(symbol)
        entries: dict[str, list[dict]] = {}
        candidates = (
            entry_candidates if entry_candidates is not None
            else self.index.entrypoints(limit=2000)
        )
        for item in candidates:
            entries.setdefault(str(item["module_id"]), []).append(item)

        preliminary_ranked: list[tuple[float, str, dict]] = []
        for module_id, files in grouped.items():
            records = [self._records[path] for path in files]
            root = module_id.removeprefix("module:")
            label = Path(root).name if len(files) > 1 else str(records[0]["label"])
            languages = sorted({str(record.get("language") or "unknown") for record in records})
            matched = symbol_matches.get(module_id, [])
            entry_files = sorted({str(item["path"]) for item in entries.get(module_id, [])})
            module_kinds = {self._module_kind(path) for path in files}
            kind = next((value for value in (
                "test", "service", "application", "tooling", "config", "library",
            ) if value in module_kinds), "package")
            purpose = f"{kind} module {label}"
            preliminary = structural_match_score(
                query, label, module_id, root, purpose, " ".join(languages),
                " ".join(entry_files),
                " ".join(str(item["qualified_name"]) for item in matched),
            )
            if preliminary <= 0 and not matched:
                continue
            preliminary_ranked.append((-preliminary, module_id, {
                "module_id": module_id, "label": label, "root": root,
                "kind": kind, "languages": languages, "files": sorted(files),
                "entry_files": entry_files, "purpose": purpose,
                "matched": matched, "records": records,
            }))

        preliminary_ranked.sort(key=lambda item: (item[0], item[1]))
        ranked: list[tuple[float, str, dict]] = []
        shortlist = preliminary_ranked[: max(20, int(limit) * 2)]
        for _preliminary, module_id, candidate in shortlist:
            matched = candidate.pop("matched")
            records = candidate.pop("records")
            top_symbols = matched[:8] or self._top_symbols(module_id, limit=8)
            score = structural_match_score(
                query, candidate["label"], module_id, candidate["root"],
                candidate["purpose"], " ".join(candidate["languages"]),
                " ".join(str(item["name"]) for item in top_symbols),
                " ".join(candidate["entry_files"]),
                " ".join(str(item["qualified_name"]) for item in matched),
            )
            if score <= 0:
                continue
            confidence = min(float(record.get("confidence", 0.0)) for record in records)
            payload = {
                **candidate, "top_symbols": top_symbols, "match_score": score,
                "confidence": round(confidence, 3),
                "source": "repository-graph-structural-ranking",
            }
            ranked.append((-score, module_id, payload))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [item for _score, _module_id, item in ranked[: max(1, limit)]]

    @staticmethod
    def _is_test(path: str) -> bool:
        value = Path(path)
        return (
            any(part in _TEST_DIRS for part in value.parts)
            or value.name.startswith("test_")
            or any(token in value.name for token in (".test.", ".spec."))
            or value.name.endswith("_test.go")
        )

    def related_tests(self, module: str, *, limit: int = 100) -> list[str]:
        _module_id, files, _selected = self._select_module(module)
        stems = {Path(name).stem.removeprefix("test_") for name in files}
        candidates: set[str] = {
            source for source, dependencies in self._graph.items()
            if set(files) & set(dependencies) and self._is_test(source)
        }
        for path in self._records:
            if not self._is_test(path):
                continue
            test_stem = Path(path).stem.removeprefix("test_")
            test_stem = test_stem.removesuffix("_test").split(".", 1)[0]
            if test_stem in stems:
                candidates.add(path)
        for name in files:
            for symbol in self.index.file_symbols(name, limit=200):
                for edge in self.index.callers(str(symbol["symbol_id"]), limit=limit):
                    if self._is_test(edge.path):
                        candidates.add(edge.path)
        candidates.difference_update(files)
        return sorted(candidates)[: max(0, int(limit))]

    @staticmethod
    def _module_kind(path: str) -> str:
        value = Path(path)
        if RepositoryGraph._is_test(path):
            return "test"
        if value.name in {"main.py", "main.go", "app.py", "cli.py", "server.ts", "server.js"}:
            return "application"
        if any(part in {"tools", "scripts", "bin"} for part in value.parts):
            return "tooling"
        if value.suffix in {".json", ".yaml", ".yml", ".toml"}:
            return "config"
        if value.name in {"__init__.py", "index.ts", "index.js", "lib.rs"}:
            return "library"
        if any(part in {"services", "service", "api"} for part in value.parts):
            return "service"
        return "package"

    def _top_symbols(self, module_id: str, limit: int = 12) -> list[dict]:
        return self.index.top_symbols(module_id, limit=limit)

    def module_context(self, module: str) -> dict:
        module_id, files, selected = self._select_module(module)
        file_set = set(files)
        records = [self._records[name] for name in files]
        dependencies = sorted({
            target for name in files for target in self._graph.get(name, ())
            if target not in file_set
        })
        dependents = sorted({
            source for target in file_set for source in self._reverse_graph.get(target, ())
            if source not in file_set
        })
        symbols = [
            item for name in files for item in self.index.file_symbols(name, limit=200)
        ]
        entrypoints = self.index.entrypoints(limit=1000, module_id=module_id)
        related_tests = self.related_tests(module_id)
        top_symbols = self._top_symbols(module_id)
        process_ids = [
            f"process:{item['symbol_id']}:depth=4" for item in entrypoints
        ]
        changed = set(self._changed_paths())
        changed_files = sorted(file_set & changed)
        kinds = [self._module_kind(name) for name in files]
        kind = next((value for value in (
            "test", "service", "application", "tooling", "config", "library",
        ) if value in kinds), "package")
        languages = sorted({str(item.get("language") or "unknown") for item in records})
        root = module_id.removeprefix("module:")
        label = Path(root).name if len(files) > 1 else str(records[0]["label"])
        confidence = min(
            [float(record.get("confidence", 0.0)) for record in records]
            + [float(item.get("confidence", 0.0)) for item in symbols]
        ) if symbols else min(float(record.get("confidence", 0.0)) for record in records)
        capsule = ModuleCapsule(
            module_id=module_id, root=root, label=label, kind=kind,
            languages=tuple(languages), files=tuple(files),
            entry_files=tuple(sorted({str(item["path"]) for item in entrypoints})),
            top_symbols=tuple(top_symbols), dependencies=tuple(dependencies),
            dependents=tuple(dependents), related_tests=tuple(related_tests),
            process_ids=tuple(process_ids), confidence=round(confidence, 3),
        )
        return {
            **capsule.to_dict(),
            # Legacy selector plus explicit V3 identity.
            "module": selected, "path": selected,
            "file_count": len(files), "source_file_count": len(files),
            "language": languages[0] if len(languages) == 1 else "mixed",
            "summary": {
                "purpose": f"{kind} module {label}",
                "symbol_count": len(symbols), "dependency_count": len(dependencies),
            },
            "dependency_ids": sorted({self._records[item]["module_id"] for item in dependencies}),
            "dependent_ids": sorted({self._records[item]["module_id"] for item in dependents}),
            "symbols": symbols, "exports": [item for item in symbols if item.get("exported")],
            "entrypoints": entrypoints,
            "processes": process_ids, "changed_files": changed_files,
            "outgoing_calls": [
                self.index._edge_dict(item)
                for name in files for item in self.index.file_calls(name, limit=200)
            ],
            "capability_tier": min(
                (str(record.get("capability_tier") or "") for record in records),
                key=lambda value: {"ast-native": 3, "tree-sitter": 2, "lsp-augmented": 2, "lexical-fallback": 1}.get(value, 0),
            ),
            "freshness": "indexed", "generation": self._generation,
            "source": "repository-graph+identity-index", "capsule_version": 2,
            "module_context_version": 3,
        }

    def relationship_scan(self, module: str, *, limit: int = 30) -> dict:
        context = self.module_context(module)
        related = sorted(set(context["dependencies"]) | set(context["dependents"]))
        return {
            "module": context["module"], "module_id": context["module_id"],
            "related": related[: max(0, int(limit))],
        }

    def cycles(self) -> tuple[tuple[str, ...], ...]:
        index = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lows: dict[str, int] = {}
        result: list[tuple[str, ...]] = []

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = lows[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for target in self._graph.get(node, ()):
                if target not in indices:
                    visit(target)
                    lows[node] = min(lows[node], lows[target])
                elif target in on_stack:
                    lows[node] = min(lows[node], indices[target])
            if lows[node] == indices[node]:
                component = []
                while stack:
                    item = stack.pop()
                    on_stack.remove(item)
                    component.append(item)
                    if item == node:
                        break
                if len(component) > 1:
                    result.append(tuple(sorted(component)))

        for module in sorted(self._graph):
            if module not in indices:
                visit(module)
        return tuple(sorted(result))

    def _changed_paths(self) -> list[str]:
        try:
            completed = subprocess.run(
                ["git", "status", "--short", "--untracked-files=all"],
                cwd=self.workspace, capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        result = []
        for line in completed.stdout.splitlines():
            value = line[3:].strip() if len(line) > 3 else ""
            if " -> " in value:
                value = value.split(" -> ", 1)[1]
            if value:
                result.append(value.strip('"'))
        return list(dict.fromkeys(result))

    def changed_scope(
        self, *, limit: int = 100, changed_paths: list[str] | None = None,
        max_depth: int = 4, node_limit: int | None = None,
        time_budget_ms: float = 100.0, confidence_threshold: float = 0.0,
    ) -> dict:
        changed = list(dict.fromkeys(changed_paths or self._changed_paths()))
        modules = [name for name in changed if name in self._graph]
        budget = max(1, int(node_limit or limit))
        changed_symbols = [
            item for name in modules for item in self.index.file_symbols(name, limit=budget)
        ]
        direct_edges = []
        for symbol in changed_symbols:
            direct_edges.extend(self.index.callers(symbol["symbol_id"], limit=budget))
        direct_edges = [item for item in direct_edges if item.confidence >= confidence_threshold]
        transitive_symbols: set[str] = set()
        transitive_edges: list[dict] = []
        queue = [(item.caller_symbol_id, 1) for item in direct_edges]
        started = time.perf_counter()
        truncated = False
        while queue:
            if len(transitive_symbols) >= budget or (time.perf_counter() - started) * 1000 >= time_budget_ms:
                truncated = True
                break
            symbol_id, depth = queue.pop(0)
            if symbol_id in transitive_symbols:
                continue
            transitive_symbols.add(symbol_id)
            if depth >= max(0, max_depth):
                continue
            for edge in self.index.callers(symbol_id, limit=budget):
                if edge.confidence < confidence_threshold:
                    continue
                transitive_edges.append(self.index._edge_dict(edge))
                queue.append((edge.caller_symbol_id, depth + 1))

        dependent_modules: set[str] = set()
        frontier = list(modules)
        depth = 0
        while frontier and depth < max_depth and len(dependent_modules) < budget:
            next_frontier = []
            for target in frontier:
                for source in self._reverse_graph.get(target, ()):
                    if source not in dependent_modules:
                        dependent_modules.add(source)
                        next_frontier.append(source)
            frontier = next_frontier
            depth += 1

        related_tests = sorted({
            test for module in modules for test in self.related_tests(module, limit=budget)
        } | {path for path in dependent_modules if self._is_test(path)})
        public_symbols = [item for item in changed_symbols if item.get("exported")]
        risk_points = (
            len(direct_edges) + len(transitive_symbols) + len(dependent_modules)
            + len(public_symbols) * 3
        )
        risk = "high" if risk_points >= 12 else "medium" if risk_points >= 4 else "low"
        direct_callers = sorted({
            f"{edge.path}:{edge.caller}" for edge in direct_edges if edge.path not in modules
        })
        return {
            "changed": changed[:limit], "changed_files": changed[:limit],
            "modules": modules[:limit],
            "module_ids": [self._records[name]["module_id"] for name in modules[:limit]],
            "changed_symbols": [item["name"] for item in changed_symbols[:limit]],
            "changed_symbol_ids": [item["symbol_id"] for item in changed_symbols[:limit]],
            "direct_callers": direct_callers[:limit],
            "impacted_callers": direct_callers[:limit],
            "transitive_callers": sorted(transitive_symbols)[:limit],
            "transitive_edges": transitive_edges[:limit],
            "dependent_modules": sorted(dependent_modules)[:limit],
            "dependent_module_ids": sorted({
                str(self._records[path]["module_id"])
                for path in dependent_modules if path in self._records
            })[:limit],
            "related": sorted(dependent_modules)[:limit],
            "related_tests": related_tests[:limit],
            "public_api_exposure": [item["symbol_id"] for item in public_symbols[:limit]],
            "risk": risk, "truncated": truncated,
            "budget": {
                "max_depth": max_depth, "node_limit": budget,
                "time_budget_ms": time_budget_ms,
                "confidence_threshold": confidence_threshold,
            },
            "confidence": min(
                [float(item["confidence"]) for item in changed_symbols]
                + [float(item.confidence) for item in direct_edges] + [1.0]
            ),
            "freshness": "indexed", "generation": self._generation,
            "source": "module+identity-call-graph", "capsule_version": 3,
        }

    def _select_module(self, module: str) -> tuple[str, list[str], str]:
        value = str(module).replace("\\", "/").lstrip("./")
        if value.startswith("module:"):
            matches = sorted(
                path for path, item in self._records.items() if item["module_id"] == value
            )
            if matches:
                return value, matches, value
        if value not in self._graph:
            raise KeyError(f"Unknown repository module: {module}")
        module_id = str(self._records[value]["module_id"])
        matches = sorted(
            path for path, item in self._records.items() if item["module_id"] == module_id
        )
        return module_id, matches, value

    def _require_module(self, module: str) -> str:
        _module_id, _files, selected = self._select_module(module)
        if selected.startswith("module:"):
            return _files[0]
        return selected


__all__ = ["GraphBuildStats", "ModuleCapsule", "RepositoryGraph"]
