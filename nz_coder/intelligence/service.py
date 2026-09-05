"""Workspace-owned production lifecycle for repository intelligence."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Event, RLock, Thread
import atexit
import os
import time
import weakref

from nz_coder.intelligence.code_index import (
    PersistentCodeIndex,
    is_excluded_directory,
    structural_match_score,
)
from nz_coder.intelligence.repository_graph import RepositoryGraph
from nz_coder.lsp.servers import language_for_path


@dataclass(frozen=True)
class RepoIntelligenceState:
    status: str = "cold"
    files_indexed: int = 0
    files_omitted: int = 0
    error: str = ""
    generation: int = 0
    incremental_batches: int = 0
    last_updated_paths: tuple[str, ...] = ()
    cold_build_ms: float = 0.0
    incremental_update_ms: float = 0.0
    symbols_indexed: int = 0
    call_edges: int = 0
    worker_queue: int = 0
    watcher_backend: str = "none"
    lsp_augmented_calls: int = 0
    languages: tuple[str, ...] = ()


class RepoIntelligenceService:
    """Own one workspace index, graph, watcher, cache, and metrics stream."""

    def __init__(self, workspace: Path, *, query_cache_size: int = 256) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("Repo intelligence workspace must be a directory")
        self.index = PersistentCodeIndex(self.workspace)
        self.graph: RepositoryGraph | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nz-repo-index")
        self._future: Future | None = None
        self._state = RepoIntelligenceState()
        self._lock = RLock()
        self._watch_stop = Event()
        self._watch_thread: Thread | None = None
        self._closed = False
        self._deferred_watch: tuple[float, float, int] | None = None
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._process_catalog: dict[str, dict] = {}
        self._cache_size = max(16, int(query_cache_size))
        self._cache_hits = 0
        self._cache_misses = 0
        self._fallback_count = 0
        self._query_count = 0
        self._query_ms = 0.0
        self._tracers: weakref.WeakSet = weakref.WeakSet()
        self._semantic_index = None
        self._semantic_provider_identity = ""
        self._semantic_queries = 0
        self._semantic_builds = 0

    def attach_tracer(self, tracer: object) -> None:
        """Fan workspace metrics out to each Agent trace without owning it."""
        if callable(getattr(tracer, "log", None)):
            with self._lock:
                self._tracers.add(tracer)

    def detach_tracer(self, tracer: object) -> None:
        with self._lock:
            self._tracers.discard(tracer)

    def _emit(self, event: str, **payload) -> None:
        with self._lock:
            tracers = tuple(self._tracers)
        for tracer in tracers:
            try:
                tracer.log(event, workspace=str(self.workspace), **payload)
            except Exception:
                continue

    @property
    def state(self) -> RepoIntelligenceState:
        with self._lock:
            return self._state

    def prewarm(self, *, max_files: int = 5000) -> Future:
        """Schedule one non-blocking cold build for the workspace."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Repo intelligence service is closed")
            if self._future is not None and not self._future.done():
                return self._future
            previous = self._state
            self._state = replace(previous, status="warming", error="", worker_queue=1)
            self._future = self._executor.submit(self._build, max(1, int(max_files)))
            self._emit("repo_intelligence_prewarm", max_files=max_files)
            return self._future

    def _build(self, max_files: int) -> RepoIntelligenceState:
        started = time.perf_counter()
        try:
            _entries, stats = self.index.scan(self.workspace, max_files=max_files)
            snapshot = self.index.snapshot()
            graph = RepositoryGraph(self.workspace, index=self.index)
            graph.build(max_files=max_files, snapshot=snapshot)
            metrics = self.index.metrics()
            previous = self.state
            state = RepoIntelligenceState(
                status="ready", files_indexed=metrics["files_indexed"],
                files_omitted=stats.omitted,
                generation=stats.generation,
                incremental_batches=previous.incremental_batches,
                cold_build_ms=round((time.perf_counter() - started) * 1000, 3),
                symbols_indexed=metrics["symbols_indexed"],
                call_edges=metrics["call_edges"], worker_queue=0,
                watcher_backend=previous.watcher_backend,
                lsp_augmented_calls=previous.lsp_augmented_calls,
                languages=self.index.languages(),
            )
            with self._lock:
                self.graph = graph
                self._cache.clear()
                self._process_catalog.clear()
        except Exception as exc:
            previous = self.state
            state = replace(
                previous, status="failed", error=f"{type(exc).__name__}: {exc}",
                worker_queue=0,
            )
        with self._lock:
            self._state = state
        self._emit(
            "repo_intelligence_cold_build",
            status=state.status, generation=state.generation,
            cold_build_ms=state.cold_build_ms, files_indexed=state.files_indexed,
            symbols_indexed=state.symbols_indexed, call_edges=state.call_edges,
            files_omitted=state.files_omitted, error=state.error,
        )
        return state

    def wait_ready(self, timeout: float | None = None) -> RepoIntelligenceState:
        """Wait for an existing prewarm; never starts work implicitly."""
        with self._lock:
            future = self._future
        if future is None:
            return self.state
        state = future.result(timeout=timeout)
        config = self._deferred_watch
        if config is not None and state.status == "ready":
            self.start_watching(interval=config[0], debounce=config[1], max_files=config[2])
        return state

    def submit_bounded_query(self, callback) -> Future:
        """Schedule a query on the workspace worker for a caller-owned wait budget."""
        if not callable(callback):
            raise TypeError("repository query callback must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("Repo intelligence service is closed")
        return self._executor.submit(callback)

    @property
    def semantic_available(self) -> bool:
        with self._lock:
            semantic = self._semantic_index
        return bool(semantic is not None and semantic.available)

    @property
    def semantic_status(self) -> str:
        with self._lock:
            semantic = self._semantic_index
        if semantic is None:
            return "unconfigured"
        provider = getattr(semantic, "provider", None)
        return str(getattr(provider, "status", "ready"))

    def configure_semantic(self, provider, *, max_chunks: int = 10_000) -> None:
        """Attach an optional generation-aware experiment to this workspace."""
        from nz_coder.intelligence.semantic import RepositorySemanticIndex

        identity = str(getattr(provider, "identity", "") or type(provider).__name__)
        prepare = getattr(provider, "prepare", None)
        if callable(prepare):
            prepare()
        with self._lock:
            if self._semantic_provider_identity == identity and self._semantic_index is not None:
                return
            self._semantic_index = RepositorySemanticIndex(
                self.workspace, self.index, provider, max_chunks=max_chunks,
            )
            self._semantic_provider_identity = identity
            self._cache.clear()
        self._emit("repo_semantic_configured", provider=identity, experimental=True)

    def disable_semantic(self) -> None:
        with self._lock:
            self._semantic_index = None
            self._semantic_provider_identity = ""
            self._cache.clear()

    def _query_ready(self, wait_budget_ms: float) -> RepoIntelligenceState:
        state = self.state
        if state.status == "warming" and wait_budget_ms > 0:
            try:
                return self.wait_ready(timeout=max(0.001, wait_budget_ms / 1000))
            except FutureTimeout:
                return self.state
        return state

    def start_watching(
        self, *, interval: float = 1.0, debounce: float = 0.5, max_files: int = 5000,
    ) -> None:
        """Start native events when available, otherwise adaptive polling."""
        failure: Exception | None = None
        with self._lock:
            if self._closed or (self._watch_thread and self._watch_thread.is_alive()):
                return
            self._watch_stop.clear()
            try:
                if not self.workspace.is_dir():
                    raise OSError("repository workspace is unavailable")
                try:
                    import watchfiles  # noqa: F401
                except ImportError:
                    initial = self._fingerprints(max(1, int(max_files)))
                    target = self._poll_watch_loop
                    args = (
                        max(0.25, float(interval)),
                        max(0.0, float(debounce)),
                        max(1, int(max_files)),
                        initial,
                    )
                    backend = "adaptive-polling"
                else:
                    target = self._native_watch_loop
                    args = (
                        max(0.01, float(interval)), max(0.0, float(debounce)),
                        max(1, int(max_files)), self._indexed_fingerprints(),
                    )
                    backend = "watchfiles"
                self._state = replace(self._state, watcher_backend=backend)
                self._watch_thread = Thread(
                    target=target, args=args, name="nz-repo-watch", daemon=True,
                )
                self._watch_thread.start()
            except Exception as exc:
                failure = exc
                self._state = replace(self._state, watcher_backend="none")
                self._watch_thread = None
        if failure is not None:
            self._emit(
                "repo_intelligence_watcher_failed",
                error=f"{type(failure).__name__}: {failure}",
            )
            return
        self._emit("repo_intelligence_watcher_started", backend=backend)

    def _eligible_event(self, value: str) -> str | None:
        try:
            target = Path(value).resolve()
            relative = target.relative_to(self.workspace).as_posix()
        except (OSError, ValueError):
            return None
        if any(is_excluded_directory(part) for part in Path(relative).parts):
            return None
        if target.exists() and not target.is_file():
            return None
        if language_for_path(target) is None:
            return None
        return relative

    def _native_watch_loop(
        self, interval: float, debounce: float, max_files: int,
        known: dict[str, tuple[int, int]],
    ) -> None:
        try:
            from watchfiles import watch

            # start_watching() returns after launching this thread, before the
            # native backend has necessarily installed its OS watch. Reconcile
            # that startup window so an immediate file create cannot vanish.
            current = self._fingerprints(max_files)
            startup_changes = tuple(sorted(
                path
                for path in set(known) | set(current)
                if known.get(path) != current.get(path)
            ))
            if startup_changes:
                self._apply_incremental(startup_changes, max_files)
            known = current
            for changes in watch(
                self.workspace, stop_event=self._watch_stop,
                debounce=max(1, int(debounce * 1000)),
                step=min(100, max(50, int(interval * 1000))), raise_interrupt=False,
            ):
                paths = tuple(sorted({
                    relative for _change, path in changes
                    if (relative := self._eligible_event(str(path))) is not None
                }))
                if paths:
                    self._apply_incremental(paths, max_files)
                if self._watch_stop.is_set():
                    break
        except Exception:
            if self._watch_stop.is_set() or not self.workspace.is_dir():
                with self._lock:
                    self._state = replace(self._state, watcher_backend="none")
                self._emit(
                    "repo_intelligence_watcher_stopped",
                    reason="workspace-unavailable",
                )
                return
            # Native watcher failure is observable and degrades to a slower poll.
            with self._lock:
                self._state = replace(self._state, watcher_backend="adaptive-polling")
            self._emit(
                "repo_intelligence_watcher_fallback", backend="adaptive-polling",
            )
            self._poll_watch_loop(
                max(0.25, interval), debounce, max_files, known,
            )

    def _indexed_fingerprints(self) -> dict[str, tuple[int, int]]:
        return {
            entry.path: entry.fingerprint for entry in self.index.snapshot().files
        }

    def _fingerprints(self, max_files: int) -> dict[str, tuple[int, int]]:
        files, _omitted = self.index._source_files(self.workspace, max_files)
        result = {}
        for path in files:
            try:
                stat = path.stat()
                result[path.relative_to(self.workspace).as_posix()] = (
                    stat.st_mtime_ns, stat.st_size,
                )
            except OSError:
                continue
        return result

    def _poll_watch_loop(
        self, interval: float, debounce: float, max_files: int,
        known: dict[str, tuple[int, int]],
    ) -> None:
        pending: set[str] = set()
        last_change = 0.0
        unchanged_rounds = 0
        current_interval = interval
        while not self._watch_stop.wait(current_interval):
            try:
                current = self._fingerprints(max_files)
            except OSError:
                with self._lock:
                    self._state = replace(self._state, watcher_backend="none")
                self._emit(
                    "repo_intelligence_watcher_stopped",
                    reason="workspace-unavailable",
                )
                return
            changed = {
                path for path in set(known) | set(current)
                if known.get(path) != current.get(path)
            }
            if changed:
                pending.update(changed)
                known = current
                last_change = time.monotonic()
                unchanged_rounds = 0
                current_interval = interval
            else:
                unchanged_rounds += 1
                current_interval = min(5.0, interval * (1 + unchanged_rounds // 4))
            if pending and time.monotonic() - last_change >= debounce:
                paths = tuple(sorted(pending))
                pending.clear()
                self._apply_incremental(paths, max_files)

    def _apply_incremental(self, paths: tuple[str, ...], max_files: int) -> None:
        started = time.perf_counter()
        previous = self.state
        with self._lock:
            self._state = replace(previous, worker_queue=1)
        try:
            stats = self.index.update_paths(list(paths))
            snapshot = self.index.snapshot(list(paths))
            graph = self.graph or RepositoryGraph(self.workspace, index=self.index)
            deleted = [path for path in paths if not (self.workspace / path).is_file()]
            graph_stats = graph.update_paths(paths, deleted, snapshot=snapshot)
            metrics = self.index.metrics()
            state = RepoIntelligenceState(
                status="ready", files_indexed=metrics["files_indexed"],
                files_omitted=previous.files_omitted,
                generation=stats.generation,
                incremental_batches=previous.incremental_batches + 1,
                last_updated_paths=paths, cold_build_ms=previous.cold_build_ms,
                incremental_update_ms=round((time.perf_counter() - started) * 1000, 3),
                symbols_indexed=metrics["symbols_indexed"],
                call_edges=metrics["call_edges"], worker_queue=0,
                watcher_backend=previous.watcher_backend,
                lsp_augmented_calls=previous.lsp_augmented_calls,
                languages=self.index.languages(),
            )
            with self._lock:
                self.graph = graph
                self._cache.clear()
                self._process_catalog.clear()
        except Exception as exc:
            state = replace(
                previous, status="failed", error=f"{type(exc).__name__}: {exc}",
                last_updated_paths=paths, worker_queue=0,
            )
        with self._lock:
            self._state = state
        self._emit(
            "repo_intelligence_incremental_update",
            status=state.status, generation=state.generation,
            incremental_update_ms=state.incremental_update_ms,
            paths=list(paths), indexed=stats.indexed if "stats" in locals() else 0,
            removed=stats.removed if "stats" in locals() else 0,
            calls_resolved=stats.calls_resolved if "stats" in locals() else 0,
            relationships_updated=(
                graph_stats.relationships_updated if "graph_stats" in locals() else 0
            ),
            worker_queue=state.worker_queue, error=state.error,
        )

    def _cached_query(self, operation: str, args: tuple, compute) -> dict:
        generation = self.state.generation
        key = (generation, operation, *args)
        started = time.perf_counter()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self._cache_hits += 1
                self._emit(
                    "repo_intelligence_query", operation=operation,
                    generation=generation, cache_hit=True, query_ms=0.0,
                )
                return {**cached, "cache_hit": True}
            self._cache_misses += 1
        result = compute()
        elapsed = (time.perf_counter() - started) * 1000
        result = {**result, "cache_hit": False, "query_ms": round(elapsed, 3)}
        with self._lock:
            self._query_count += 1
            self._query_ms += elapsed
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        self._emit(
            "repo_intelligence_query", operation=operation,
            generation=generation, cache_hit=False, query_ms=round(elapsed, 3),
        )
        return result

    def _fallback(self, operation: str, query: str, state: RepoIntelligenceState) -> dict:
        with self._lock:
            self._fallback_count += 1
        self._emit(
            "repo_intelligence_fallback", operation=operation,
            query=query, freshness=state.status, generation=state.generation,
        )
        return {
            "operation": operation, "symbol": query, "definition": None,
            "callers": [], "callees": [], "freshness": state.status,
            "fallback": True,
            "confidence": "unavailable", "confidence_score": 0.0,
            "source": "repo-intelligence-service",
            "warnings": [
                (
                    "Repository intelligence is not ready; use read, grep, repo_map, or LSP meanwhile."
                    if state.status != "ready" else
                    "Repository intelligence query fell back; use read, grep, repo_map, or LSP meanwhile."
                )
            ],
        }

    def symbol_context(
        self, name: str, limit: int = 30, *, wait_budget_ms: float = 50.0,
    ) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready":
            return self._fallback("symbol_context", name, state)
        def compute() -> dict:
            result = self.index.symbol_context(name, limit)
            definition = result.get("definition")
            graph = self.graph
            if definition and graph is not None:
                path = str(definition["path"])
                result["related_tests"] = graph.related_tests(path, limit=limit)
                result["changed"] = path in graph._changed_paths()
            return result

        return self._cached_query(
            "symbol_context", (name, int(limit)),
            compute,
        )

    def module_context(
        self, module: str, *, wait_budget_ms: float = 50.0,
    ) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready" or self.graph is None:
            return self._fallback("module_context", module, state)
        return self._cached_query(
            "module_context", (module,), lambda: self.graph.module_context(module),
        )

    def overview(self, *, limit: int = 50, wait_budget_ms: float = 50.0) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready" or self.graph is None:
            return self._fallback("overview", "", state)
        return self._cached_query(
            "overview", (limit,), lambda: self.graph.overview(limit=limit),
        )

    def relationship_scan(
        self, module: str, *, limit: int = 30, wait_budget_ms: float = 50.0,
    ) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready" or self.graph is None:
            return self._fallback("relationship_scan", module, state)
        return self._cached_query(
            "relationship_scan", (module, limit),
            lambda: self.graph.relationship_scan(module, limit=limit),
        )

    def cyclic_dependencies(
        self, *, limit: int = 50, wait_budget_ms: float = 50.0,
    ) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready" or self.graph is None:
            return self._fallback("cyclic_dependencies", "", state)
        return self._cached_query(
            "cyclic_dependencies", (limit,),
            lambda: {
                "cycles": self.graph.cycles()[:limit], "freshness": "indexed",
                "generation": state.generation, "source": "repository-graph",
            },
        )

    def search_symbols(
        self, query: str, *, limit: int = 30, wait_budget_ms: float = 50.0,
    ) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready":
            return self._fallback("symbol_search", query, state)
        return self._cached_query(
            "symbol_search", (query, limit),
            lambda: {
                "query": query, "matches": self.index.search_symbols(query, limit),
                "freshness": "indexed", "generation": state.generation,
                "source": "structural-symbol-search",
            },
        )

    def intent_lookup(
        self, query: str, *, kind: str = "auto", limit: int = 20,
        wait_budget_ms: float = 50.0,
    ) -> dict:
        """Search Symbol, Module, and bounded Process candidates in one query."""
        selected_kind = str(kind or "auto").casefold()
        if selected_kind not in {"auto", "symbol", "module", "process"}:
            raise ValueError("lookup kind must be auto, symbol, module, or process")
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready" or self.graph is None:
            return self._fallback("lookup", query, state)

        def compute() -> dict:
            results: list[dict] = []
            bounded = max(1, int(limit))
            symbol_candidates = self.index.search_symbols(
                query, limit=max(100, bounded * 8),
            )
            entry_candidates = []
            for symbol in symbol_candidates:
                classified = self.index.entrypoint_kind(symbol)
                if classified is None:
                    continue
                entry = dict(symbol)
                entry["entry_kind"] = classified[0]
                entry_candidates.append(entry)
            if selected_kind in {"auto", "symbol"}:
                for symbol in symbol_candidates[: bounded * 3]:
                    results.append({
                        "kind": "symbol",
                        "title": f"{symbol['name']} ({symbol['kind']})",
                        "locator": f"{symbol['path']}:{symbol['line']}",
                        "snippet": symbol.get("signature") or symbol["qualified_name"],
                        "score": float(symbol["match_score"]),
                        "identity": symbol["symbol_id"],
                        "confidence": float(symbol["confidence"]),
                        "source": "structural-symbol-ranking",
                    })
            if selected_kind in {"auto", "module"}:
                for module in self.graph.search_modules(
                    query, limit=bounded * 2,
                    symbol_candidates=symbol_candidates,
                    entry_candidates=entry_candidates,
                ):
                    names = ", ".join(
                        str(item["name"]) for item in module["top_symbols"][:5]
                    )
                    snippet = module["purpose"]
                    if names:
                        snippet += f"; top symbols: {names}"
                    results.append({
                        "kind": "module", "title": module["label"],
                        "locator": module["root"], "snippet": snippet,
                        "score": float(module["match_score"]),
                        "identity": module["module_id"],
                        "confidence": float(module["confidence"]),
                        "source": "structural-module-ranking",
                    })
            if selected_kind in {"auto", "process"}:
                with self._lock:
                    cached_processes = tuple(self._process_catalog.values())
                process_candidates: dict[str, dict] = {}
                for entry in entry_candidates:
                    score = structural_match_score(
                        query, str(entry["name"]), str(entry["qualified_name"]),
                        str(entry.get("signature") or ""), str(entry["path"]),
                        str(entry["module_id"]), str(entry.get("entry_kind") or ""),
                    )
                    if score <= 0:
                        continue
                    identity = f"process:{entry['symbol_id']}"
                    process_candidates[identity] = {
                        "kind": "process", "title": entry["qualified_name"],
                        "locator": f"{entry['path']}:{entry['line']}",
                        "snippet": f"{entry.get('entry_kind', 'entry')} process candidate",
                        "score": score, "identity": identity,
                        "entry_symbol_id": entry["symbol_id"],
                        "confidence": float(entry["confidence"]),
                        "source": "entrypoint-process-candidate", "materialized": False,
                    }
                for process in cached_processes:
                    score = structural_match_score(
                        query, str(process.get("label") or ""),
                        str(process.get("entry_file") or ""),
                        str(process.get("entry_symbol_id") or ""),
                        " ".join(str(item) for item in process.get("module_ids", ())),
                    )
                    if score <= 0:
                        continue
                    identity = str(process["process_id"])
                    process_candidates[identity] = {
                        "kind": "process", "title": process["label"],
                        "locator": process["entry_file"],
                        "snippet": (
                            f"bounded process with {len(process.get('steps', ()))} steps"
                        ),
                        "score": score, "identity": identity,
                        "entry_symbol_id": process["entry_symbol_id"],
                        "confidence": float(process["confidence"]),
                        "source": "cached-process-capsule", "materialized": True,
                    }
                results.extend(process_candidates.values())
            results.sort(key=lambda item: (-float(item["score"]), item["kind"], item["title"]))
            return {
                "query": query, "kind": selected_kind,
                "items": results[:bounded], "freshness": "indexed",
                "generation": state.generation,
                "source": "unified-structural-intent-lookup",
                "embedding": False,
            }

        return self._cached_query(
            "lookup", (query, selected_kind, int(limit)), compute,
        )

    def semantic_search(
        self, query: str, *, path: str | None = None, limit: int = 10,
        wait_budget_ms: float = 50.0,
    ) -> dict:
        """Run the optional embedding experiment and retain structural identities."""
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready":
            return self._fallback("semantic_search", query, state)
        with self._lock:
            semantic = self._semantic_index
        if semantic is None or not semantic.available:
            result = self._fallback("semantic_search", query, state)
            result["warnings"].append(
                "Embedding retrieval is optional and unavailable; use structural lookup, grep, or LSP."
            )
            provider_error = getattr(
                getattr(semantic, "provider", None), "load_error", "",
            ) if semantic is not None else ""
            if provider_error:
                result["warnings"].append(f"Provider load error: {provider_error}")
            return result

        def compute() -> dict:
            before_generation = semantic.generation
            items = [
                item.to_dict() for item in semantic.search(
                    query, path=path, limit=max(1, min(50, int(limit))),
                )
            ]
            with self._lock:
                self._semantic_queries += 1
                if before_generation != semantic.generation:
                    self._semantic_builds += 1
            return {
                "query": query, "path": path, "items": items,
                "freshness": "indexed", "generation": state.generation,
                "source": "optional-embedding-semantic-index",
                "embedding": True,
                "provider": self._semantic_provider_identity,
            }

        # Model loading can involve a local cache miss or a remote model fetch.
        # Keep that optional path outside the Agent's critical request thread.
        if wait_budget_ms > 0:
            completed = Event()
            outcome: list[object] = []

            def run_query() -> None:
                try:
                    outcome.append(self._cached_query(
                        "semantic_search", (query, path or "", int(limit)), compute,
                    ))
                except Exception as exc:  # provider failures become explicit fallback
                    outcome.append(exc)
                finally:
                    completed.set()

            # A provider may be loading a model or waiting on a network cache.
            # The optional experiment must never keep the process alive or block
            # the workspace worker when its caller's budget expires.
            Thread(
                target=run_query, name="nz-semantic-query", daemon=True,
            ).start()
            completed.wait(timeout=max(0.001, float(wait_budget_ms) / 1000))
            if completed.is_set() and outcome:
                result = outcome[0]
                if isinstance(result, Exception):
                    with self._lock:
                        self._fallback_count += 1
                    fallback = self._fallback("semantic_search", query, state)
                    fallback["warnings"].append(
                        f"Semantic provider failed: {type(result).__name__}: {result}"
                    )
                    return fallback
                return result
            self._emit(
                "repo_intelligence_fallback", operation="semantic_search",
                query=query, freshness=state.status, generation=state.generation,
                reason="query-timeout", wait_budget_ms=wait_budget_ms,
            )
            with self._lock:
                self._fallback_count += 1
            result = self._fallback("semantic_search", query, state)
            result["warnings"].append(
                f"Semantic retrieval exceeded its {float(wait_budget_ms):g}ms wait budget; "
                "use structural lookup, grep, or LSP meanwhile."
            )
            return result
        return self._cached_query(
            "semantic_search", (query, path or "", int(limit)), compute,
        )

    def process_context(
        self, entry: str, *, max_depth: int = 4, limit: int = 50,
        time_budget_ms: float = 100.0, wait_budget_ms: float = 50.0,
    ) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready":
            return self._fallback("process_context", entry, state)
        def compute() -> dict:
            result = self.index.process_context(
                entry, max_depth=max_depth, limit=limit, time_budget_ms=time_budget_ms,
            )
            graph = self.graph
            if graph is not None:
                paths = {
                    str(item.get("path") or item.get("file_path") or "")
                    for item in result.get("nodes", [])
                }
                result["related_tests"] = sorted({
                    test for path in paths if path in graph.modules()
                    for test in graph.related_tests(path, limit=limit)
                })[:limit]
            if result.get("process_id"):
                with self._lock:
                    self._process_catalog[str(result["process_id"])] = dict(result)
            return result

        return self._cached_query(
            "process_context", (entry, max_depth, limit, time_budget_ms),
            compute,
        )

    def changed_scope(
        self, *, changed_paths: list[str] | None = None, limit: int = 100,
        max_depth: int = 4, node_limit: int = 100, time_budget_ms: float = 100.0,
        confidence_threshold: float = 0.0, wait_budget_ms: float = 50.0,
    ) -> dict:
        state = self._query_ready(wait_budget_ms)
        if state.status != "ready" or self.graph is None:
            return self._fallback("changed_scope", "", state)
        paths = tuple(changed_paths or ())
        return self._cached_query(
            "changed_scope",
            (paths, limit, max_depth, node_limit, time_budget_ms, confidence_threshold),
            lambda: self.graph.changed_scope(
                changed_paths=list(paths) if changed_paths is not None else None,
                limit=limit, max_depth=max_depth, node_limit=node_limit,
                time_budget_ms=time_budget_ms,
                confidence_threshold=confidence_threshold,
            ),
        )

    def augment_with_lsp(
        self, *, paths: list[str] | None = None, max_calls: int = 20,
        time_budget_ms: float = 250.0,
    ) -> dict:
        """Explicitly spend a bounded LSP budget to upgrade unresolved calls."""
        state = self.state
        if state.status != "ready":
            return self._fallback("lsp_call_augmentation", "", state)
        from nz_coder.intelligence.lsp_resolver import LspCallTargetResolver

        stats = self.index.augment_call_targets(
            LspCallTargetResolver(self.workspace), paths=paths,
            max_calls=max_calls, time_budget_ms=time_budget_ms,
        )
        if stats.resolved:
            with self._lock:
                self._state = replace(
                    self._state, generation=stats.generation,
                    lsp_augmented_calls=self._state.lsp_augmented_calls + stats.resolved,
                )
                self._cache.clear()
        result = asdict(stats)
        result.update({
            "freshness": "indexed", "source": "lsp-definition-augmentation",
        })
        self._emit("repo_intelligence_lsp_augmentation", **result)
        return result

    def metrics(self) -> dict:
        state = self.state
        with self._lock:
            semantic_metrics = (
                self._semantic_index.metrics()
                if self._semantic_index is not None else {}
            )
            return {
                **state.__dict__, "cache_hit": self._cache_hits,
                "cache_miss": self._cache_misses,
                "fallback_count": self._fallback_count,
                "query_count": self._query_count,
                "query_ms": round(self._query_ms, 3),
                "query_average_ms": round(self._query_ms / max(1, self._query_count), 3),
                "semantic_available": self.semantic_available,
                "semantic_status": self.semantic_status,
                "semantic_provider": self._semantic_provider_identity,
                "semantic_queries": self._semantic_queries,
                "semantic_builds": self._semantic_builds,
                "semantic_index": semantic_metrics,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None:
            thread.join(timeout=5)
        self._executor.shutdown(wait=True, cancel_futures=False)


_REGISTRY_LOCK = RLock()
_REGISTRY: dict[Path, tuple[RepoIntelligenceService, int]] = {}


def _close_all_repo_intelligence_services() -> None:
    """Stop every process-owned worker before native modules are finalized."""
    with _REGISTRY_LOCK:
        services = tuple({id(item[0]): item[0] for item in _REGISTRY.values()}.values())
        _REGISTRY.clear()
    for service in services:
        try:
            service.close()
        except Exception:
            # Interpreter teardown is best-effort, but each close() signals the
            # native watcher before any operation that may itself fail.
            continue


def _reset_registry_after_fork() -> None:
    global _REGISTRY_LOCK, _REGISTRY
    _REGISTRY_LOCK = RLock()
    _REGISTRY = {}


def _start_watcher_after_prewarm(
    future: Future,
    service: RepoIntelligenceService,
    *,
    interval: float,
    debounce: float,
    max_files: int,
) -> None:
    """Start a deferred watcher only for a successfully built live index."""
    try:
        state = future.result()
    except Exception:
        return
    if state.status != "ready":
        return
    service.start_watching(
        interval=interval,
        debounce=debounce,
        max_files=max_files,
    )


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_registry_after_fork)

atexit.register(_close_all_repo_intelligence_services)


def workspace_repo_intelligence(
    workspace: Path, *, create: bool = True, interval: float = 1.0,
    max_files: int = 5000, start_watcher: bool = False,
) -> RepoIntelligenceService | None:
    """Return the process-wide workspace service without adding an owner lease."""
    key = Path(workspace).resolve()
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is not None:
            service = existing[0]
            if start_watcher:
                service._deferred_watch = (interval, max(0.05, interval * 2), max_files)
                if service.state.status == "ready":
                    service.start_watching(
                        interval=interval, debounce=max(0.05, interval * 2),
                        max_files=max_files,
                    )
            return service
        if not create:
            return None
        service = RepoIntelligenceService(key)
        if start_watcher:
            service._deferred_watch = (interval, max(0.05, interval * 2), max_files)
        _REGISTRY[key] = (service, 0)
    future = service.prewarm(max_files=max_files)
    if start_watcher:
        future.add_done_callback(lambda completed: _start_watcher_after_prewarm(
            completed,
            service,
            interval=interval,
            debounce=max(0.05, interval * 2),
            max_files=max_files,
        ))
    return service


def acquire_repo_intelligence(
    workspace: Path, *, interval: float = 1.0, max_files: int = 5000,
) -> RepoIntelligenceService:
    """Lease the single service shared by all agents in a workspace."""
    key = Path(workspace).resolve()
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is not None:
            service, count = existing
            _REGISTRY[key] = (service, count + 1)
            service._deferred_watch = (
                interval,
                max(0.05, interval * 2),
                max_files,
            )
            start_watcher = service.state.status == "ready"
            created = False
        else:
            service = RepoIntelligenceService(key)
            service._deferred_watch = (
                interval,
                max(0.05, interval * 2),
                max_files,
            )
            _REGISTRY[key] = (service, 1)
            start_watcher = False
            created = True
    if created:
        future = service.prewarm(max_files=max_files)
        future.add_done_callback(lambda completed: _start_watcher_after_prewarm(
            completed,
            service,
            interval=interval,
            debounce=max(0.05, interval * 2),
            max_files=max_files,
        ))
    elif start_watcher:
        service.start_watching(
            interval=interval,
            debounce=max(0.05, interval * 2),
            max_files=max_files,
        )
    return service


def release_repo_intelligence(workspace: Path) -> None:
    key = Path(workspace).resolve()
    service = None
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is None:
            return
        candidate, count = existing
        if count > 1:
            _REGISTRY[key] = (candidate, count - 1)
            return
        service = candidate
        del _REGISTRY[key]
    try:
        service.close()
    finally:
        # LSP clients share the workspace lifetime and must not outlive the
        # final SDK/agent lease for that workspace.
        from nz_coder.lsp.manager import close_workspace_clients

        close_workspace_clients(key)


@contextmanager
def repo_intelligence_workspace_lease(
    workspace: Path, *, interval: float = 1.0, max_files: int = 5000,
):
    """Explicit workspace-runtime ownership for SDK, evaluation, and agents."""
    service = acquire_repo_intelligence(
        workspace, interval=interval, max_files=max_files,
    )
    try:
        yield service
    finally:
        release_repo_intelligence(workspace)


__all__ = [
    "RepoIntelligenceService", "RepoIntelligenceState",
    "acquire_repo_intelligence", "release_repo_intelligence",
    "repo_intelligence_workspace_lease", "workspace_repo_intelligence",
]
