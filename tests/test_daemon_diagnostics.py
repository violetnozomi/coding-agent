"""Owned daemon startup failures retain bounded evidence without authentication data."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import pytest

from nz_coder.http_service import daemon
from nz_coder.protocol.public_error import PublicRuntimeError


SECRET = "SENTINEL-private-nonce-token-path"


def _records(root):
    return [
        json.loads(line)["operation"]
        for path in root.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _assert_safe(error, records):
    assert isinstance(error, PublicRuntimeError)
    assert error.public_error.code == "daemon_failed"
    assert SECRET not in str(error)
    assert SECRET not in json.dumps(records)
    assert "diag-" in str(error)


@pytest.mark.parametrize("collector_failure", [False, True])
def test_spawn_failure_cleans_owned_token_and_lock_even_without_evidence(
    tmp_path, monkeypatch, collector_failure,
):
    """A failed Popen must not strand the lifecycle fence or expose its message."""
    paths = daemon.daemon_paths(state_root=tmp_path / SECRET)

    def fail_spawn(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(daemon.subprocess, "Popen", fail_spawn)
    if collector_failure:
        from nz_coder.state.trace import TraceRecorder
        monkeypatch.setattr(TraceRecorder, "log", fail_spawn)
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path / SECRET, port=0)
    assert not paths.token.exists()
    assert not paths.lock.exists()
    assert not paths.state.exists()
    rows = _records(tmp_path)
    _assert_safe(caught.value, rows)
    assert caught.value.public_error.metadata["evidence_saved"] is not collector_failure
    if not collector_failure:
        first = next(row for row in rows if row["exceptions"])
        assert first["phase"] == "spawn"
        assert first["exceptions"][0]["type"] == "OSError"
        assert any(row["facts"].get("cleanup_complete") for row in rows)


def test_spawn_failure_preserves_replacement_state_token_and_lock(tmp_path, monkeypatch):
    """An operation that loses ownership may not remove another instance's files."""
    paths = daemon.daemon_paths(state_root=tmp_path)
    other_state = {"nonce": "foreign", "pid": 987654, "process_identity": "foreign"}
    other_lock = {"pid": 987654, "process_identity": "foreign"}

    def fail_spawn(*args, **kwargs):
        daemon._atomic_state(paths.state, other_state)
        daemon._atomic_state(paths.lock, other_lock)
        daemon._atomic_private_text(paths.token, "foreign-token-must-survive")
        raise OSError(SECRET)

    monkeypatch.setattr(daemon.subprocess, "Popen", fail_spawn)
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path, port=0)
    assert daemon._load_state(paths.state) == other_state
    assert daemon._load_state(paths.lock) == other_lock
    assert paths.token.read_text() == "foreign-token-must-survive"
    rows = _records(tmp_path)
    _assert_safe(caught.value, rows)
    assert any(row["facts"].get("foreign_state_preserved") for row in rows)


def test_initial_probe_consumes_the_single_startup_budget(tmp_path, monkeypatch):
    """Resetting the deadline after preflight would wrongly spawn another process."""
    clock = [0.0]
    probe_timeouts = []

    def status(*args, timeout=0.75, **kwargs):
        probe_timeouts.append(timeout)
        clock[0] += 1.0
        return {"running": False, "reason": "not_started"}

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("startup budget was already exhausted before spawn")

    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon, "daemon_status", status)
    monkeypatch.setattr(daemon.subprocess, "Popen", unexpected_spawn)
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path, port=0, startup_timeout=0.1)
    assert probe_timeouts == [0.1]
    _assert_safe(caught.value, _records(tmp_path))
    assert "deadline_exhausted" in str(caught.value)


