"""Secret-safe diagnostics on the real Repo Map failure boundaries."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from threading import Event

import pytest

from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.state.diagnostics import correlation


_DIAGNOSTIC_ID = re.compile(r"diag-[0-9a-f]{32}")


def _diagnostic_id(result: str) -> str:
    match = _DIAGNOSTIC_ID.search(result)
    assert match is not None, result
    return match.group(0)


@pytest.mark.parametrize("recorder_failed", [False, True])
def test_real_path_escape_keeps_rejection_prefix_without_echoing_input(
    tmp_path, monkeypatch, recorder_failed,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.trace import TraceRecorder
    from nz_coder.tools.repo_map import repo_map

    if recorder_failed:
        def fail_record(*args, **kwargs):
            raise OSError("SENTINEL-private-recorder")
        monkeypatch.setattr(TraceRecorder, "log", fail_record)
    with scoped_workdir(tmp_path):
        result = repo_map("../SENTINEL-private-path")
    assert result.startswith("Error: Path escapes workspace:")
    assert "SENTINEL" not in result
    assert _diagnostic_id(result)
    assert ("evidence=unavailable" if recorder_failed else "evidence=saved") in result


def _operations(workspace: Path, diagnostic_id: str) -> list[dict]:
    directory = prepare_user_storage(workspace).workspace_state / "diagnostics"
    path = directory / f"diagnostic__{diagnostic_id}.jsonl"
    return [json.loads(line)["operation"] for line in path.read_text().splitlines()]


def _indexed_entry(path: str = "sample.py"):
    from nz_coder.intelligence.code_index import FileEntry, SymbolEntry

    symbol = SymbolEntry(
        "function", "sample", "sample.sample", 1, 1, "sample()",
    )
    return FileEntry(path, "python", (1, 1), (symbol,))


def test_cold_map_keeps_100ms_gate_and_reports_warming_phase(
    tmp_path,
    monkeypatch,
):
    from nz_coder.intelligence.service import (
        RepoIntelligenceService,
        release_repo_intelligence,
        workspace_repo_intelligence,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.repo_map import repo_map

    (tmp_path / "sample.py").write_text("def sample():\n    pass\n", encoding="utf-8")
    entered = Event()
    release = Event()
    read_budgets: list[float] = []
    original = RepoIntelligenceService._build
    original_read = RepoIntelligenceService.read_index

    def held_build(self, max_files, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, max_files, *args, **kwargs)

    def observed_read(self, callback, *, wait_budget_ms=100.0):
        read_budgets.append(wait_budget_ms)
        return original_read(self, callback, wait_budget_ms=wait_budget_ms)

    monkeypatch.setattr(RepoIntelligenceService, "_build", held_build)
    monkeypatch.setattr(RepoIntelligenceService, "read_index", observed_read)
    map_executor = ThreadPoolExecutor(max_workers=1)
    try:
        with scoped_workdir(tmp_path):
            _ = workspace_repo_intelligence(tmp_path)
        assert entered.wait(2)

        def invoke():
            with scoped_workdir(tmp_path):
                return repo_map()

        result = map_executor.submit(invoke).result(timeout=1)
        service = workspace_repo_intelligence(tmp_path, create=False)
        assert not release.is_set()
        assert service is not None and service.state.status == "warming"
        assert read_budgets == [100.0]
        diagnostic_id = _diagnostic_id(result)
        assert "Repository index unavailable (warming)" in result
        failure = _operations(tmp_path, diagnostic_id)[-1]
        assert failure["primary_phase"] == "index_read"
        assert failure["facts"] == {
            "generation": 0,
            "cache_hits": 0,
            "index_warming": True,
            "index_failed": False,
            "index_ready": False,
        }
    finally:
        release.set()
        map_executor.shutdown(wait=True, cancel_futures=True)
        service = workspace_repo_intelligence(tmp_path, create=False)
        if service is not None:
            service.wait_ready(timeout=5)
        release_repo_intelligence(tmp_path)


def test_failed_cold_build_records_original_exception_and_sanitizes_state(
    tmp_path,
    monkeypatch,
):
    from nz_coder.intelligence.code_index import PersistentCodeIndex
    from nz_coder.intelligence.service import (
        release_repo_intelligence,
        workspace_repo_intelligence,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.repo_context import repo_context
    from nz_coder.tools.repo_map import repo_map

    secret = "SENTINEL-private-index-failure-/home/operator/token"
    (tmp_path / "sample.py").write_text("def sample():\n    pass\n", encoding="utf-8")

    def broken_scan(self, *args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(PersistentCodeIndex, "scan", broken_scan)
    try:
        with scoped_workdir(tmp_path):
            result = repo_map()
        service = workspace_repo_intelligence(tmp_path, create=False)
        assert service is not None
        state = service.wait_ready(timeout=2)
        diagnostic_id = _diagnostic_id(result)
        assert secret not in result
        assert "Repository index unavailable (failed)" in result
        assert state.error == "OSError"
        assert state.diagnostic_id == diagnostic_id
        assert state.diagnostic_evidence_saved is True
        assert secret not in json.dumps(service.metrics())
        with scoped_workdir(tmp_path):
            public_metrics = repo_context("runtime_metrics")
        assert secret not in public_metrics
        assert diagnostic_id in public_metrics
        status = next(
            row for row in _operations(tmp_path, diagnostic_id)
            if "index_failed" in row["facts"]
        )
        assert status["facts"] == {
            "generation": 0,
            "cache_hits": 0,
            "index_warming": False,
            "index_failed": True,
            "index_ready": False,
        }
        failures = [
            row for row in _operations(tmp_path, diagnostic_id)
            if row["exceptions"]
        ]
        primary = next(
            row for row in failures if row["facts"].get("primary_record")
        )
        assert primary["primary_type"] == "OSError"
        assert primary["primary_phase"] == "index_read"
        assert any(
            frame["module"] == "nz_coder.intelligence.service"
            and frame["function"] == "_update"
            for frame in primary["exceptions"][0]["frames"]
        )
    finally:
        release_repo_intelligence(tmp_path)


def test_acquired_service_prewarm_failure_keeps_root_diagnostic_for_repo_map(
    tmp_path,
    monkeypatch,
):
    from nz_coder.intelligence.code_index import PersistentCodeIndex
    from nz_coder.intelligence.service import (
        acquire_repo_intelligence,
        release_repo_intelligence,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.repo_map import repo_map

    secret = "SENTINEL-acquire-prewarm-/home/operator/token"
    entered = Event()
    release = Event()

    def broken_scan(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise OSError(secret)

    monkeypatch.setattr(PersistentCodeIndex, "scan", broken_scan)
    service = acquire_repo_intelligence(tmp_path, max_files=20)
    try:
        assert entered.wait(2)
        assert service.state.status == "warming"
        release.set()
        state = service.wait_ready(timeout=2)
        assert state.status == "failed"
        assert state.error == "OSError"
        assert _DIAGNOSTIC_ID.fullmatch(state.diagnostic_id)
        assert state.diagnostic_evidence_saved is True

        with scoped_workdir(tmp_path):
            result = repo_map()

        assert secret not in result
        assert "Repository index unavailable (failed)" in result
        assert _diagnostic_id(result) == state.diagnostic_id
        assert "evidence=saved" in result
        primary = next(
            row for row in _operations(tmp_path, state.diagnostic_id)
            if row["facts"].get("primary_record")
        )
        assert primary["primary_type"] == "OSError"
        assert any(
            frame["module"] == "nz_coder.intelligence.service"
            and frame["function"] == "_update"
            for frame in primary["exceptions"][0]["frames"]
        )
    finally:
        release.set()
        release_repo_intelligence(tmp_path)


def test_direct_scan_without_callback_records_first_failure(tmp_path, monkeypatch):
    from nz_coder.intelligence.code_index import PersistentCodeIndex
    from nz_coder.intelligence.service import RepoIntelligenceService

    secret = "SENTINEL-direct-scan-/home/operator/token"

    def broken_scan(self, *args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(PersistentCodeIndex, "scan", broken_scan)
    service = RepoIntelligenceService(tmp_path)
    try:
        with pytest.raises(OSError):
            service.scan(tmp_path, max_files=20)
        state = service.state
        assert state.error == "OSError"
        assert _DIAGNOSTIC_ID.fullmatch(state.diagnostic_id)
        assert state.diagnostic_evidence_saved is True
        primary = next(
            row for row in _operations(tmp_path, state.diagnostic_id)
            if row["facts"].get("primary_record")
        )
        assert primary["primary_type"] == "OSError"
        assert secret not in json.dumps(service.metrics())
    finally:
        service.close()


def test_service_owned_persistence_does_not_block_failed_read_or_root_id(
    tmp_path,
    monkeypatch,
):
    from nz_coder.intelligence.service import (
        release_repo_intelligence,
        workspace_repo_intelligence,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.diagnostics import OperationDiagnostic
    from nz_coder.tools.repo_map import repo_map

    primary = OSError("SENTINEL-service-owned-primary")
    persist_entered = Event()
    persist_release = Event()
    original_persist = OperationDiagnostic.persist

    def blocked_persist(diagnostic, captured):
        if set(diagnostic.identities) == {"workspace_id"}:
            persist_entered.set()
            assert persist_release.wait(5)
        return original_persist(diagnostic, captured)

    def fail(_paths):
        raise primary

    service = workspace_repo_intelligence(tmp_path, max_files=20)
    assert service is not None
    assert service.wait_ready(timeout=2).status == "ready"
    monkeypatch.setattr(OperationDiagnostic, "persist", blocked_persist)
    update_executor = ThreadPoolExecutor(max_workers=1)
    map_executor = ThreadPoolExecutor(max_workers=1)
    update = update_executor.submit(service._update, fail, cold=True)
    root_id = ""
    try:
        assert persist_entered.wait(2)
        state = service.state
        assert state.status == "failed"
        assert _DIAGNOSTIC_ID.fullmatch(state.diagnostic_id)
        assert state.diagnostic_evidence_saved is False
        root_id = state.diagnostic_id

        def invoke():
            with scoped_workdir(tmp_path):
                return repo_map()

        result = map_executor.submit(invoke).result(timeout=1)
        assert not persist_release.is_set()
        assert _diagnostic_id(result) == state.diagnostic_id
        assert "Repository index unavailable (failed)" in result
        assert "evidence=unavailable" in result
        assert "SENTINEL" not in result
        persist_release.set()
        with pytest.raises(OSError) as caught:
            update.result(timeout=2)
        assert caught.value is primary
        assert service.state.diagnostic_id == root_id
        assert service.state.diagnostic_evidence_saved is True
    finally:
        persist_release.set()
        try:
            if not update.done():
                try:
                    update.result(timeout=2)
                except BaseException:
                    pass
        finally:
            try:
                update_executor.shutdown(wait=True, cancel_futures=True)
            finally:
                try:
                    map_executor.shutdown(wait=True, cancel_futures=True)
                finally:
                    release_repo_intelligence(tmp_path)


def test_service_owned_diagnostic_failure_keeps_safe_root_and_primary(
    tmp_path,
    monkeypatch,
):
    from nz_coder.intelligence.service import (
        release_repo_intelligence,
        workspace_repo_intelligence,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.repo_map import repo_map

    primary = OSError("SENTINEL-primary-index-error")

    def fail(_paths):
        raise primary

    def broken_diagnostic(_diagnostic_id, _failure):
        raise RuntimeError("SENTINEL-secondary-recorder-error")

    service = workspace_repo_intelligence(tmp_path, max_files=20)
    assert service is not None
    assert service.wait_ready(timeout=2).status == "ready"
    monkeypatch.setattr(
        service, "_persist_failure_diagnostic", broken_diagnostic,
    )
    try:
        with pytest.raises(OSError) as caught:
            service._update(fail, cold=True)
        assert caught.value is primary
        state = service.state
        assert state.error == "OSError"
        assert _DIAGNOSTIC_ID.fullmatch(state.diagnostic_id)
        assert state.diagnostic_evidence_saved is False

        with scoped_workdir(tmp_path):
            result = repo_map()
        assert _diagnostic_id(result) == state.diagnostic_id
        assert "Repository index unavailable (failed)" in result
        assert "evidence=unavailable" in result
        assert "SENTINEL" not in result
    finally:
        release_repo_intelligence(tmp_path)


def test_blocked_failure_collector_runs_after_failure_publication_and_view_unlock(
    tmp_path,
):
    from nz_coder.intelligence.service import RepoIntelligenceService

    service = RepoIntelligenceService(tmp_path)
    service.scan(tmp_path, max_files=20)
    primary = OSError("SENTINEL-primary")
    collector_entered = Event()
    collector_release = Event()

    def fail(_paths):
        raise primary

    def blocked_collector(_error):
        def persist():
            collector_entered.set()
            assert collector_release.wait(5)
            return "diag-0123456789abcdef0123456789abcdef", True

        return persist

    executor = ThreadPoolExecutor(max_workers=1)
    recovery = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        service._update,
        fail,
        cold=False,
        failure_diagnostic=blocked_collector,
    )
    try:
        assert collector_entered.wait(2)
        assert service.metrics()["status"] == "failed"
        assert service._view_lock.acquire(blocking=False)
        service._view_lock.release()
        with pytest.raises(RuntimeError) as caught:
            service.read_index(lambda index: index.snapshot(), wait_budget_ms=0)
        assert caught.value.status == "failed"
        recovery.submit(
            service.scan, tmp_path, max_files=20,
        ).result(timeout=1)
        assert service.read_index(lambda index: index.snapshot()).files == ()
        assert service.metrics()["status"] == "ready"
    finally:
        collector_release.set()
        with pytest.raises(OSError) as caught:
            future.result(timeout=2)
        assert caught.value is primary
        assert service.state.status == "ready"
        assert service.state.error == ""
        executor.shutdown(wait=True)
        recovery.shutdown(wait=True, cancel_futures=True)
        service.close()


def test_misbehaving_failure_collector_cannot_replace_primary_exception(tmp_path):
    from nz_coder.intelligence.service import RepoIntelligenceService

    service = RepoIntelligenceService(tmp_path)
    primary = OSError("SENTINEL-primary")
    observations: list[tuple[str, bool]] = []

    def fail(_paths):
        raise primary

    def broken_collector(_error):
        def persist():
            acquired = service._view_lock.acquire(blocking=False)
            observations.append((service.state.status, acquired))
            if acquired:
                service._view_lock.release()
            raise RuntimeError("SENTINEL-secondary")

        return persist

    try:
        with pytest.raises(OSError) as caught:
            service._update(
                fail,
                cold=True,
                failure_diagnostic=broken_collector,
            )
        assert caught.value is primary
        assert observations == [("failed", True)]
        assert service.state.error == "OSError"
    finally:
        service.close()


@pytest.mark.parametrize(
    ("failure_phase", "patch_name"),
    [("path_validation", "_safe_path"), ("settings", "current_run_settings")],
)
def test_early_repo_map_failures_keep_their_phase(
    tmp_path,
    monkeypatch,
    failure_phase,
    patch_name,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import repo_map as module

    secret = f"SENTINEL-{failure_phase}-secret"

    def fail(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(module, patch_name, fail)
    with scoped_workdir(tmp_path):
        result = module.repo_map()

    diagnostic_id = _diagnostic_id(result)
    assert secret not in result
    failure = _operations(tmp_path, diagnostic_id)[-1]
    assert failure["primary_phase"] == failure_phase
    assert failure["primary_type"] == "OSError"


def test_parse_error_details_are_not_rendered_publicly(tmp_path, monkeypatch):
    from nz_coder.intelligence.code_index import FileEntry
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import repo_map as module

    secret = "SENTINEL-parser-message-/home/operator/source.py"
    bad = FileEntry("broken.ts", "typescript", (1, 1), (), f"RuntimeError: {secret}")
    monkeypatch.setattr(
        module,
        "_build_index",
        lambda *args, **kwargs: ([_indexed_entry(), bad], 0, 0),
    )

    with scoped_workdir(tmp_path):
        result = module.repo_map()

    assert "notice: skipped 1 unparsable/oversized file(s)" in result
    assert "broken.ts: parse unavailable" in result
    assert secret not in result


def test_lsp_failure_is_nonfatal_safe_and_keeps_semantic_phase(
    tmp_path,
    monkeypatch,
):
    from nz_coder.lsp import workspace_symbols
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import repo_map as module

    secret = "SENTINEL-lsp-provider-secret"
    (tmp_path / "sample.py").write_text("def sample():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_build_index",
        lambda *args, **kwargs: ([_indexed_entry()], 0, 0),
    )

    def fail_client(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(workspace_symbols, "get_client_for_file", fail_client)
    with scoped_workdir(tmp_path):
        result = module.repo_map(semantic=True)

    assert "function sample" in result
    assert "semantic_notice: LSP semantic enrichment unavailable" in result
    assert secret not in result
    diagnostic_id = _diagnostic_id(result)
    assert f"diagnostic={diagnostic_id}; evidence=saved" in result
    directory = prepare_user_storage(tmp_path).workspace_state / "diagnostics"
    rows = [
        json.loads(line)["operation"]
        for path in directory.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    failure = next(
        row for row in rows
        if row["id"] == diagnostic_id and row["exceptions"]
    )
    assert failure["primary_phase"] == "semantic_probe"
    assert failure["primary_type"] == "OSError"


def test_concurrent_workspaces_keep_distinct_call_and_session_correlation(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session
    from nz_coder.tools import scoped_tool_call
    from nz_coder.tools import repo_map as module

    workspaces = (tmp_path / "one", tmp_path / "two")
    for workspace in workspaces:
        workspace.mkdir()

    def fail_index(workspace, *args, **kwargs):
        raise OSError(f"SENTINEL-{workspace.name}")

    monkeypatch.setattr(module, "_build_index", fail_index)

    def invoke(index: int):
        workspace = workspaces[index]
        session_id = f"session-{index}"
        call_id = f"call-{index}"
        with scoped_workdir(workspace), scoped_session(session_id), scoped_tool_call(call_id):
            return module.repo_map(), session_id, call_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(invoke, range(2)))

    ids = []
    for workspace, (result, session_id, call_id) in zip(workspaces, outcomes):
        diagnostic_id = _diagnostic_id(result)
        ids.append(diagnostic_id)
        failure = _operations(workspace, diagnostic_id)[-1]
        assert failure["identities"] == {
            "session_id": correlation(session_id),
            "call_id": correlation(call_id),
        }
        assert "SENTINEL" not in result
    assert ids[0] != ids[1]


def test_diagnostic_recorder_failure_does_not_mask_repo_map_failure(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state import diagnostics
    from nz_coder.tools import repo_map as module

    def recorder_failure(*args, **kwargs):
        raise OSError("SENTINEL-recorder-secret")

    def primary_failure(*args, **kwargs):
        raise ValueError("SENTINEL-primary-secret")

    monkeypatch.setattr(diagnostics.TraceRecorder, "log", recorder_failure)
    monkeypatch.setattr(module, "_build_index", primary_failure)
    with scoped_workdir(tmp_path):
        result = module.repo_map()

    _diagnostic_id(result)
    assert "evidence=unavailable" in result
    assert "SENTINEL" not in result


def test_repo_map_does_not_convert_cancellation_to_public_error(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import repo_map as module

    def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "_build_index", cancelled)
    with scoped_workdir(tmp_path), pytest.raises(asyncio.CancelledError):
        module.repo_map()
