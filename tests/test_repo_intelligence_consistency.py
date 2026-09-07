"""Deterministic index/graph publication and product refresh regressions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from nz_coder.intelligence.service import RepoIntelligenceService


@pytest.fixture
def service(tmp_path):
    (tmp_path / "base.py").write_text("def base(): return 1\n", encoding="utf-8")
    instance = RepoIntelligenceService(tmp_path, query_cache_size=16)
    assert instance.prewarm(max_files=100).result(5).status == "ready"
    try:
        yield instance
    finally:
        instance.close()


@contextmanager
def paused_call(monkeypatch, owner, name):
    """Pause after a real call; control thread always releases before joining."""
    entered, release = Event(), Event()
    original = getattr(owner, name)

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5), f"controller did not release {name}"
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        with monkeypatch.context() as patch:
            patch.setattr(owner, name, pause)
            try:
                yield pool, entered, release
            finally:
                release.set()


def test_new_module_cannot_cross_index_graph_boundary(service, tmp_path, monkeypatch):
    """Without whole-update reader coordination, graph rejects the new module."""
    (tmp_path / "new.py").write_text("def created(): return 2\n", encoding="utf-8")
    with paused_call(monkeypatch, service.index, "update_paths") as (pool, entered, release):
        update = pool.submit(service._apply_incremental, ("new.py",), 100)
        assert entered.wait(3), "update did not reach index/graph boundary"
        query = pool.submit(service.symbol_context, "created", wait_budget_ms=20)
        result = query.result(1)
        assert result["fallback"] is True
        assert result["freshness"] in {"updating", "unavailable"}
        assert service.state.status != "ready"
        release.set()
        update.result(3)
    result = service.symbol_context("created")
    assert result["definition"]["path"] == "new.py"
    assert result["generation"] == service.state.generation
    assert service.module_context("new.py")["generation"] == result["generation"]


def test_ordinary_write_hook_publishes_graph_and_invalidates_cache(tmp_path):
    from nz_coder.intelligence.code_index import update_code_index_after_write
    from nz_coder.intelligence.service import workspace_repo_intelligence, release_repo_intelligence

    instance = workspace_repo_intelligence(tmp_path)
    try:
        assert instance.wait_ready(5).status == "ready"
        assert instance.symbol_context("created")["definition"] is None
        (tmp_path / "new.py").write_text("def created(): pass\n", encoding="utf-8")
        stats = update_code_index_after_write(["new.py"], tmp_path)
        result = instance.symbol_context("created")
        assert result["definition"] is not None
        assert result["definition"]["path"] == "new.py"
        assert result["generation"] == stats.generation
        assert instance.module_context("new.py")["generation"] == stats.generation
    finally:
        release_repo_intelligence(tmp_path)


def test_old_query_cannot_write_cache_across_publication(service, tmp_path, monkeypatch):
    """The update must wait for the old reader, then invalidate its cache write."""
    old_generation = service.state.generation
    with paused_call(monkeypatch, service.index, "symbol_context") as (pool, entered, release):
        query = pool.submit(service.symbol_context, "base")
        assert entered.wait(3)
        (tmp_path / "base.py").write_text("def renamed(): pass\n", encoding="utf-8")
        started = Event()

        def update():
            started.set()
            service._apply_incremental(("base.py",), 100)

        writer = pool.submit(update)
        assert started.wait(3)
        # A separate query with zero wait must not block behind this reader.
        bounded = pool.submit(service.search_symbols, "renamed", wait_budget_ms=0).result(1)
        assert bounded.get("fallback") is True
        release.set()
        result = query.result(3)
        writer.result(3)
    assert result["generation"] == old_generation
    assert result["definition"]["name"] == "base"
    assert service.symbol_context("base")["definition"] is None
    current = service.symbol_context("renamed")
    assert current["definition"]["name"] == "renamed"
    assert current["generation"] > old_generation
    assert all(key[0] == current["generation"] for key in service._cache)


@pytest.mark.parametrize("stage", ["index", "graph", "snapshot"])
def test_failed_update_withholds_partial_view_and_next_update_repairs(service, tmp_path, monkeypatch, stage):
    """A successful retry of another path must repair the failed path as well."""
    from nz_coder.intelligence.repository_graph import RepositoryGraph

    (tmp_path / "new.py").write_text("def created(): pass\n", encoding="utf-8")
    owner, method = {
        "index": (service.index, "update_paths"),
        "graph": (RepositoryGraph, "update_paths"),
        "snapshot": (service.index, "metrics"),
    }[stage]

    def fail(*args, **kwargs):
        raise OSError("injected publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, fail)
        service._apply_incremental(("new.py",), 100)
    assert service.state.status == "failed"
    failed = service.symbol_context("created")
    assert failed["fallback"] and failed["freshness"] == "failed"
    # The caller does not know which failed paths were already committed.
    # Even a later unrelated update must repair the lost path.
    service._apply_incremental(("base.py",), 100)
    result = service.symbol_context("created")
    assert result["definition"]["path"] == "new.py"
    assert service.module_context("new.py")["generation"] == result["generation"]


def test_cache_is_bounded_hits_and_does_not_share_mutable_payload(service):
    first = service.symbol_context("base")
    first["definition"]["name"] = "caller mutation"
    hit = service.symbol_context("base")
    assert hit["cache_hit"] is True
    assert hit["definition"]["name"] == "base"
    for i in range(20):
        result = service.search_symbols(f"missing{i}")
        assert result["matches"] == []
    assert len(service._cache) == 16
    assert service.search_symbols("missing19")["cache_hit"] is True
    assert service.symbol_context("base")["cache_hit"] is False


@pytest.mark.parametrize("rebuild", [False, True])
def test_queries_during_cold_or_rebuild_are_bounded(tmp_path, monkeypatch, rebuild):
    instance = RepoIntelligenceService(tmp_path)
    try:
        if rebuild:
            assert instance.prewarm(max_files=100).result(5).status == "ready"
        (tmp_path / "new.py").write_text("def created(): pass\n", encoding="utf-8")
        with paused_call(monkeypatch, instance.index, "scan") as (pool, entered, release):
            build = instance.prewarm(max_files=100)
            assert entered.wait(3)
            result = pool.submit(instance.symbol_context, "created", wait_budget_ms=20).result(1)
            assert result["fallback"] and result["freshness"] in {"warming", "updating"}
            release.set()
            assert build.result(3).status == "ready"
        assert instance.symbol_context("created")["definition"]["path"] == "new.py"
    finally:
        instance.close()


def test_close_during_build_is_bounded_and_prevents_ready_publication(tmp_path, monkeypatch):
    instance = RepoIntelligenceService(tmp_path)
    try:
        with paused_call(monkeypatch, instance.index, "scan") as (pool, entered, release):
            future = instance.prewarm(max_files=100)
            assert entered.wait(3)
            pool.submit(instance.close).result(1)
            assert instance.symbol_context("missing")["freshness"] == "closed"
            release.set()
            assert future.result(3).status == "closed"
        assert instance.state.status == "closed"
    finally:
        instance.close()


def test_executor_query_does_not_wait_on_build_queued_behind_it(service):
    def query_and_rebuild():
        future = service.prewarm(max_files=100)
        result = service.symbol_context("base", wait_budget_ms=1000)
        return future, result

    future, result = service.submit_bounded_query(query_and_rebuild).result(0.5)
    assert result["fallback"] is True
    assert future.result(3).status == "ready"


def test_map_refresh_publishes_graph_not_only_sqlite(tmp_path):
    from nz_coder.intelligence.service import workspace_repo_intelligence, release_repo_intelligence
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools.repo_map import repo_map, code_references

    instance = workspace_repo_intelligence(tmp_path)
    try:
        assert instance.wait_ready(5).status == "ready"
        (tmp_path / "new.py").write_text("def created(): pass\ndef run(): created()\n", encoding="utf-8")
        with scoped_workdir(tmp_path):
            result = repo_map(refresh=True)
            references = code_references("created")
        assert "created" in result and not result.startswith("Error:")
        assert "new.py:2" in references
        result = instance.symbol_context("created")
        assert not result.get("fallback")
        assert instance.module_context("new.py")["generation"] == result["generation"]
    finally:
        release_repo_intelligence(tmp_path)


def test_recovery_refresh_failure_retains_progress_and_retry_does_not_rewrite(tmp_path, monkeypatch):
    from tests.recovery.test_recovery_operations import edit, history, reverter
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.intelligence.service import workspace_repo_intelligence, release_repo_intelligence
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal
    from nz_coder.protocol.recovery import RecoveryError
    from nz_coder.state.tool_ledger import ToolLedger
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.state.sessions import save_session, session_runtime_state_path, write_session_runtime_json
    import json

    (tmp_path / "a.py").write_text("def before(): pass\n", encoding="utf-8")
    ledger = ToolLedger(tmp_path)
    edit(ledger, "a.py", b"def after(): pass\n")
    messages = history()
    with scoped_workdir(tmp_path):
        save_session(messages, session_id="session-a")
        state_path = session_runtime_state_path("session-a")
        write_session_runtime_json(state_path, {"active": True, "verification": "old"})
    instance = workspace_repo_intelligence(tmp_path)
    try:
        assert instance.wait_ready(5).status == "ready"
        writes, commits = [], []
        original_write = WorkspaceFileAccess.write_bytes
        original_commit = RecoveryJournal._commit_history

        def write(self, *args, **kwargs):
            writes.append(args[0])
            return original_write(self, *args, **kwargs)

        def commit(self, operation):
            commits.append(operation["operation_id"])
            return original_commit(self, operation)

        monkeypatch.setattr(WorkspaceFileAccess, "write_bytes", write)
        monkeypatch.setattr(RecoveryJournal, "_commit_history", commit)
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError("index refresh interrupted")
            patch.setattr(instance.index, "update_paths", fail)
            with pytest.raises(RecoveryError) as error:
                reverter(tmp_path).revert(messages)
        operation_id = error.value.result.operation_id
        assert instance.state.status == "failed"
        assert (tmp_path / "a.py").read_text() == "def before(): pass\n"
        assert messages == history()
        journal = RecoveryJournal(ledger, "session-a")
        assert journal.pending()["operation_id"] == operation_id
        assert [row["status"] for row in journal.files(operation_id)] == ["applied"]
        assert json.loads(state_path.read_text())["verification_invalidated"] == operation_id
        assert commits == [] and writes == ["a.py"]
        recovered = reverter(tmp_path).recover(messages)
        assert recovered.status == "completed" and recovered.operation_id == operation_id
        assert commits == [operation_id] and writes == ["a.py"]
        assert messages == []
        assert reverter(tmp_path).recover(messages) is None
        assert instance.symbol_context("before")["definition"]["path"] == "a.py"
        assert instance.symbol_context("after")["definition"] is None
        assert json.loads(state_path.read_text())["active"] is False
    finally:
        release_repo_intelligence(tmp_path)


def test_delete_rename_removes_old_symbols_and_call_targets(service, tmp_path):
    (tmp_path / "test_base.py").write_text("from base import base\ndef test_run(): base()\n", encoding="utf-8")
    service.refresh_paths(["test_base.py"])
    before = service.symbol_context("base")
    assert before["related_tests"] == ["test_base.py"]
    assert [item["caller"] for item in before["callers"]] == ["test_run"]
    (tmp_path / "base.py").rename(tmp_path / "renamed.py")
    (tmp_path / "renamed.py").write_text("def replacement(): pass\n", encoding="utf-8")
    (tmp_path / "test_base.py").write_text("from renamed import replacement\ndef test_run(): replacement()\n", encoding="utf-8")
    service.refresh_paths(["base.py", "renamed.py", "test_base.py"])
    assert service.symbol_context("base")["definition"] is None
    after = service.symbol_context("replacement")
    assert after["definition"]["path"] == "renamed.py"
    assert after["related_tests"] == ["test_base.py"]
    assert [item["caller"] for item in after["callers"]] == ["test_run"]
    assert service.module_context("test_base.py")["dependencies"] == ["renamed.py"]
    (tmp_path / "renamed.py").unlink()
    service.refresh_paths(["renamed.py"])
    assert service.symbol_context("replacement")["definition"] is None
    assert not any(edge.get("callee_symbol_id") for edge in service.symbol_context("test_run")["callees"])


def test_overlapping_watcher_and_explicit_updates_keep_later_paths(service, tmp_path, monkeypatch):
    (tmp_path / "one.py").write_text("def first(): pass\n", encoding="utf-8")
    with paused_call(monkeypatch, service.index, "update_paths") as (pool, entered, release):
        first = pool.submit(service._apply_incremental, ("one.py",), 100)
        assert entered.wait(3)
        (tmp_path / "one.py").write_text("def latest(): pass\n", encoding="utf-8")
        (tmp_path / "two.py").write_text("def second(): pass\n", encoding="utf-8")
        submitted = Event()

        def later():
            submitted.set()
            return service.refresh_paths(["one.py", "two.py", "one.py"])

        second = pool.submit(later)
        assert submitted.wait(3)
        assert service.symbol_context("first", wait_budget_ms=0)["fallback"]
        release.set()
        first.result(3)
        stats = second.result(3)
    assert service.symbol_context("first")["definition"] is None
    assert service.symbol_context("latest")["generation"] == stats.generation
    assert service.symbol_context("second")["definition"]["path"] == "two.py"
    repeated = service.refresh_paths(["two.py", "two.py"])
    assert repeated.generation == stats.generation


def test_restart_rebuilds_graph_after_persisted_index_publication_failure(tmp_path, monkeypatch):
    instance = RepoIntelligenceService(tmp_path)
    assert instance.prewarm(max_files=100).result(5).status == "ready"
    (tmp_path / "new.py").write_text("def created(): pass\n", encoding="utf-8")
    try:
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError("before view publication")
            patch.setattr(instance, "_publish", fail)
            instance._apply_incremental(("new.py",), 100)
        assert instance.state.status == "failed"
    finally:
        instance.close()
    restarted = RepoIntelligenceService(tmp_path)
    try:
        assert restarted.symbol_context("created")["freshness"] == "cold"
        assert restarted.prewarm(max_files=100).result(5).status == "ready"
        context = restarted.symbol_context("created")
        assert context["definition"]["path"] == "new.py"
        assert restarted.module_context("new.py")["generation"] == context["generation"]
    finally:
        restarted.close()


def test_real_executor_and_product_observer_refresh_without_watcher(tmp_path):
    from tests.recovery.test_execution_tracking import Permissions, batch
    from tests.test_code_index import _write_effects
    from nz_coder.intelligence.code_index import update_code_index_after_write
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run, register_batch
    from nz_coder.intelligence.service import workspace_repo_intelligence, release_repo_intelligence
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.state.tool_ledger import ToolLedger
    from nz_coder.tools import files  # noqa: F401

    instance = workspace_repo_intelligence(tmp_path)
    try:
        assert instance.wait_ready(5).status == "ready"
        call, messages = batch()
        call["function"]["arguments"] = {"path": "new.py", "content": "def created(): pass\n"}
        events = []
        effects = _write_effects(
            tmp_path,
            update_code_index_after_write,
            lambda event, **payload: events.append((event, payload)),
        )
        with scoped_workdir(tmp_path), recovery_run(tmp_path, "session-a") as run:
            run.attach("interaction-a", messages)
            register_batch(messages, [call])
            result = ToolExecutor(Permissions()).execute_one(call, 0)
            assert result.executed and not result.dispatch_failed
            # This is the real post-write observer entry, not a copied hook.
            effects.refresh_code_index([(0, call, result)])
        assert len(ToolLedger(tmp_path).mutations("session-a")) == 1
        assert events[-1][0] == "code_index_refreshed"
        assert instance.symbol_context("created")["definition"]["path"] == "new.py"
        assert instance.module_context("new.py")["generation"] == instance.state.generation
    finally:
        release_repo_intelligence(tmp_path)


@pytest.mark.parametrize("direction", ["undo", "redo"])
def test_real_recovery_concurrent_queries_use_the_publication_boundary(tmp_path, monkeypatch, direction):
    from tests.recovery.test_recovery_operations import edit, history, reverter
    from nz_coder.intelligence.service import workspace_repo_intelligence, release_repo_intelligence
    from nz_coder.state.tool_ledger import ToolLedger
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.state.sessions import session_runtime_state_path, write_session_runtime_json
    import json

    # Undo restores a deleted module: this exercises the original new-index /
    # old-graph KeyError window through real recovery, not a direct service call.
    (tmp_path / "base.py").write_text("def base(): pass\n", encoding="utf-8")
    (tmp_path / "test_base.py").write_text("from base import base\ndef test_run(): base()\n", encoding="utf-8")
    edit(ToolLedger(tmp_path), "base.py", None)
    messages = history()
    instance = workspace_repo_intelligence(tmp_path)
    try:
        assert instance.wait_ready(5).status == "ready"
        if direction == "redo":
            reverter(tmp_path).revert(messages)
        with scoped_workdir(tmp_path):
            state_path = session_runtime_state_path("session-a")
            write_session_runtime_json(state_path, {"active": True, "verification": "old"})
        with paused_call(monkeypatch, instance.index, "update_paths") as (pool, entered, release):
            coordinator = reverter(tmp_path)
            operation = pool.submit(coordinator.revert if direction == "undo" else coordinator.unrevert, messages)
            assert entered.wait(3)
            result = pool.submit(instance.symbol_context, "base", wait_budget_ms=20).result(1)
            assert result["fallback"] and result["freshness"] == "updating"
            release.set()
            completed = operation.result(3)
        assert completed.status == "completed"
        state = json.loads(state_path.read_text())
        assert state["verification_invalidated"] == completed.operation_id and state["active"] is False
        result = instance.symbol_context("base")
        if direction == "undo":
            assert result["definition"]["path"] == "base.py"
            assert result["related_tests"] == ["test_base.py"]
            assert [edge["caller"] for edge in result["callers"]] == ["test_run"]
            assert messages == []
        else:
            assert result["definition"] is None
            assert not (tmp_path / "base.py").exists()
            assert messages == history()
        assert result["generation"] == instance.state.generation
    finally:
        release_repo_intelligence(tmp_path)


def test_cancelled_queued_build_is_not_left_warming(service):
    entered, release = Event(), Event()

    def occupy_worker():
        entered.set()
        assert release.wait(5)

    occupied = service.submit_bounded_query(occupy_worker)
    try:
        assert entered.wait(3)
        queued = service.prewarm(max_files=100)
        assert queued.cancel()
        result = service.symbol_context("base", wait_budget_ms=10)
        assert result["fallback"] and result["freshness"] == "failed"
        assert "cancel" in service.state.error.lower()
    finally:
        release.set()
        occupied.result(3)
    assert service.prewarm(max_files=100).result(3).status == "ready"


def test_reentrant_tracer_query_and_close_do_not_self_wait(service):
    results = []

    class Tracer:
        def log(self, event, **payload):
            if event == "repo_intelligence_cold_build":
                results.append(service.symbol_context("base"))
                service.close()

    tracer = Tracer()
    service.attach_tracer(tracer)
    service.prewarm(max_files=100).result(3)
    assert len(results) == 1 and results[0]["definition"]["name"] == "base"
    assert service.state.status == "closed"


def test_semantic_timeout_does_not_hold_structural_publication_lock(service, tmp_path):
    from tests.test_retrieval_intelligence import _FixtureEmbeddingProvider

    entered, release, finished = Event(), Event(), Event()

    class Provider(_FixtureEmbeddingProvider):
        def embed(self, texts):
            entered.set()
            assert release.wait(5), "semantic controller failed to release provider"
            result = super().embed(texts)
            finished.set()
            return result

    service.configure_semantic(Provider())
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            result = service.semantic_search("base", wait_budget_ms=20)
            assert entered.wait(3) and result["fallback"]
            (tmp_path / "base.py").write_text("def newer(): pass\n", encoding="utf-8")
            pool.submit(service.refresh_paths, ["base.py"]).result(1)
            current = service.symbol_context("newer")
            assert current["definition"]["name"] == "newer"
        finally:
            release.set()
        assert finished.wait(3)


def test_poll_failure_is_replayed_on_next_unrelated_event(service, tmp_path, monkeypatch):
    original = service.index.update_paths
    attempts = []

    def update(paths):
        attempts.append(tuple(paths))
        if len(attempts) == 1:
            raise OSError("transient disk failure")
        return original(paths)

    class Rounds:
        round = 0

        def wait(self, timeout):
            self.round += 1
            if self.round == 1:
                (tmp_path / "base.py").write_text("def after(): pass\n", encoding="utf-8")
            elif self.round == 2:
                assert service.state.status == "failed"
                (tmp_path / "other.py").write_text("def other(): pass\n", encoding="utf-8")
            return self.round > 2

        def set(self):
            pass

    known = service._indexed_fingerprints()
    monkeypatch.setattr(service.index, "update_paths", update)
    monkeypatch.setattr(service, "_watch_stop", Rounds())
    service._poll_watch_loop(0.01, 0, 100, known)
    assert service.state.status == "ready"
    assert service.symbol_context("after")["definition"]["path"] == "base.py"
    assert service.symbol_context("base")["definition"] is None
    assert service.symbol_context("other")["definition"]["path"] == "other.py"


def test_native_subscribes_before_startup_refresh(service, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    (tmp_path / "early.py").write_text("def version_one(): pass\n", encoding="utf-8")
    subscribed = Event()

    def watch(*args, **kwargs):
        subscribed.set()
        yield set()  # subscription installed; first reconciliation can begin
        yield {(2, str(tmp_path / "early.py"))}  # event buffered during refresh

    monkeypatch.setitem(sys.modules, "watchfiles", SimpleNamespace(watch=watch))
    known = service._indexed_fingerprints()
    with paused_call(monkeypatch, service.index, "update_paths") as (pool, entered, release):
        watcher = pool.submit(service._native_watch_loop, 0.01, 0.01, 100, known)
        assert entered.wait(3)
        assert subscribed.is_set(), "startup refresh began before OS subscription"
        (tmp_path / "early.py").write_text("def version_two(): pass\n", encoding="utf-8")
        release.set()
        watcher.result(3)
    assert service.symbol_context("version_two")["definition"]["path"] == "early.py"
    assert service.symbol_context("version_one")["definition"] is None


def test_lsp_augmentation_publishes_calls_graph_generation_together(service, tmp_path, monkeypatch):
    from nz_coder.intelligence.code_index import ResolvedCallLocation
    from nz_coder.intelligence import lsp_resolver

    (tmp_path / "dynamic.py").write_text("def run(handler): return handler.base()\n", encoding="utf-8")
    service.refresh_paths(["dynamic.py"])
    before = service.symbol_context("run")
    assert before["callees"][0]["callee_symbol_id"] is None

    class Resolver:
        def resolve(self, request):
            assert request.file_path == "dynamic.py"
            return ResolvedCallLocation("base.py", 1, name="base")

    monkeypatch.setattr(lsp_resolver, "LspCallTargetResolver", lambda workspace: Resolver())
    with paused_call(monkeypatch, service.index, "augment_call_targets") as (pool, entered, release):
        update = pool.submit(service.augment_with_lsp, max_calls=1)
        assert entered.wait(3)
        assert service.symbol_context("run", wait_budget_ms=10)["fallback"]
        release.set()
        stats = update.result(3)
    result = service.symbol_context("run")
    assert result["generation"] == stats["generation"] > before["generation"]
    assert "base.py" in result["callees"][0]["callee_symbol_id"]
    assert service.module_context("dynamic.py")["generation"] == result["generation"]
    assert service.metrics()["lsp_augmented_calls"] == 1


def test_late_semantic_result_cannot_poison_new_generation(service, tmp_path, monkeypatch):
    from tests.test_retrieval_intelligence import _FixtureEmbeddingProvider

    service.configure_semantic(_FixtureEmbeddingProvider())
    with paused_call(monkeypatch, service._semantic_index, "search") as (pool, entered, release):
        query = pool.submit(service.semantic_search, "base", wait_budget_ms=0)
        assert entered.wait(3)
        (tmp_path / "base.py").write_text("def latest(): pass\n", encoding="utf-8")
        service.refresh_paths(["base.py"])
        release.set()
        result = query.result(3)
    assert result["fallback"] and result["freshness"] == "superseded"
    fresh = service.semantic_search("latest", wait_budget_ms=0)
    assert fresh["generation"] == service.state.generation
    assert "latest" in fresh["items"][0]["symbol_id"]
    assert service.semantic_search("latest", wait_budget_ms=0)["cache_hit"]


def test_exception_and_partial_queries_do_not_enter_cache(service, monkeypatch):
    with pytest.raises(KeyError, match="Unknown repository module"):
        service.module_context("missing.py")
    assert not service._cache
    original = service.index.process_context

    def partial(*args, **kwargs):
        return {**original(*args, **kwargs), "truncated": True}

    monkeypatch.setattr(service.index, "process_context", partial)
    assert service.process_context("base")["truncated"]
    assert not service._cache
    assert not service._process_catalog


def test_query_callback_cannot_reenter_update_or_block_on_its_future(service):
    def nested(index):
        with pytest.raises(RuntimeError, match="inside a query"):
            service.refresh_paths(["base.py"])
        return index.symbol_context("base")

    assert service.read_index(nested)["definition"]["name"] == "base"


def test_wait_ready_never_returns_historical_success_after_failed_refresh(service, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("latest refresh failed")

    monkeypatch.setattr(service.index, "update_paths", fail)
    service._apply_incremental(("base.py",), 100)
    assert service.wait_ready(0.01).status == "failed"


def test_wait_ready_inside_read_callback_cannot_wait_on_queued_writer(service):
    def nested(index):
        future = service.prewarm(max_files=100)
        state = service.wait_ready(0.01)
        return future, state

    future, state = service.read_index(nested)
    assert state.status == "warming"
    assert future.result(3).status == "ready"


def test_metrics_provider_status_wait_does_not_block_structural_refresh(service, tmp_path):
    from threading import RLock
    from tests.test_retrieval_intelligence import _FixtureEmbeddingProvider

    entered, release, status_waiting = Event(), Event(), Event()
    provider_lock = RLock()

    class Provider(_FixtureEmbeddingProvider):
        @property
        def status(self):
            status_waiting.set()
            with provider_lock:
                return "ready"

        def embed(self, texts):
            with provider_lock:
                entered.set()
                assert release.wait(5)
                return super().embed(texts)

    service.configure_semantic(Provider())
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            assert service.semantic_search("base", wait_budget_ms=20)["fallback"]
            assert entered.wait(3)
            metrics = pool.submit(service.metrics)
            assert status_waiting.wait(3)
            (tmp_path / "base.py").write_text("def after(): pass\n", encoding="utf-8")
            pool.submit(service.refresh_paths, ["base.py"]).result(1)
            assert service.symbol_context("after")["definition"]["path"] == "base.py"
        finally:
            release.set()
        assert metrics.result(3)["semantic_provider"] == "fixture/intent-v1"