@pytest.mark.parametrize("missing_log", [False, True])
def test_real_preinit_exit_retains_exit_and_safe_stderr_before_cleanup(
    tmp_path, monkeypatch, missing_log,
):
    """A child exiting before daemon import has no worker trace, but has an exit code."""
    real_popen = subprocess.Popen
    children = []
    paths = daemon.daemon_paths(state_root=tmp_path / SECRET)
    if missing_log:
        original_open = Path.open

        def anonymous_capture(path, *args, **kwargs):
            if path == paths.log and args == ("a+b",):
                return tempfile.TemporaryFile(mode="w+b", dir=paths.root)
            return original_open(path, *args, **kwargs)

        # Windows may not unlink an open file. Model a missing pathname with
        # an actual anonymous descriptor, retaining real subprocess writes.
        monkeypatch.setattr(daemon, "_ensure_private_log", lambda path: None)
        monkeypatch.setattr(Path, "open", anonymous_capture)

    def spawn(argv, **kwargs):
        assert kwargs.get("shell", False) is False
        process = real_popen([
            sys.executable, "-c",
            f"import sys; sys.stderr.write({SECRET!r} * 3000 + '\\n'); sys.exit(17)",
        ], **kwargs)
        children.append(process)
        process.wait(timeout=5)
        return process

    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    try:
        with pytest.raises(RuntimeError) as caught:
            daemon.start_daemon(state_root=tmp_path / SECRET, port=0, startup_timeout=20)
        rows = _records(tmp_path)
        _assert_safe(caught.value, rows)
        assert "child_exited" in str(caught.value)
        assert any(row["facts"].get("exit_code") == 17 for row in rows)
        assert any(row["facts"].get("stderr_present") for row in rows)
        assert any(row["facts"].get("stderr_truncated") for row in rows)
        assert all(row["facts"].get("stderr_bytes", 0) <= 65536 for row in rows)
        assert any(row["facts"].get("log_present") is (not missing_log) for row in rows)
        assert not paths.token.exists() and not paths.lock.exists()
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_real_alive_gate_exhausts_budget_and_reaps_only_owned_child(tmp_path, monkeypatch):
    """A socket notification proves the child reached its gate; no sleep predicts it."""
    real_popen = subprocess.Popen
    children = []
    paths = daemon.daemon_paths(state_root=tmp_path)
    clock = [0.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        address = listener.getsockname()

        def spawn(argv, **kwargs):
            process = real_popen([
                sys.executable, "-c",
                "import socket; "
                f"s=socket.create_connection({address!r}); "
                "s.sendall(b'entered'); s.recv(1)",
            ], **kwargs)
            children.append(process)
            return process

        monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
        connection = None
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    daemon.start_daemon, state_root=tmp_path, port=0, startup_timeout=1.5,
                )
                try:
                    connection, _ = listener.accept()
                    connection.settimeout(5)
                    assert connection.recv(7) == b"entered"
                finally:
                    # Also release on watchdog/assertion failure before the
                    # executor's joining context can wait for this operation.
                    clock[0] = 2.0
                with pytest.raises(RuntimeError) as caught:
                    future.result(timeout=6)
            rows = _records(tmp_path)
            _assert_safe(caught.value, rows)
            assert "deadline_exhausted" in str(caught.value)
            assert any(row["facts"].get("alive") is True for row in rows)
            assert all(process.poll() is not None for process in children)
            assert not paths.token.exists() and not paths.lock.exists()
        finally:
            if connection is not None:
                connection.close()
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


def test_worker_token_failure_is_caught_and_recorded_before_owned_cleanup(tmp_path):
    """Reading the token outside the worker try loses the first failing phase."""
    paths = daemon.daemon_paths(state_root=tmp_path / SECRET)
    daemon._prepare_private_dir(paths.root)
    daemon._atomic_state(paths.state, {"nonce": SECRET, "pid": os.getpid()})
    daemon._atomic_private_text(paths.token, "short")
    args = argparse.Namespace(
        profile="default", state_root=str(tmp_path / SECRET), nonce=SECRET,
        handoff_timeout=1, host="127.0.0.1", port=0,
        interaction_timeout=1, workspace=[],
    )
    assert daemon._serve_worker(args) == 4
    rows = _records(tmp_path)
    assert SECRET not in json.dumps(rows)
    first = next(row for row in rows if row["exceptions"])
    assert first["phase"] == "token_read"
    assert first["exceptions"][0]["type"] == "RuntimeError"
    assert not paths.state.exists() and not paths.token.exists()


def test_worker_partial_cleanup_is_not_reported_complete(tmp_path, monkeypatch):
    """Removing state before a failed token unlink is only partial cleanup."""
    paths = daemon.daemon_paths(state_root=tmp_path)
    daemon._prepare_private_dir(paths.root)
    daemon._atomic_state(paths.state, {"nonce": SECRET, "pid": os.getpid()})
    daemon._atomic_private_text(paths.token, "short")
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == paths.token:
            raise PermissionError(SECRET)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    args = argparse.Namespace(
        profile="default", state_root=str(tmp_path), nonce=SECRET,
        handoff_timeout=1, host="127.0.0.1", port=0,
        interaction_timeout=1, workspace=[],
    )
    assert daemon._serve_worker(args) == 4
    rows = _records(tmp_path)
    assert rows[-1]["facts"]["cleanup_complete"] is False
    assert rows[-1]["primary_type"] == "RuntimeError"
    assert paths.token.exists()
    assert any(row["exceptions"] and row["exceptions"][0]["type"] == "PermissionError" for row in rows)
    assert SECRET not in json.dumps(rows)


