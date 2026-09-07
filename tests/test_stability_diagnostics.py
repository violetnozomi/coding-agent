"""Secret-safe, best-effort diagnostics used by actual standalone operations."""
from __future__ import annotations

import asyncio
import json
import inspect
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from nz_coder.state.diagnostics import OperationDiagnostic


def test_exception_chain_has_locations_but_no_messages_locals_or_paths(tmp_path):
    secret = "SENTINEL-private-input-nonce-path"
    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path / secret)
    diagnostic.advance("index_read")
    try:
        try:
            raise OSError(secret)
        except OSError as cause:
            raise RuntimeError(secret) from cause
    except RuntimeError as error:
        public = diagnostic.failure(error)
    assert secret not in str(public)
    rows = [json.loads(line) for line in diagnostic.path.read_text().splitlines()]
    assert secret not in json.dumps(rows)
    failure = rows[-1]["operation"]
    assert failure["phase"] == "index_read"
    assert [item["type"] for item in failure["exceptions"]] == ["RuntimeError", "OSError"]
    assert failure["exceptions"][0]["frames"][-1]["line"] > 0
    assert diagnostic.id in str(public)
    assert diagnostic.saved


def test_storage_failure_is_safe_and_does_not_replace_primary(tmp_path, monkeypatch):
    import nz_coder.state.diagnostics as diagnostics

    def broken(*args, **kwargs):
        raise OSError("SENTINEL-storage-secret")

    monkeypatch.setattr(diagnostics.TraceRecorder, "log", broken)
    diagnostic = OperationDiagnostic("daemon", directory=tmp_path)
    diagnostic.advance("spawn")
    public = diagnostic.failure(ValueError("SENTINEL-primary-secret"))
    assert not diagnostic.saved
    assert diagnostic.primary_type == "ValueError"
    assert "unavailable" in str(public)
    assert "SENTINEL" not in str(public)


def test_secondary_error_preserves_first_failure(tmp_path):
    diagnostic = OperationDiagnostic("daemon", directory=tmp_path)
    diagnostic.advance("service_init")
    diagnostic.failure(ValueError("private"))
    diagnostic.cleanup_error(OSError("also private"))
    rows = [json.loads(line)["operation"] for line in diagnostic.path.read_text().splitlines()]
    assert rows[-1]["primary_type"] == "ValueError"
    assert rows[-1]["exceptions"][0]["type"] == "OSError"
    assert rows[-1]["phase"] == "cleanup"


