"""Workspace-owned repository intelligence prewarm contracts."""
from __future__ import annotations

import time
import multiprocessing


def _fork_acquire_worker(workspace: str, connection) -> None:
    from pathlib import Path
    from nz_coder.intelligence.service import (
        acquire_repo_intelligence,
        release_repo_intelligence,
    )

    service = acquire_repo_intelligence(Path(workspace), interval=0.02, max_files=100)
    service.wait_ready(timeout=5)
    connection.send(bool(service._watch_thread and service._watch_thread.is_alive()))
    release_repo_intelligence(Path(workspace))
    connection.close()


def test_service_prewarms_in_background_and_reports_fresh_state(tmp_path) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = RepoIntelligenceService(tmp_path)

    future = service.prewarm(max_files=100)
    state = service.wait_ready(timeout=5)

    assert future.done()
    assert state.status == "ready"
    assert state.files_indexed == 1
    assert service.symbol_context("run")["definition"]["path"] == "app.py"
    service.close()


def test_service_fallback_is_explicit_before_index_is_ready(tmp_path) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService

    service = RepoIntelligenceService(tmp_path)

    result = service.symbol_context("missing")

    assert result["freshness"] == "cold"
    assert result["warnings"]
    service.close()


def test_service_watcher_incrementally_indexes_create_change_delete(tmp_path) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService

    source = tmp_path / "live.py"
    service = RepoIntelligenceService(tmp_path)
    service.prewarm(max_files=100).result(timeout=5)
    service.start_watching(interval=0.02, debounce=0.03, max_files=100)

    source.write_text("def first(): return 1\n", encoding="utf-8")
    assert _wait_until(lambda: service.symbol_context("first")["definition"] is not None)
    first_generation = service.state.generation

    source.write_text("def second(): return 2\n", encoding="utf-8")
    assert _wait_until(lambda: service.symbol_context("second")["definition"] is not None)
    assert service.symbol_context("first")["definition"] is None
    assert service.state.generation > first_generation

    source.unlink()
    assert _wait_until(lambda: service.symbol_context("second")["definition"] is None)
    assert service.state.status == "ready"
    service.close()


def test_service_coalesces_burst_events_before_incremental_update(tmp_path) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService

    service = RepoIntelligenceService(tmp_path)
    service.prewarm(max_files=100).result(timeout=5)
    service.start_watching(interval=0.01, debounce=0.08, max_files=100)
    source = tmp_path / "burst.py"
    for value in range(5):
        source.write_text(f"def value(): return {value}\n", encoding="utf-8")
        time.sleep(0.005)

    assert _wait_until(lambda: service.symbol_context("value")["definition"] is not None)
    assert _wait_until(lambda: service.state.incremental_batches == 1)
    service.close()


def test_product_environment_owns_repo_worker_lifecycle(tmp_path) -> None:
    from nz_coder.runtime.loop import ProductRunEnvironment

    environment = object.__new__(ProductRunEnvironment)
    environment._initialize_repo_intelligence(tmp_path, interval=0.05)
    state = environment.repo_intelligence.wait_ready(timeout=5)

    assert state.status == "ready"
    assert environment.repo_intelligence._watch_thread is not None
    environment._close_repo_intelligence()
    assert not environment.repo_intelligence._watch_thread.is_alive()


def test_workspace_registry_shares_worker_and_closes_after_last_release(tmp_path) -> None:
    from nz_coder.intelligence.service import (
        acquire_repo_intelligence,
        release_repo_intelligence,
    )

    first = acquire_repo_intelligence(tmp_path, interval=0.05)
    second = acquire_repo_intelligence(tmp_path, interval=0.05)

    assert first is second
    release_repo_intelligence(tmp_path)
    assert not first._watch_stop.is_set()
    release_repo_intelligence(tmp_path)
    assert first._watch_stop.is_set()


def test_last_workspace_release_closes_only_its_lsp_clients(tmp_path, monkeypatch) -> None:
    from nz_coder.intelligence.service import (
        acquire_repo_intelligence,
        release_repo_intelligence,
    )

    closed = []
    monkeypatch.setattr(
        "nz_coder.lsp.manager.close_workspace_clients",
        lambda workspace: closed.append(workspace.resolve()),
    )
    first = acquire_repo_intelligence(tmp_path, interval=0.05)
    second = acquire_repo_intelligence(tmp_path, interval=0.05)

    release_repo_intelligence(tmp_path)
    assert closed == []
    release_repo_intelligence(tmp_path)

    assert first is second
    assert closed == [tmp_path.resolve()]