def test_worker_service_failure_never_writes_raw_exception_log(tmp_path, monkeypatch):
    paths = daemon.daemon_paths(state_root=tmp_path)
    daemon._prepare_private_dir(paths.root)
    daemon._atomic_state(paths.state, {"nonce": SECRET, "pid": os.getpid()})
    daemon._atomic_private_text(paths.token, "valid-private-token-value")

    def failed_service(**kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(daemon, "SessionHTTPService", failed_service)
    args = argparse.Namespace(
        profile="default", state_root=str(tmp_path), nonce=SECRET,
        handoff_timeout=1, host="127.0.0.1", port=0,
        interaction_timeout=1, workspace=[],
    )
    assert daemon._serve_worker(args) == 4
    rows = _records(tmp_path)
    assert SECRET not in json.dumps(rows)
    assert not paths.log.exists() or SECRET not in paths.log.read_text()
    first = next(row for row in rows if row["exceptions"])
    assert first["phase"] == "service_init"
    assert first["exceptions"][0]["type"] == "OSError"


class _Child:
    """Specific Popen boundary for deterministic clock and health experiments."""

    pid = 424242
    returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout):
        assert timeout >= 0
        assert self.returncode is not None
        return self.returncode


def _ready_child(monkeypatch, paths, child):
    original = daemon._atomic_state

    def publish(path, payload):
        if path == paths.state and payload.get("status") == "starting":
            payload = {**payload, "status": "ready", "endpoint": "http://127.0.0.1:1234"}
        original(path, payload)

    monkeypatch.setattr(daemon, "_atomic_state", publish)
    monkeypatch.setattr(daemon, "_process_identity", lambda pid: f"owned-{pid}")
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: child)


def test_wrong_health_identity_is_not_readiness_and_is_saved_before_reaping(tmp_path, monkeypatch):
    """An open endpoint returning another nonce cannot satisfy this start request."""
    paths = daemon.daemon_paths(state_root=tmp_path)

    class Child(_Child):
        def terminate(self):
            rows = _records(tmp_path)
            first = next(row for row in rows if row["exceptions"])
            assert first["phase"] == "identity_mismatch"
            assert any(row["facts"].get("state_present") for row in rows)
            super().terminate()

    child = Child()
    _ready_child(monkeypatch, paths, child)
    monkeypatch.setattr(daemon.NZCoderClient, "health", lambda self: {
        "pid": child.pid, "runtime": {"profile": "default", "nonce": SECRET},
    })
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path, port=0, startup_timeout=1)
    rows = _records(tmp_path)
    _assert_safe(caught.value, rows)
    assert "identity_mismatch" in str(caught.value)
    assert child.poll() is not None
    assert not paths.state.exists() and not paths.token.exists()


def test_health_probe_receives_only_remaining_budget_after_spawn(tmp_path, monkeypatch):
    """A 0.5-second default probe cannot overrun a 0.05-second remaining budget."""
    paths = daemon.daemon_paths(state_root=tmp_path)
    child = _Child()
    clock = [0.0]
    seen = []
    _ready_child(monkeypatch, paths, child)

    def spawn(*args, **kwargs):
        clock[0] += 0.15
        return child

    def health(client):
        seen.append(client.timeout)
        clock[0] += client.timeout
        raise TimeoutError(SECRET)

    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    monkeypatch.setattr(daemon.NZCoderClient, "health", health)
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path, port=0, startup_timeout=0.2)
    assert seen == [pytest.approx(0.05)]
    assert clock[0] == pytest.approx(0.2)
    assert "deadline_exhausted" in str(caught.value)
    _assert_safe(caught.value, _records(tmp_path))


