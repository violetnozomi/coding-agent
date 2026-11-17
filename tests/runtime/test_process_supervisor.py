"""Real spawned workers exercise bounded parent supervision and ownership."""
from __future__ import annotations

import importlib
import multiprocessing
import threading
import time
from dataclasses import replace

import pytest

from tests.fixtures import subagent_process_targets as targets


_OWNED_SCOPES = {}


def _supervisor(tmp_path, monkeypatch, target, **payload):
    module = importlib.import_module("nz_coder.runtime.agent.process_supervisor")
    monkeypatch.setattr(module, "_run_subagent_process", target)
    directory = tmp_path / "invocation"
    directory.mkdir(mode=0o700)
    if __import__("os").name == "nt":
        from nz_coder.foundation.private_paths import harden_private_path

        assert harden_private_path(directory).hardened
    bound_payload = {"workspace": str(tmp_path), "parent_session_id": "parent", "parent_model_id": "offline-parent",
                     "session_id": "child", "settlement_id": "round-1", "prompt": "offline", "agent_type": "explore", **payload}
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.process.command_broker import ParentCommandScope
    from nz_coder.runtime.process.command_channel import CommandChannelLimits
    from nz_coder.runtime.process.workdir import scoped_workdir

    deadline = time.monotonic() + 12
    settings = replace(RunSettings.from_legacy_globals(), subagent_worktree_enabled=False)
    with scoped_workdir(tmp_path), scoped_run_settings(settings):
        state = subagent._new_subagent_state("parent", "explore", ["bash"])
        state.update(session_id="child", settlement_id="round-1")
        from tests.fixtures.process_lifecycle import make_preparation_scope, complete_prepared_evidence
        preparation = make_preparation_scope(tmp_path, settings=settings, deadline=deadline, settlement_id="round-1")
        try:
            prepared, authority = subagent.prepare_subagent_command_context(bound_payload["prompt"], state, workspace=tmp_path, config_snapshot=None, settings=settings, settlement_id="round-1", preparation=preparation)
            prepared = complete_prepared_evidence(prepared, state, preparation)
            preparation.seal()
        finally:
            assert preparation.close(deadline=time.monotonic() + 2)
        subagent._save_subagent_state("parent", state, tmp_path)
    holder = {}
    scope = ParentCommandScope(authority, deadline=deadline, child_publisher=None, on_channel_error=lambda error: holder["supervisor"].fail(error))
    endpoint = scope.start()
    bound_payload.update(prepared_workspace=prepared, command_settings=authority.run_settings,
                         memory_scope=state["memory_scope"],
                         command_limits=CommandChannelLimits.from_settings(authority.run_settings),
                         command_deadline=deadline)
    supervisor = module.SubagentProcessSupervisor(bound_payload, module.ChildProcessResultFile.capture(directory / "result.json"), command_endpoint=endpoint, on_stop=scope.request_stop)
    holder["supervisor"] = supervisor
    _OWNED_SCOPES[supervisor] = scope
    return supervisor


def _ready(supervisor, tmp_path):
    deadline = time.monotonic() + 3
    while not (tmp_path / "invocation" / "ready").exists():
        assert time.monotonic() < deadline, "child never reached its ready point"
        assert supervisor.process.is_alive()
        time.sleep(0.01)


def _cleanup(supervisor, caller=None):
    scope = _OWNED_SCOPES.pop(supervisor)
    scope.request_stop()
    process = supervisor.process
    if process is not None:
        try:
            if process.is_alive():
                process.kill()
            process.join(3)
        except ValueError:
            pass
    if caller is not None:
        caller.join(3)
        assert not caller.is_alive()
    supervisor.close(deadline=time.monotonic() + 0.5)
    assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("characters", [512 * 1024, 2_000_000])
def test_large_result_exits_without_pipe_backpressure(tmp_path, monkeypatch, characters):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.large_result, characters=characters)
    try:
        supervisor.start()
        result = supervisor.wait(threading.Event(), deadline=time.monotonic() + 5, stop_grace=0.1)
        assert result.reason == "exit"
        assert result.result == "中" * characters
        assert result.exit_code == 0
        assert supervisor.process.is_alive() is False
    finally:
        _cleanup(supervisor)


def test_parent_deadline_stops_uncooperative_child(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.blocking)
    deadline = time.monotonic() + 0.2
    try:
        supervisor.start()
        started = time.monotonic()
        result = supervisor.wait(threading.Event(), deadline=deadline, stop_grace=0.1)
        assert result.reason == "timeout"
        assert time.monotonic() - started < 1.5
        assert result.exit_code != 0
        assert supervisor.process.is_alive() is False
    finally:
        _cleanup(supervisor)