def test_explicit_workspace_lease_shares_generation_and_releases(tmp_path) -> None:
    from nz_coder.intelligence.service import repo_intelligence_workspace_lease

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    with repo_intelligence_workspace_lease(tmp_path, interval=0.05) as main:
        main.wait_ready(timeout=5)
        with repo_intelligence_workspace_lease(tmp_path, interval=0.05) as child:
            assert child is main
            assert child.state.generation == main.state.generation
            assert child.index.database_path == main.index.database_path
        assert not main._watch_stop.is_set()
    assert main._watch_stop.is_set()


def test_main_and_two_children_share_service_but_worktree_is_isolated(tmp_path) -> None:
    from nz_coder.intelligence.service import repo_intelligence_workspace_lease

    worktree = tmp_path.parent / f"{tmp_path.name}-worktree"
    worktree.mkdir()
    (tmp_path / "main.py").write_text("def main(): return 1\n", encoding="utf-8")
    (worktree / "main.py").write_text("def main(): return 2\n", encoding="utf-8")

    with repo_intelligence_workspace_lease(tmp_path, interval=0.05) as main:
        main.wait_ready(timeout=5)
        with repo_intelligence_workspace_lease(tmp_path, interval=0.05) as child_a:
            with repo_intelligence_workspace_lease(tmp_path, interval=0.05) as child_b:
                with repo_intelligence_workspace_lease(worktree, interval=0.05) as isolated:
                    isolated.wait_ready(timeout=5)
                    assert main is child_a is child_b
                    assert child_a.state.generation == child_b.state.generation
                    assert isolated is not main
                    assert isolated.index.database_path != main.index.database_path
            assert not main._watch_stop.is_set()
    assert main._watch_stop.is_set()
    assert isolated._watch_stop.is_set()


def test_forked_child_rebuilds_inherited_workspace_registry(tmp_path) -> None:
    from nz_coder.intelligence.service import (
        acquire_repo_intelligence,
        release_repo_intelligence,
    )

    parent_service = acquire_repo_intelligence(tmp_path, interval=0.02, max_files=100)
    parent_service.wait_ready(timeout=5)
    receive, send = multiprocessing.get_context("fork").Pipe(duplex=False)
    process = multiprocessing.get_context("fork").Process(
        target=_fork_acquire_worker, args=(str(tmp_path), send),
    )
    process.start()
    send.close()

    assert receive.poll(8)
    assert receive.recv() is True
    process.join(timeout=8)
    assert process.exitcode == 0
    release_repo_intelligence(tmp_path)


def test_query_wait_budget_falls_back_while_prewarm_is_blocked(tmp_path, monkeypatch) -> None:
    from threading import Event
    from nz_coder.intelligence.service import RepoIntelligenceService

    entered = Event()
    release = Event()
    service = RepoIntelligenceService(tmp_path)
    original = service._build

    def blocked(max_files):
        entered.set()
        release.wait(timeout=5)
        return original(max_files)

    monkeypatch.setattr(service, "_build", blocked)
    service.prewarm(max_files=20)
    assert entered.wait(timeout=2)

    result = service.symbol_context("anything", wait_budget_ms=1)

    assert result["freshness"] == "warming"
    assert result["warnings"]
    release.set()
    service.wait_ready(timeout=5)
    service.close()


def test_query_cache_hits_and_generation_update_invalidates(tmp_path) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService

    source = tmp_path / "app.py"
    source.write_text("def value(): return 1\n", encoding="utf-8")
    service = RepoIntelligenceService(tmp_path)
    service.prewarm(max_files=20).result(timeout=5)

    first = service.symbol_context("value")
    second = service.symbol_context("value")
    source.write_text("def value(): return 2\n", encoding="utf-8")
    service._apply_incremental(("app.py",), 20)
    third = service.symbol_context("value")

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert third["cache_hit"] is False
    assert third["definition"]["symbol_id"] == first["definition"]["symbol_id"]
    assert service.metrics()["cache_hit"] == 1
    assert service.state.generation > first.get("generation", 0)
    service.close()


def test_repo_runtime_metrics_emit_to_attached_agent_trace(tmp_path) -> None:
    from nz_coder.intelligence.service import RepoIntelligenceService

    (tmp_path / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    events = []

    class Tracer:
        def log(self, event, **payload):
            events.append({"event": event, **payload})

    tracer = Tracer()
    service = RepoIntelligenceService(tmp_path)
    service.attach_tracer(tracer)
    service.prewarm(max_files=20).result(timeout=5)
    service.symbol_context("run")
    service.symbol_context("run")
    service.close()

    assert any(item["event"] == "repo_intelligence_cold_build" for item in events)
    queries = [item for item in events if item["event"] == "repo_intelligence_query"]
    assert [item["cache_hit"] for item in queries] == [False, True]


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False