def test_snapshot_failure_cannot_mask_primary_or_skip_cleanup(tmp_path, monkeypatch):
    """Even a collector implementation fault must leave the original spawn failure."""
    paths = daemon.daemon_paths(state_root=tmp_path)

    def fail_spawn(*args, **kwargs):
        raise ValueError(SECRET)

    def fail_snapshot(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(daemon.subprocess, "Popen", fail_spawn)
    monkeypatch.setattr(daemon, "_startup_snapshot", fail_snapshot)
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path, port=0)
    rows = _records(tmp_path)
    _assert_safe(caught.value, rows)
    assert "spawn" in str(caught.value)
    assert not paths.token.exists() and not paths.lock.exists()
    errors = [row for row in rows if row["exceptions"]]
    assert errors[0]["exceptions"][0]["type"] == "ValueError"
    assert errors[1]["exceptions"][0]["type"] == "OSError"
    assert all(row["primary_type"] == "ValueError" for row in errors)


@pytest.mark.parametrize("successful", [False, True])
def test_capture_close_failure_cannot_replace_startup_outcome(tmp_path, monkeypatch, successful):
    """Closing a diagnostic descriptor is not an authority over startup success/failure."""
    paths = daemon.daemon_paths(state_root=tmp_path)
    original_open = Path.open

    class Capture:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.handle.close()
            raise OSError(SECRET)

    def open_path(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return Capture(handle) if path == paths.log and args == ("a+b",) else handle

    monkeypatch.setattr(Path, "open", open_path)
    if successful:
        child = _Child()
        _ready_child(monkeypatch, paths, child)
        monkeypatch.setattr(daemon.NZCoderClient, "health", lambda self: {
            "pid": child.pid,
            "runtime": {"profile": "default", "nonce": daemon._load_state(paths.state)["nonce"]},
        })
        assert daemon.start_daemon(state_root=tmp_path, port=0)["running"]
    else:
        def fail_spawn(*args, **kwargs):
            raise ValueError(SECRET)
        monkeypatch.setattr(daemon.subprocess, "Popen", fail_spawn)
        with pytest.raises(RuntimeError) as caught:
            daemon.start_daemon(state_root=tmp_path, port=0)
        _assert_safe(caught.value, _records(tmp_path))
        assert "spawn" in str(caught.value)
        assert not paths.token.exists() and not paths.lock.exists()
    errors = [row for row in _records(tmp_path) if row["exceptions"]]
    assert errors[-1]["exceptions"][0]["type"] == "OSError"
    assert errors[-1]["phase"] == "cleanup"


def test_real_worker_initialization_error_links_parent_exit_to_original_source(tmp_path, monkeypatch):
    """An actual worker must retain its first failure, not just the parent's exit label."""
    real_popen = subprocess.Popen
    children = []
    script = (
        "import sys\n"
        "from nz_coder.http_service import daemon\n"
        "def failed_service(**kwargs):\n"
        f"    raise ImportError({SECRET!r})\n"
        "daemon.SessionHTTPService = failed_service\n"
        "sys.exit(daemon.daemon_main(sys.argv[1:]))\n"
    )

    def spawn(argv, **kwargs):
        assert argv[1:5] == ["-m", "nz_coder", "daemon", "_serve"]
        process = real_popen([sys.executable, "-c", script, *argv[4:]], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    try:
        with pytest.raises(RuntimeError) as caught:
            daemon.start_daemon(state_root=tmp_path, port=0, startup_timeout=20)
        rows = _records(tmp_path)
        _assert_safe(caught.value, rows)
        identity = caught.value.public_error.metadata["diagnostic_id"]
        assert all(row["id"] == identity for row in rows)
        failure = next(row for row in rows if row["primary_type"] == "ImportError")
        assert failure["phase"] == "service_init"
        assert failure["last_completed_phase"] == "token_read"
        assert any(
            frame["module"] == "nz_coder.http_service.daemon" and frame["function"] == "_serve_worker"
            for frame in failure["exceptions"][0]["frames"]
        )
        assert any(row["facts"].get("exit_code") == 4 for row in rows)
        assert all(process.poll() == 4 for process in children)
        paths = daemon.daemon_paths(state_root=tmp_path)
        assert SECRET not in paths.log.read_text()
        assert not paths.token.exists() and not paths.lock.exists()
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_failed_reap_retains_live_instance_state_and_primary_failure(tmp_path, monkeypatch):
    """Losing both termination attempts must not erase management state of a live child."""
    paths = daemon.daemon_paths(state_root=tmp_path)

    class Child(_Child):
        def terminate(self):
            raise PermissionError(SECRET)

        def kill(self):
            raise PermissionError(SECRET)

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("owned-child", timeout)

    child = Child()
    _ready_child(monkeypatch, paths, child)
    monkeypatch.setattr(daemon.NZCoderClient, "health", lambda self: {
        "pid": child.pid, "runtime": {"profile": "default", "nonce": "wrong"},
    })
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path, port=0, startup_timeout=1)
    rows = _records(tmp_path)
    _assert_safe(caught.value, rows)
    assert "identity_mismatch" in str(caught.value)
    assert paths.state.exists() and paths.token.exists() and paths.lock.exists()
    assert daemon._load_state(paths.state)["pid"] == child.pid
    assert any(
        row["facts"].get("cleanup_complete") is False and row["facts"].get("alive") is True
        for row in rows
    )


def test_final_cleanup_poll_failure_keeps_primary_and_reports_unknown(tmp_path, monkeypatch):
    """A fifth-poll failure after owned cleanup cannot escape as a raw exception."""
    paths = daemon.daemon_paths(state_root=tmp_path)

    class ExitedChild:
        pid = 424242
        returncode = 17
        polls = 0

        def poll(self):
            self.polls += 1
            # First exit check, two snapshot samples, and pre-wait cleanup
            # succeed. Only the final post-cleanup status sample fails.
            if self.polls == 5:
                raise OSError(SECRET)
            return self.returncode

        def wait(self, timeout):
            assert timeout >= 0
            return self.returncode

    child = ExitedChild()
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: child)
    with pytest.raises(RuntimeError) as caught:
        daemon.start_daemon(state_root=tmp_path, port=0)
    rows = _records(tmp_path)
    _assert_safe(caught.value, rows)
    assert "child_exited" in str(caught.value)
    assert child.polls == 5
    assert not any(path.exists() for path in (paths.state, paths.token, paths.lock))
    failures = [row for row in rows if row["exceptions"]]
    assert failures[0]["exceptions"][0]["type"] == "ChildProcessError"
    assert failures[-1]["exceptions"][0]["type"] == "OSError"
    assert failures[-1]["phase"] == "cleanup"
    assert failures[-1]["primary_type"] == "ChildProcessError"
    assert failures[-1]["primary_phase"] == "child_exited"
    assert rows[-1]["phase"] == "finished"
    assert rows[-1]["facts"]["alive"] is None
    assert rows[-1]["facts"]["exit_code"] is None
    assert rows[-1]["facts"]["cleanup_complete"] is True