@pytest.mark.parametrize("error", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_diagnostic_never_consumes_cancellation(error, tmp_path):
    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    with pytest.raises(type(error)) as caught:
        diagnostic.failure(error)
    assert caught.value is error


def test_diagnostics_bound_records_and_reject_arbitrary_fact_values(tmp_path):
    diagnostic = OperationDiagnostic("daemon", directory=tmp_path)
    for _ in range(200):
        diagnostic.advance("health_check", raw="SENTINEL", pid=23, alive=True)
    rows = diagnostic.path.read_text().splitlines()
    assert len(rows) <= 64
    assert "SENTINEL" not in "".join(rows)
    assert json.loads(rows[0])["operation"]["facts"] == {"pid": 23, "alive": True}


def test_custom_exception_name_is_not_a_public_secret_channel(tmp_path):
    error_type = type("SENTINEL_PRIVATE_TYPE", (Exception,), {})
    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    diagnostic.failure(error_type("private"))
    assert "SENTINEL" not in diagnostic.path.read_text()


def test_failure_and_cleanup_have_reserved_capacity_after_phase_limit(tmp_path):
    diagnostic = OperationDiagnostic("daemon", directory=tmp_path)
    for _ in range(200):
        diagnostic.advance("health_check")
    diagnostic.failure(OSError("SENTINEL"))
    diagnostic.cleanup_error(ValueError("SENTINEL"))
    rows = [json.loads(line)["operation"] for line in diagnostic.path.read_text().splitlines()]
    assert len(rows) <= 64
    assert rows[-2]["exceptions"][0]["type"] == "OSError"
    assert rows[-1]["exceptions"][0]["type"] == "ValueError"
    assert rows[-1]["facts"]["records_omitted"] > 0


def test_known_database_exception_keeps_actual_type_without_message(tmp_path):
    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    diagnostic.failure(sqlite3.OperationalError("SENTINEL-secret-database-path"))
    row = json.loads(diagnostic.path.read_text().splitlines()[-1])["operation"]
    assert row["exceptions"][0]["type"] == "OperationalError"
    assert "SENTINEL" not in diagnostic.path.read_text()


def test_custom_traceback_accessor_cannot_replace_original_failure(tmp_path):
    class BrokenTraceback(Exception):
        def __getattribute__(self, name):
            if name == "__traceback__":
                raise OSError("SENTINEL-secondary-error")
            return super().__getattribute__(name)

    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    public = diagnostic.failure(BrokenTraceback("SENTINEL-original"))
    assert diagnostic.primary_type == "Exception"
    assert "SENTINEL" not in str(public)


def test_first_failure_keeps_entry_phase_across_concurrent_capture(tmp_path, monkeypatch):
    import nz_coder.state.diagnostics as diagnostics

    entered, release = Event(), Event()
    original = diagnostics.exception_evidence

    def held(error):
        entered.set()
        assert release.wait(5)
        return original(error)

    monkeypatch.setattr(diagnostics, "exception_evidence", held)
    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    diagnostic.advance("index_read")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(diagnostic.failure, ValueError("private"))
        try:
            assert entered.wait(5)
            diagnostic.advance("render")
        finally:
            release.set()
        future.result(timeout=5)
    assert diagnostic.primary_phase == "index_read"
    rows = [json.loads(line)["operation"] for line in diagnostic.path.read_text().splitlines()]
    assert rows[-1]["phase"] == "index_read"


def test_capture_reserves_first_error_without_persistence_or_extraction(tmp_path, monkeypatch):
    import nz_coder.state.diagnostics as diagnostics

    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    diagnostic.advance("index_read")
    previous = diagnostic.path.read_bytes()
    with monkeypatch.context() as scope:
        scope.setattr(diagnostics, "exception_evidence", lambda *args: pytest.fail("capture performed extraction"))
        scope.setattr(diagnostics.TraceRecorder, "log", lambda *args, **kwargs: pytest.fail("capture performed I/O"))
        captured = diagnostic.capture(OSError("SENTINEL"))
    assert diagnostic.path.read_bytes() == previous
    diagnostic.advance("render")
    assert "evidence=unavailable" in str(diagnostic.public_failure())
    diagnostic.persist(captured)
    assert diagnostic.primary_phase == "index_read"
    assert diagnostic.primary_type == "OSError"
    assert "evidence=saved" in str(diagnostic.public_failure())


def test_deep_trace_keeps_innermost_location_and_explicit_omission():
    from nz_coder.state.diagnostics import exception_evidence

    def nested(depth):
        if depth:
            return nested(depth - 1)
        raise ValueError("SENTINEL")

    try:
        nested(40)
    except ValueError as error:
        evidence = exception_evidence(error)[0]
        last = error.__traceback__
        while last.tb_next is not None:
            last = last.tb_next
        assert evidence["frames"][-1]["line"] == last.tb_lineno
    assert len(evidence["frames"]) == 24
    assert evidence["frames_omitted"] > 0


def test_deferred_primary_has_its_own_slot_after_secondary_failures(tmp_path):
    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    for _ in range(60):
        diagnostic.advance("index_read")
    primary = diagnostic.capture(OSError("SENTINEL-first"))
    for _ in range(4):
        diagnostic.failure(RuntimeError("SENTINEL-secondary"))
    diagnostic.persist(primary)
    rows = [json.loads(line)["operation"] for line in diagnostic.path.read_text().splitlines()]
    first = [row for row in rows if row["facts"].get("primary_record")]
    assert len(rows) <= 64
    assert len(first) == 1
    assert first[0]["exceptions"][0]["type"] == "OSError"
    assert first[0]["facts"]["records_omitted"] > 0


def test_concurrent_secondary_write_cannot_claim_failed_primary_was_saved(tmp_path, monkeypatch):
    diagnostic = OperationDiagnostic("repo_map", directory=tmp_path)
    diagnostic.advance("index_read")
    original_log = diagnostic._recorder.log
    entered, release = Event(), Event()
    source, start = inspect.getsourcelines(OperationDiagnostic._write)
    return_line = max(start + offset for offset, line in enumerate(source)
                      if line.startswith("        return "))

    def log(event, **data):
        if data["operation"]["facts"].get("primary_record"):
            raise OSError("SENTINEL-primary-write-failed")
        return original_log(event, **data)

    def pause_before_return(frame, event, arg):
        if (frame.f_code is OperationDiagnostic._write.__code__ and event == "line"
                and frame.f_lineno == return_line and frame.f_locals["facts"].get("primary_record")):
            entered.set()
            assert release.wait(10)
        return pause_before_return

    def fail_primary():
        sys.settrace(pause_before_return)
        try:
            return diagnostic.failure(OSError("SENTINEL-primary"))
        finally:
            sys.settrace(None)

    monkeypatch.setattr(diagnostic._recorder, "log", log)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fail_primary)
        try:
            assert entered.wait(10)
            diagnostic.cleanup_error(RuntimeError("SENTINEL-secondary"))
        finally:
            release.set()
        public = future.result(timeout=10)
    rows = [json.loads(line)["operation"] for line in diagnostic.path.read_text().splitlines()]
    assert not any(row["facts"].get("primary_record") for row in rows)
    assert "evidence=unavailable" in str(public)
