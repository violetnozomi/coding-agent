from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import time
import pytest

from io import StringIO

from nz_coder.http_service.client import NZCoderClient, NZCoderHTTPError
from nz_coder.http_service.daemon import (
    daemon_main,
    daemon_paths,
    daemon_status,
    start_daemon,
    stop_daemon,
)
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import save_session


def test_windows_process_identity_uses_native_creation_time_without_shell():
    from nz_coder.http_service.daemon import _windows_process_start_time

    class Kernel32:
        def __init__(self):
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            assert (access, inherit, pid) == (0x1000, False, 4242)
            return 73

        def GetProcessTimes(self, handle, creation, exit_time, kernel, user):
            assert handle == 73
            creation._obj.dwHighDateTime = 0x12345678
            creation._obj.dwLowDateTime = 0x9ABCDEF0
            return 1

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return 1

    kernel32 = Kernel32()

    marker = _windows_process_start_time(4242, kernel32=kernel32)

    assert marker == str((0x12345678 << 32) | 0x9ABCDEF0)
    assert kernel32.closed == [73]


def test_windows_pid_liveness_uses_waitable_process_handle():
    from nz_coder.http_service.daemon import _windows_pid_alive

    class Kernel32:
        def __init__(self, wait_result):
            self.wait_result = wait_result
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            assert (access, inherit, pid) == (0x00100000, False, 4242)
            return 91

        def WaitForSingleObject(self, handle, timeout):
            assert (handle, timeout) == (91, 0)
            return self.wait_result

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return 1

    running = Kernel32(0x00000102)
    exited = Kernel32(0x00000000)

    assert _windows_pid_alive(4242, kernel32=running) is True
    assert _windows_pid_alive(4242, kernel32=exited) is False
    assert running.closed == [91]
    assert exited.closed == [91]


def test_windows_pid_liveness_classifies_missing_and_inaccessible_processes():
    from nz_coder.http_service.daemon import _windows_pid_alive

    class Kernel32:
        def __init__(self, error):
            self.error = error

        def OpenProcess(self, _access, _inherit, _pid):
            return 0

        def GetLastError(self):
            return self.error

    assert _windows_pid_alive(4242, kernel32=Kernel32(87)) is False
    assert _windows_pid_alive(4242, kernel32=Kernel32(5)) is True


def test_windows_pid_liveness_never_falls_back_to_posix_kill(monkeypatch):
    import nz_coder.http_service.daemon as daemon

    monkeypatch.setattr(daemon.os, "name", "nt")
    monkeypatch.setattr(daemon, "_windows_pid_alive", lambda _pid: None)
    monkeypatch.setattr(
        daemon.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not call os.kill")),
    )

    assert daemon._pid_alive(4242) is True