def test_failure_cleanup_preserves_a_real_other_child_and_its_replacement_state(tmp_path, monkeypatch):
    """Cleanup uses the retained child handle, never the PID in replacement state."""
    real_popen = subprocess.Popen
    paths = daemon.daemon_paths(state_root=tmp_path)
    children = []
    connections = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(5)
        script = (
            "import socket; "
            f"s=socket.create_connection({listener.getsockname()!r}); "
            "s.sendall(b'entered'); s.recv(1)"
        )

        def gated_process(**kwargs):
            process = real_popen([sys.executable, "-c", script], **kwargs)
            children.append(process)
            connection, _ = listener.accept()
            connections.append(connection)
            connection.settimeout(5)
            assert connection.recv(7) == b"entered"
            return process

        try:
            foreign = gated_process(
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            other_state = {
                "pid": foreign.pid, "process_identity": daemon._process_identity(foreign.pid),
                "nonce": "other-instance", "endpoint": "", "profile": "default",
            }
            other_lock = {"pid": foreign.pid, "process_identity": other_state["process_identity"],
                          "owner_id": "other-operation"}
            original = daemon._atomic_state

            def replace_handoff(path, payload):
                if path == paths.state and payload.get("status") == "starting":
                    original(paths.state, other_state)
                    original(paths.lock, other_lock)
                    daemon._atomic_private_text(paths.token, "other-private-token")
                    raise RuntimeError("handoff ownership replaced")
                else:
                    original(path, payload)

            monkeypatch.setattr(daemon, "_atomic_state", replace_handoff)
            monkeypatch.setattr(daemon.subprocess, "Popen", lambda argv, **kwargs: gated_process(**kwargs))
            with pytest.raises(RuntimeError) as caught:
                daemon.start_daemon(state_root=tmp_path, port=0, startup_timeout=20)
            _assert_safe(caught.value, _records(tmp_path))
            assert "handoff" in str(caught.value)
            assert children[1].poll() is not None
            assert foreign.poll() is None
            assert daemon._load_state(paths.state) == other_state
            assert daemon._load_state(paths.lock) == other_lock
            assert paths.token.read_text() == "other-private-token"
        finally:
            for connection in connections:
                connection.close()
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