def test_cancellation_wins_over_late_result(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.result_then_wait)
    cancel = threading.Event()
    try:
        supervisor.start()
        _ready(supervisor, tmp_path)
        cancel.set()
        result = supervisor.wait(cancel, deadline=time.monotonic() - 1, stop_grace=0.1)
        assert result.reason == "cancelled"
        assert result.result != "中" * (512 * 1024)
        assert supervisor.process.is_alive() is False
    finally:
        _cleanup(supervisor)


def test_result_after_deadline_cannot_become_success(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.result_then_wait)
    try:
        supervisor.start()
        _ready(supervisor, tmp_path)
        result = supervisor.wait(threading.Event(), deadline=time.monotonic() - 1, stop_grace=0.1)
        assert result.reason == "timeout"
        assert supervisor.process.is_alive() is False
    finally:
        _cleanup(supervisor)


def test_unconfirmed_exit_retains_handle_for_retry(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.blocking)
    results = []
    caller = None
    try:
        supervisor.start()
        _ready(supervisor, tmp_path)
        with monkeypatch.context() as fault:
            fault.setattr(supervisor.process, "terminate", lambda: None)
            fault.setattr(supervisor.process, "kill", lambda: None)
            caller = threading.Thread(target=lambda: results.append(supervisor.wait(threading.Event(), deadline=time.monotonic() + 5, stop_grace=0.01)))
            caller.start()
            started = time.monotonic()
            assert supervisor.close(deadline=started + 0.05) is False
            assert time.monotonic() - started < 0.4
            assert supervisor.process.is_alive() is True
            assert not results
        caller.join(2)
        assert not caller.is_alive()
        assert len(results) == 1 and results[0].reason == "cancelled"
        assert supervisor.close(deadline=time.monotonic() + 0.5) is True
    finally:
        _cleanup(supervisor, caller)


def test_start_failure_retires_allocated_resources(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.blocking)
    process_class = multiprocessing.get_context("spawn").Process
    original_start = process_class.start
    started = []

    def partial_start(process):
        original_start(process)
        started.append(process)
        raise RuntimeError("injected partial spawn failure")

    with monkeypatch.context() as fault:
        fault.setattr(process_class, "start", partial_start)
        with pytest.raises(RuntimeError, match="partial spawn"):
            supervisor.start()
    assert len(started) == 1
    try:
        try:
            assert started[0].is_alive() is False
        except ValueError:
            assert started[0]._closed
        assert not list((tmp_path / "invocation").glob("*.tmp"))
        assert supervisor.close(deadline=time.monotonic() + 0.05) is True
    finally:
        _cleanup(supervisor)


@pytest.mark.parametrize("target,exit_code", [(targets.exit_without_result, 0), (targets.exit_without_result, 17), (targets.invalid_result, 0), (targets.failed_write, 0)], ids=["missing", "crashed", "invalid", "write-failed"])
def test_missing_or_invalid_result_is_secret_safe(tmp_path, monkeypatch, target, exit_code):
    supervisor = _supervisor(tmp_path, monkeypatch, target, exit_code=exit_code)
    try:
        supervisor.start()
        result = supervisor.wait(threading.Event(), deadline=time.monotonic() + 5, stop_grace=0.1)
        assert result.reason == "error"
        assert result.public_error is not None
        assert "sentinel-secret" not in repr(result)
        assert supervisor.process.is_alive() is False
    finally:
        _cleanup(supervisor)


@pytest.mark.parametrize("cancel", [False, True])
def test_child_save_failure_preserves_canonical_in_same_transport(tmp_path, monkeypatch, cancel):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.canonical_result, block_after_result=cancel)
    cancellation = threading.Event()
    try:
        supervisor.start()
        if cancel:
            _ready(supervisor, tmp_path)
            cancellation.set()
        result = supervisor.wait(cancellation, deadline=time.monotonic() + 5, stop_grace=0.1)
        assert result.reason == ("cancelled" if cancel else "exit")
        assert result.canonical["usage"]["total"] == 18
        assert result.canonical["cost"] == 0.125
        assert result.canonical["structured"] == {"answer": 42}
        assert result.canonical_settlement_id == "round-1"
    finally:
        _cleanup(supervisor)


@pytest.mark.parametrize("identity", ["task_id", "session_id", "parent_session_id", "settlement"])
def test_foreign_canonical_is_rejected(tmp_path, monkeypatch, identity):
    supervisor = _supervisor(tmp_path, monkeypatch, targets.canonical_result, wrong_identity=identity)
    try:
        supervisor.start()
        result = supervisor.wait(threading.Event(), deadline=time.monotonic() + 5, stop_grace=0.1)
        assert result.reason == "error"
        assert result.public_error.code == "child_process_protocol_error"
        assert result.canonical is None
    finally:
        _cleanup(supervisor)