def test_windows_terminate_pid_uses_identity_scoped_taskkill(monkeypatch):
    import nz_coder.http_service.daemon as daemon

    alive = True
    calls = []

    def run(argv, **kwargs):
        nonlocal alive
        calls.append((argv, kwargs))
        alive = False
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(daemon, "_pid_alive", lambda _pid: alive)

    daemon._terminate_pid(4242, timeout=0.1, os_name="nt", runner=run)

    assert calls[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert calls[0][1]["shell"] is False


def test_daemon_private_writer_hardens_directory_and_final_file(tmp_path, monkeypatch):
    """Removing either final-path hardening call would leave Windows inheritance."""
    import nz_coder.http_service.daemon as daemon

    hardened = []
    monkeypatch.setattr(
        daemon,
        "harden_private_path",
        lambda path: hardened.append(Path(path)),
    )
    target = tmp_path / "daemon" / "token"

    daemon._atomic_private_text(target, "secret\n")

    assert target.read_text(encoding="utf-8") == "secret\n"
    assert target.parent in hardened
    assert target in hardened


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_daemon_status_rejects_invalid_timeout_before_state_access(tmp_path, timeout):
    with pytest.raises(ValueError, match="status timeout"):
        daemon_status(state_root=tmp_path, timeout=timeout)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_daemon_start_rejects_invalid_startup_timeout_before_spawn(tmp_path, timeout):
    with pytest.raises(ValueError, match="startup timeout"):
        start_daemon(state_root=tmp_path, port=0, startup_timeout=timeout)
    assert not tmp_path.exists() or not any(tmp_path.rglob("state.json"))


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_daemon_stop_rejects_invalid_timeout_before_state_access(tmp_path, timeout):
    with pytest.raises(ValueError, match="stop timeout"):
        stop_daemon(state_root=tmp_path, timeout=timeout)


def test_daemon_state_rejects_nonstandard_numbers_and_preserves_previous(tmp_path):
    from nz_coder.http_service import daemon

    target = tmp_path / "state.json"
    target.write_text('{"pid":NaN}', encoding="utf-8")
    assert daemon._load_state(target) == {}

    daemon._atomic_state(target, {"pid": 42})
    with pytest.raises(ValueError):
        daemon._atomic_state(target, {"pid": float("nan")})
    assert json.loads(target.read_text(encoding="utf-8")) == {"pid": 42}


def test_daemon_start_status_stop_owns_pid_and_private_token(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state = start_daemon(
        state_root=state_root,
        port=0,
        workspaces=[str(workspace)],
        startup_timeout=20,
    )
    try:
        assert state["running"] is True
        assert state["endpoint"].startswith("http://127.0.0.1:")
        assert state["pid"] > 0
        assert state["nonce"]
        assert daemon_status(state_root=state_root)["reason"] == "ready"
        second = start_daemon(state_root=state_root, port=0)
        assert second["already_running"] is True
        assert second["pid"] == state["pid"]
        assert second["nonce"] == state["nonce"]
        token_path = Path(state["token_path"])
        assert token_path.exists()
        if os.name != "nt":
            assert token_path.stat().st_mode & 0o077 == 0
        else:
            from nz_coder.foundation.private_paths import inspect_private_path

            assert inspect_private_path(token_path).hardened is True
            assert inspect_private_path(token_path.parent).hardened is True
        log_text = Path(state["log_path"]).read_text(encoding="utf-8")
        assert state["nonce"] not in log_text
    finally:
        result = stop_daemon(state_root=state_root)
        assert result["stopped"] is True
    assert daemon_status(state_root=state_root)["running"] is False
    assert not Path(state["token_path"]).exists()
    assert not Path(state["state"]).exists()


def test_daemon_start_accepts_option_like_nonce(tmp_path: Path, monkeypatch):
    """A generated nonce beginning with '-' must remain one argv value."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    values = iter([
        "-nonce-that-used-to-confuse-argparse",
        "token-long-enough-for-authentication",
    ])
    monkeypatch.setattr(
        "nz_coder.http_service.daemon.secrets.token_urlsafe",
        lambda _size: next(values),
    )

    state = start_daemon(
        state_root=state_root,
        port=0,
        workspaces=[str(workspace)],
        startup_timeout=20,
    )
    try:
        assert state["running"] is True
        assert state["nonce"] == "-nonce-that-used-to-confuse-argparse"
    finally:
        stop_daemon(state_root=state_root)


def test_daemon_restart_preserves_workspace_sessions_and_rotates_token(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    completed_id = "daemon-completed"
    with scoped_workdir(workspace):
        save_session(
            [
                {"role": "user", "content": "done"},
                {"role": "assistant", "content": "complete"},
            ],
            mode="default",
            session_id=completed_id,
            activate=False,
            run_status="completed",
            require_aliases=False,
            title="Completed survivor",
        )
    state = start_daemon(
        state_root=state_root,
        port=0,
        workspaces=[str(workspace)],
        startup_timeout=20,
    )
    paths = daemon_paths(state_root=state_root)
    old_token = paths.token.read_text(encoding="utf-8").strip()
    old_client = NZCoderClient(state["endpoint"], old_token, timeout=2)
    session = old_client.create_session("default")
    old_client.rename_session(session["id"], "Restart survivor")
    output = StringIO()
    try:
        assert daemon_main([
            "restart",
            "--state-root", str(state_root),
            "--startup-timeout", "20",
        ], output=output) == 0
        restarted = daemon_status(state_root=state_root)
        assert restarted["running"] is True
        assert restarted["workspaces"] == [str(workspace)]
        new_token = paths.token.read_text(encoding="utf-8").strip()
        assert new_token != old_token
        new_client = NZCoderClient(restarted["endpoint"], new_token, timeout=2)
        restored = next(
            item for item in new_client.list_sessions() if item["id"] == session["id"]
        )
        assert restored["title"] == "Restart survivor"
        assert restored["status"] == "idle"
        completed = next(
            item for item in new_client.list_sessions() if item["id"] == completed_id
        )
        assert completed["title"] == "Completed survivor"
        assert completed["status"] == "completed"
        with pytest.raises(NZCoderHTTPError) as stale:
            old_client.list_sessions()
        assert stale.value.status == 401
    finally:
        stop_daemon(state_root=state_root)


def test_daemon_start_marks_unsettled_persisted_run_interrupted(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    session_id = "daemon-interrupted"
    with scoped_workdir(workspace):
        path = save_session(
            [{"role": "user", "content": "accepted before crash"}],
            mode="default",
            session_id=session_id,
            activate=False,
            run_status="running",
            require_aliases=False,
            title="Interrupted run",
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_status"] == "running"

    state = start_daemon(
        state_root=state_root,
        port=0,
        workspaces=[str(workspace)],
        startup_timeout=20,
    )
    try:
        token = Path(state["token_path"]).read_text(encoding="utf-8").strip()
        client = NZCoderClient(state["endpoint"], token, timeout=2)
        restored = client.get_session(session_id)
        assert restored["status"] == "interrupted"
        assert restored["runtime_status"] == "interrupted"
        assert restored["running"] is False
        assert client.messages(session_id) == [
            {"role": "user", "content": "accepted before crash"}
        ]
    finally:
        stop_daemon(state_root=state_root)


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL crash recovery is POSIX-specific")
def test_daemon_forced_termination_restores_active_marker_as_interrupted(tmp_path: Path):
    """A real daemon crash must not leave a durable Session claiming RUNNING."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state = start_daemon(
        state_root=state_root,
        port=0,
        workspaces=[str(workspace)],
        startup_timeout=20,
    )
    restarted = None
    try:
        token = Path(state["token_path"]).read_text(encoding="utf-8").strip()
        client = NZCoderClient(state["endpoint"], token, timeout=2)
        workspace_id = next(
            item["id"]
            for item in client.list_workspaces()
            if Path(item["path"]) == workspace
        )
        session = client.create_session("default", workspace_id)
        with scoped_workdir(workspace):
            save_session(
                [{"role": "user", "content": "accepted before daemon crash"}],
                mode="default",
                session_id=session["id"],
                activate=False,
                run_status="running",
                require_aliases=False,
                title="Crash recovery",
            )

        live = daemon_status(state_root=state_root)
        assert live["running"] is True
        assert live["pid"] == state["pid"]
        os.kill(state["pid"], signal.SIGKILL)
        try:
            os.waitpid(state["pid"], 0)
        except ChildProcessError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            crashed = daemon_status(state_root=state_root)
            if not crashed.get("running"):
                break
            time.sleep(0.05)
        assert crashed["running"] is False

        restarted = start_daemon(
            state_root=state_root,
            port=0,
            workspaces=[str(workspace)],
            startup_timeout=20,
        )
        assert restarted["pid"] != state["pid"]
        new_token = Path(restarted["token_path"]).read_text(encoding="utf-8").strip()
        restored = NZCoderClient(restarted["endpoint"], new_token, timeout=2).get_session(
            session["id"]
        )
        assert restored["status"] == "interrupted"
        assert restored["runtime_status"] == "interrupted"
        assert restored["running"] is False
    finally:
        stop_daemon(state_root=state_root)
