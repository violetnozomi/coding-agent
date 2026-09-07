"""Workspace-owned production lifecycle for repository intelligence."""
from __future__ import annotations

import builtins
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Event, RLock, Thread, current_thread, local
import atexit
import os
import re
import time
import uuid
import weakref

from nz_coder.intelligence.code_index import (
    PersistentCodeIndex,
    is_excluded_directory,
    structural_match_score,
)
from nz_coder.intelligence.repository_graph import RepositoryGraph
from nz_coder.lsp.servers import language_for_path
from nz_coder.state.diagnostics import OperationDiagnostic


_DIAGNOSTIC_ID = re.compile(r"^diag-[0-9a-f]{32}$")
_FailurePersistence = Callable[[], tuple[str, bool]]
_FailureDiagnostic = Callable[[BaseException], _FailurePersistence]
_INDEX_STATUSES = frozenset({
    "cold", "warming", "updating", "failed", "unavailable", "closed",
})


class _RepoIntelligenceUnavailable(RuntimeError):
    """Safe structural state from one failed bounded index read."""

    def __init__(
        self, status: str, generation: int, *, diagnostic_id: str = "",
        diagnostic_evidence_saved: bool = False,
    ) -> None:
        self.status = status if status in _INDEX_STATUSES else "unavailable"
        self.generation = max(0, int(generation))
        self.diagnostic_id = (
            diagnostic_id if _DIAGNOSTIC_ID.fullmatch(diagnostic_id) else ""
        )
        self.diagnostic_evidence_saved = bool(
            self.diagnostic_id and diagnostic_evidence_saved
        )
        super().__init__("Repository intelligence is unavailable")


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
    diagnostic_id: str = ""
    diagnostic_evidence_saved: bool = False


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
        # Lock order: view/index -> short state lock. No Future wait, worker
        # join or tracer callback may run while holding either boundary.
        self._view_lock = self.index._lock
        self._worker = local()
        self._pending_paths: tuple[str, ...] = ()
        self._repair_scan = False
        self._max_files = 5000
        self._watch_stop = Event()
        self._watch_thread: Thread | None = None
        self._closed = False
        self._deferred_watch: tuple[float, float, int] | None = None
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._process_catalog: dict[str, dict] = {}
        self._cache_size = max(16, int(query_cache_size))
        self._cache_hits = 0
        self._cache_epoch = 0
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

    def prewarm(
        self, *, max_files: int = 5000,
        failure_diagnostic: _FailureDiagnostic | None = None,
    ) -> Future:
        """Schedule one non-blocking cold build for the workspace."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Repo intelligence service is closed")
            if self._future is not None and not self._future.done():
                return self._future
            previous = self._state
            self._state = replace(
                previous, status="warming", error="", worker_queue=1,
                diagnostic_id="", diagnostic_evidence_saved=False,
            )
            build_args = (
                (max(1, int(max_files)), failure_diagnostic)
                if failure_diagnostic is not None
                else (max(1, int(max_files)),)
            )
            self._future = self._executor.submit(
                self._on_worker, self._build, *build_args,
            )
            future = self._future
            future.add_done_callback(self._build_cancelled)
        self._emit("repo_intelligence_prewarm", max_files=max_files)
        return future

    def _build_cancelled(self, future: Future) -> None:
        if future.cancelled():
            with self._lock:
                if self._future is future and not self._closed and self._state.status == "warming":
                    self._state = replace(self._state, status="failed", error="Repository build cancelled", worker_queue=0)

    def _on_worker(self, callback, *args):
        self._worker.active = True
        try:
            return callback(*args)
        finally:
            self._worker.active = False

    def _clear_queries(self) -> None:
        """Caller holds the short state lock; invalidate in-flight writeback too."""
        self._cache_epoch += 1
        self._cache.clear()
        self._process_catalog.clear()

    def _publish(self, graph, stats, previous, started, *, paths=(), cold=False):
        """Publish only a complete graph/index pair under the view boundary."""
        metrics = self.index.metrics()
        if graph._generation != stats.generation or metrics["generation"] != stats.generation:
            raise RuntimeError("Repository index/graph generation mismatch")
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        state = RepoIntelligenceState(
            status="ready", files_indexed=metrics["files_indexed"],
            files_omitted=stats.omitted if cold else previous.files_omitted,
            generation=stats.generation,
            incremental_batches=previous.incremental_batches + (0 if cold else 1),
            last_updated_paths=tuple(paths),
            cold_build_ms=elapsed if cold else previous.cold_build_ms,
            incremental_update_ms=0.0 if cold else elapsed,
            symbols_indexed=metrics["symbols_indexed"], call_edges=metrics["call_edges"],
            watcher_backend=previous.watcher_backend,
            lsp_augmented_calls=previous.lsp_augmented_calls,
            languages=self.index.languages(),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("Repo intelligence service is closed")
            self.graph = graph
            self._clear_queries()
            self._state = state
        return state

    def _update(
        self, action, *, paths=(), cold=False, lsp=False,
        failure_diagnostic: _FailureDiagnostic | None = None,
    ):
        """Serialize all mutations through graph completion and cache publication.

        SQLite may already be committed if graph publication fails. We do not
        pretend that an old graph is an old view of that database: fail closed,
        then rebuild the complete graph on the next successful update.
        """
        started = time.perf_counter()
        failure: BaseException | None = None
        failure_traceback = None
        failed_state: RepoIntelligenceState | None = None
        persist_failure: _FailurePersistence | None = None
        diagnostic_id = ""
        if getattr(self._worker, "reading", False) or getattr(self._worker, "updating", False):
            raise RuntimeError("Cannot refresh repository view from inside a query/update callback")
        try:
            with self._view_lock:
                with self._lock:
                    if self._closed:
                        raise RuntimeError("Repo intelligence service is closed")
                    previous = self._state
                    paths = tuple(dict.fromkeys((*self._pending_paths, *paths)))
                    self._pending_paths = paths
                    self._state = replace(
                        previous, status="warming" if cold else "updating",
                        worker_queue=1, error="", diagnostic_id="",
                        diagnostic_evidence_saved=False,
                    )
                self._worker.updating = True
                try:
                    # Detect even standalone low-level writes before deciding
                    # whether an incremental graph delta is sufficient.
                    complete = cold or previous.status != "ready" or self.graph is None or self.index.generation() != previous.generation
                    if self._repair_scan:
                        self.index.scan(self.workspace, max_files=self._max_files)
                    value, stats = action(paths)
                    if lsp:
                        previous = replace(previous, lsp_augmented_calls=previous.lsp_augmented_calls + stats.resolved)
                    snapshot = self.index.snapshot(None if complete else list(paths))
                    graph = self.graph or RepositoryGraph(self.workspace, index=self.index)
                    if complete:
                        graph.build(snapshot=snapshot)
                    else:
                        # Derive deletions from the indexed batch, not a second
                        # live filesystem stat that can race the scan itself.
                        selected = {entry.path for entry in snapshot.files}
                        graph.update_paths(paths, [path for path in paths if path not in selected], snapshot=snapshot)
                    self._publish(graph, stats, previous, started, paths=paths, cold=cold)
                    with self._lock:
                        self._pending_paths = ()
                        self._repair_scan = False
                except Exception as exc:
                    failure = exc
                    failure_traceback = exc.__traceback__
                    if failure_diagnostic is not None:
                        try:
                            persist_failure = failure_diagnostic(exc)
                        except Exception:
                            pass
                    else:
                        # Reserve a correlation root without touching storage.
                        # The diagnostic object and traceback extraction are
                        # deliberately deferred until all producer locks exit.
                        diagnostic_id = f"diag-{uuid.uuid4().hex}"
                    kind = type(exc)
                    safe_type = (
                        kind.__name__
                        if getattr(builtins, kind.__name__, None) is kind
                        else "Exception"
                    )
                    with self._lock:
                        self._repair_scan = self._repair_scan or cold
                        self._clear_queries()
                        if not self._closed:
                            failed_state = replace(
                                previous, status="failed", error=safe_type,
                                last_updated_paths=tuple(paths), worker_queue=0,
                                diagnostic_id=diagnostic_id,
                                diagnostic_evidence_saved=False,
                            )
                            self._state = failed_state
                finally:
                    self._worker.updating = False
            if failure is not None:
                if persist_failure is None and diagnostic_id:
                    root_diagnostic_id = diagnostic_id

                    def persist_service_failure() -> tuple[str, bool]:
                        return self._persist_failure_diagnostic(
                            root_diagnostic_id, failure,
                        )

                    persist_failure = persist_service_failure
                evidence_saved = False
                if persist_failure is not None:
                    try:
                        candidate, evidence_saved = persist_failure()
                        if (
                            isinstance(candidate, str)
                            and _DIAGNOSTIC_ID.fullmatch(candidate)
                        ):
                            diagnostic_id = candidate
                        else:
                            diagnostic_id = ""
                    except Exception:
                        pass
                if diagnostic_id and failed_state is not None:
                    with self._lock:
                        if self._state is failed_state:
                            self._state = replace(
                                failed_state,
                                diagnostic_id=diagnostic_id,
                                diagnostic_evidence_saved=bool(evidence_saved),
                            )
                raise failure.with_traceback(failure_traceback)
            return value, stats
        finally:
            state = self.state
            self._emit(
                "repo_intelligence_cold_build" if cold else "repo_intelligence_incremental_update",
                status=state.status, generation=state.generation,
                paths=list(paths), error=state.error, worker_queue=state.worker_queue,
                cold_build_ms=state.cold_build_ms, incremental_update_ms=state.incremental_update_ms,
                files_indexed=state.files_indexed, symbols_indexed=state.symbols_indexed,
                call_edges=state.call_edges, files_omitted=state.files_omitted,
            )

    def _persist_failure_diagnostic(
        self, diagnostic_id: str, failure: BaseException,
    ) -> tuple[str, bool]:
        """Persist a service-owned first cause outside producer locks."""
        diagnostic = OperationDiagnostic(
            "repo_map", workspace=self.workspace, diagnostic_id=diagnostic_id,
            identities={"workspace_id": str(self.workspace)},
        )
        diagnostic.advance("index_read")
        public = diagnostic.persist(diagnostic.capture(failure))
        return diagnostic.id, bool(
            public.public_error.metadata.get("evidence_saved")
        )

    def _build(
        self, max_files: int,
        failure_diagnostic: _FailureDiagnostic | None = None,
    ) -> RepoIntelligenceState:
        try:
            self.scan(
                self.workspace, max_files=max_files,
                failure_diagnostic=failure_diagnostic,
            )
        except Exception:
            pass  # _update records failure; Future consumers receive typed state.
        return self.state

    def scan(
        self, base: Path, *, max_files: int, refresh: bool = False,
        failure_diagnostic: _FailureDiagnostic | None = None,
    ):
        """Explicit scans publish a complete graph, including partial map scopes."""
        def scan(pending):
            self._max_files = max_files
            if pending:
                self.index.update_paths(list(pending))
            return self.index.scan(base, max_files=max_files, refresh=refresh)

        return self._update(
            scan, cold=True, failure_diagnostic=failure_diagnostic,
        )

    def refresh_paths(self, paths, *, max_files: int = 5000):
        """Synchronous product write/recovery hook; failure is NOT success."""
        normalized = tuple(dict.fromkeys(self.index._relative(self.workspace / path) for path in paths))
        _, stats = self._update(lambda batch: (None, self.index.update_paths(list(batch))), paths=normalized)
        return stats

    def wait_ready(self, timeout: float | None = None) -> RepoIntelligenceState:
        """Wait for an existing prewarm; never starts work implicitly."""
        with self._lock:
            future = self._future
        if future is None or self._closed:
            return self.state
        if not future.done() and any(getattr(self._worker, key, False) for key in ("active", "reading", "updating")):
            return self.state  # Never wait while owning the worker or its view.
        try:
            future.result(timeout=timeout)
        except CancelledError:
            pass
        state = self.state  # A completed cold Future is not today's view state.
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
            return self._executor.submit(self._on_worker, callback)

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
            self._clear_queries()
        self._emit("repo_semantic_configured", provider=identity, experimental=True)

    def disable_semantic(self) -> None:
        with self._lock:
            self._semantic_index = None
            self._semantic_provider_identity = ""
            self._clear_queries()

    def _query_ready(self, wait_budget_ms: float) -> RepoIntelligenceState:
        state = self.state
        if state.status == "warming" and wait_budget_ms > 0 and not getattr(self._worker, "active", False) and not getattr(self._worker, "updating", False) and not getattr(self._worker, "reading", False):
            with self._lock:
                future = self._future
            try:
                if future is not None:
                    future.result(timeout=max(0.0, wait_budget_ms / 1000))
            except (FutureTimeout, CancelledError):
                pass
        return self.state

    def start_watching(
        self, *, interval: float = 1.0, debounce: float = 0.5, max_files: int = 5000,
    ) -> None:
        """Start native events when available, otherwise adaptive polling."""
        failure: Exception | None = None
        with self._view_lock, self._lock:
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

            # The first yield happens AFTER OS subscription. Reconciling before
            # watch() installs it leaves a second hole during that very refresh.
            reconcile = True
            for changes in watch(
                self.workspace, stop_event=self._watch_stop,
                debounce=max(1, int(debounce * 1000)),
                step=min(100, max(50, int(interval * 1000))), raise_interrupt=False,
                yield_on_timeout=True, rust_timeout=max(250, int(interval * 1000)),
            ):
                paths = {
                    relative for _change, path in changes
                    if (relative := self._eligible_event(str(path))) is not None
                }
                if reconcile:
                    current = self._fingerprints(max_files)
                    paths.update(path for path in set(known) | set(current) if known.get(path) != current.get(path))
                    known = current
                    reconcile = False
                with self._lock:
                    paths.update(self._pending_paths)
                if paths:
                    self._apply_incremental(tuple(sorted(paths)), max_files)
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
            with self._lock:
                pending.update(self._pending_paths)
            if pending and time.monotonic() - last_change >= debounce:
                paths = tuple(sorted(pending))
                pending.clear()
                self._apply_incremental(paths, max_files)

    def _apply_incremental(self, paths: tuple[str, ...], max_files: int) -> None:
        try:
            self.refresh_paths(paths, max_files=max_files)
        except Exception:
            pass  # Watchers keep running; _update retains failed state.

    @contextmanager
    def _read_view(self, wait_budget_ms: float):
        """Pin the mutable index/graph pair, or yield explicit unavailable state."""
        deadline = time.monotonic() + max(0.0, float(wait_budget_ms)) / 1000
        state = self._query_ready(wait_budget_ms)
        acquired = self._view_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        was_reading = getattr(self._worker, "reading", False)
        try:
            if acquired:
                self._worker.reading = True
            state = self.state
            if not acquired:
                state = replace(state, status="unavailable" if state.status == "ready" else state.status)
            elif state.status == "ready":
                if self.graph is None or self.graph._generation != state.generation or self.index.generation() != state.generation:
                    with self._lock:
                        state = replace(self._state, status="failed", error="Repository view requires refresh after index generation changed")
                        self._state = state
                        self._clear_queries()
            yield state
        finally:
            if acquired:
                self._worker.reading = was_reading
                self._view_lock.release()

    def read_index(self, callback, *, wait_budget_ms: float = 100.0):
        """Bounded index-only product reads obey the same published boundary."""
        with self._read_view(wait_budget_ms) as state:
            if state.status != "ready":
                raise _RepoIntelligenceUnavailable(
                    state.status, state.generation,
                    diagnostic_id=state.diagnostic_id,
                    diagnostic_evidence_saved=state.diagnostic_evidence_saved,
                )
            return callback(self.index)

    def _cached_query(self, operation: str, args: tuple, compute, *, wait_budget_ms: float = 50.0) -> dict:
        started = time.perf_counter()
        result = None
        with self._read_view(wait_budget_ms) as state:
            if state.status == "ready":
                generation = state.generation
                key = (generation, operation, *args)
                with self._lock:
                    epoch = self._cache_epoch
                    cached = self._cache.get(key)
                    if cached is not None:
                        self._cache.move_to_end(key)
                        self._cache_hits += 1
                        result = {**deepcopy(cached), "cache_hit": True}
                    else:
                        self._cache_misses += 1
                if result is None:
                    value = compute()
                    elapsed = (time.perf_counter() - started) * 1000
                    # generation is pinned through every subquery and writeback,
                    # not relabelled from a newer state after compute().
                    result = {**value, "generation": generation, "cache_hit": False, "query_ms": round(elapsed, 3)}
                    if operation in {"symbol_context", "module_context", "process_context", "changed_scope", "relationship_scan"}:
                        result["enrichment_provenance"] = {"worktree_status": "sampled-at-query-build; not versioned with index"}
                    elif operation == "semantic_search":
                        result["enrichment_provenance"] = {"file_content": "sampled-at-embedding-build; not versioned with index"}
                    with self._lock:
                        self._query_count += 1
                        self._query_ms += elapsed
                        if not self._closed and epoch == self._cache_epoch and not result.get("fallback") and not result.get("truncated"):
                            self._cache[key] = deepcopy(result)
                            self._cache.move_to_end(key)
                            while len(self._cache) > self._cache_size:
                                self._cache.popitem(last=False)
                if self._closed:
                    result = None
                    state = self.state
        if result is None:
            return self._fallback(operation, str(args[0]) if args else "", state)
        self._emit(
            "repo_intelligence_query", operation=operation,
            generation=generation, cache_hit=result["cache_hit"], query_ms=result.get("query_ms", 0.0),
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
            "generation": state.generation,
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
            compute, wait_budget_ms=wait_budget_ms,
        )

    def module_context(
        self, module: str, *, wait_budget_ms: float = 50.0,
    ) -> dict:
        return self._cached_query(
            "module_context", (module,), lambda: self.graph.module_context(module),
            wait_budget_ms=wait_budget_ms,
        )

    def overview(self, *, limit: int = 50, wait_budget_ms: float = 50.0) -> dict:
        return self._cached_query(
            "overview", (limit,), lambda: self.graph.overview(limit=limit),
            wait_budget_ms=wait_budget_ms,
        )

    def relationship_scan(
        self, module: str, *, limit: int = 30, wait_budget_ms: float = 50.0,
    ) -> dict:
        return self._cached_query(
            "relationship_scan", (module, limit),
            lambda: self.graph.relationship_scan(module, limit=limit),
            wait_budget_ms=wait_budget_ms,
        )

    def cyclic_dependencies(
        self, *, limit: int = 50, wait_budget_ms: float = 50.0,
    ) -> dict:
        return self._cached_query(
            "cyclic_dependencies", (limit,),
            lambda: {
                "cycles": self.graph.cycles()[:limit], "freshness": "indexed",
                "source": "repository-graph",
            },
            wait_budget_ms=wait_budget_ms,
        )

    def search_symbols(
        self, query: str, *, limit: int = 30, wait_budget_ms: float = 50.0,
    ) -> dict:
        return self._cached_query(
            "symbol_search", (query, limit),
            lambda: {
                "query": query, "matches": self.index.search_symbols(query, limit),
                "freshness": "indexed",
                "source": "structural-symbol-search",
            },
            wait_budget_ms=wait_budget_ms,
        )

    def intent_lookup(
        self, query: str, *, kind: str = "auto", limit: int = 20,
        wait_budget_ms: float = 50.0,
    ) -> dict:
        """Search Symbol, Module, and bounded Process candidates in one query."""
        selected_kind = str(kind or "auto").casefold()
        if selected_kind not in {"auto", "symbol", "module", "process"}:
            raise ValueError("lookup kind must be auto, symbol, module, or process")

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
                "source": "unified-structural-intent-lookup",
                "embedding": False,
            }

        return self._cached_query(
            "lookup", (query, selected_kind, int(limit)), compute, wait_budget_ms=wait_budget_ms,
        )

    def semantic_search(
        self, query: str, *, path: str | None = None, limit: int = 10,
        wait_budget_ms: float = 50.0,
    ) -> dict:
        """Run the optional embedding experiment and retain structural identities."""
        started = time.perf_counter()
        deadline = time.monotonic() + max(0.0, wait_budget_ms) / 1000
        with self._read_view(wait_budget_ms) as state:
            snapshot = self.index.snapshot() if state.status == "ready" else None
            with self._lock:
                semantic = self._semantic_index
                semantic_identity = self._semantic_provider_identity
                epoch = self._cache_epoch
                key = (state.generation, "semantic_search", query, path or "", int(limit), semantic_identity)
                cached = self._cache.get(key) if snapshot is not None else None
                if cached is not None:
                    self._cache.move_to_end(key)
                    self._cache_hits += 1
                    return {**deepcopy(cached), "cache_hit": True}
        if state.status != "ready":
            return self._fallback("semantic_search", query, state)
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

        abandoned = Event()

        def compute() -> dict:
            before_generation = semantic.generation
            items = [
                item.to_dict() for item in semantic.search(
                    query, path=path, limit=max(1, min(50, int(limit))),
                    snapshot=snapshot,
                )
            ]
            elapsed = (time.perf_counter() - started) * 1000
            result = {
                "query": query, "path": path, "items": items,
                "freshness": "indexed", "generation": snapshot.generation,
                "source": "optional-embedding-semantic-index",
                "embedding": True, "provider": semantic_identity,
                "cache_hit": False, "query_ms": round(elapsed, 3),
                "enrichment_provenance": {"file_content": "sampled-at-embedding-build; not versioned with index"},
            }
            with self._lock:
                self._semantic_queries += 1
                if before_generation != semantic.generation:
                    self._semantic_builds += 1
                current = self._state
                accepted = (not abandoned.is_set() and not self._closed
                            and current.status == "ready" and current.generation == snapshot.generation
                            and epoch == self._cache_epoch)
                if accepted:
                    self._cache_misses += 1
                    self._query_count += 1
                    self._query_ms += elapsed
                    self._cache[key] = deepcopy(result)
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
            if not accepted:
                return self._fallback("semantic_search", query, replace(current, status="superseded" if current.status == "ready" else current.status))
            return result

        # Model loading can involve a local cache miss or a remote model fetch.
        # Keep that optional path outside the Agent's critical request thread.
        if wait_budget_ms > 0:
            completed = Event()
            outcome: list[object] = []

            def run_query() -> None:
                try:
                    outcome.append(compute())
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
            completed.wait(timeout=max(0.0, deadline - time.monotonic()))
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
            abandoned.set()
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
        return compute()

    def process_context(
        self, entry: str, *, max_depth: int = 4, limit: int = 50,
        time_budget_ms: float = 100.0, wait_budget_ms: float = 50.0,
    ) -> dict:
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
            if result.get("process_id") and not result.get("truncated") and not result.get("fallback"):
                with self._lock:
                    self._process_catalog[str(result["process_id"])] = dict(result)
            return result

        return self._cached_query(
            "process_context", (entry, max_depth, limit, time_budget_ms),
            compute, wait_budget_ms=wait_budget_ms,
        )

    def changed_scope(
        self, *, changed_paths: list[str] | None = None, limit: int = 100,
        max_depth: int = 4, node_limit: int = 100, time_budget_ms: float = 100.0,
        confidence_threshold: float = 0.0, wait_budget_ms: float = 50.0,
    ) -> dict:
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
            wait_budget_ms=wait_budget_ms,
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

        def augment(pending):
            if pending:
                self.index.update_paths(list(pending))
            stats = self.index.augment_call_targets(
                LspCallTargetResolver(self.workspace), paths=paths,
                max_calls=max_calls, time_budget_ms=time_budget_ms,
            )
            return None, stats

        _, stats = self._update(augment, lsp=True)
        result = asdict(stats)
        result.update({
            "freshness": "indexed", "source": "lsp-definition-augmentation",
            "enrichment_provenance": {"lsp": "sampled-at-augmentation; external server state is not an index snapshot"},
        })
        self._emit("repo_intelligence_lsp_augmentation", **result)
        return result

    def metrics(self) -> dict:
        with self._lock:
            semantic = self._semantic_index
            result = {
                **self._state.__dict__, "cache_hit": self._cache_hits,
                "cache_miss": self._cache_misses,
                "fallback_count": self._fallback_count,
                "query_count": self._query_count,
                "query_ms": round(self._query_ms, 3),
                "query_average_ms": round(self._query_ms / max(1, self._query_count), 3),
                "semantic_provider": self._semantic_provider_identity,
                "semantic_queries": self._semantic_queries,
                "semantic_builds": self._semantic_builds,
            }
        # Provider properties may wait for a model-loading/embedding lock.
        # Observability must never carry that wait into the service state lock.
        result.update(
            semantic_available=bool(semantic is not None and semantic.available),
            semantic_status=(str(getattr(getattr(semantic, "provider", None), "status", "ready")) if semantic is not None else "unconfigured"),
            semantic_index=semantic.metrics() if semantic is not None else {},
        )
        return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._watch_stop.set()
            self._state = replace(self._state, status="closed", worker_queue=0)
            self._clear_queries()
        thread = self._watch_thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=5)
        # Running work settles later but cannot publish. In particular, closing
        # from a worker callback must not join its own executor thread.
        self._executor.shutdown(wait=False, cancel_futures=True)


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
    failure_diagnostic: _FailureDiagnostic | None = None,
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
    future = service.prewarm(
        max_files=max_files, failure_diagnostic=failure_diagnostic,
    )
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
