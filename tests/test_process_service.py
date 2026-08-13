from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import pytest

from nz_coder.runtime.process_service import (
    ProcessOwnershipError,
    ProcessService,
    ProcessStatus,
    close_workspace_process_service,
    dispose_session_processes,
    workspace_process_service,
)


def _python(script: Path, *args: str) -> str:
    argv = [sys.executable, "-u", str(script), *args]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _wait_status(
    service: ProcessService,
    process_id: str,
    expected: set[str],
    *,
    session: str = "session-a",
    timeout: float = 5.0,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.get(process_id, owner_session_id=session).status
        if status in expected:
            return status
        time.sleep(0.02)
    raise AssertionError(f"process did not reach {expected}")


def test_process_service_repl_supports_incremental_write_and_cursor_reads(tmp_path):
    script = tmp_path / "repl.py"
    script.write_text(
        "import sys\n"
        "print('READY', flush=True)\n"
        "for line in sys.stdin:\n"
        "    value = line.strip()\n"
        "    if value == 'quit': break\n"
        "    print(eval(value, {'__builtins__': {}}), flush=True)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=True,
        )
        first = service.read(
            handle.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
        )
        assert "READY" in first.output

        service.write(handle.process_id, "1 + 1\n", owner_session_id="session-a")
        second = service.read(
            handle.process_id,
            owner_session_id="session-a",
            cursor=first.next_cursor,
            wait_seconds=2,
        )
        assert "2" in second.output
        assert "READY" not in second.output

        service.write(handle.process_id, "6 * 7\n", owner_session_id="session-a")
        third = service.read(
            handle.process_id,
            owner_session_id="session-a",
            cursor=second.next_cursor,
            wait_seconds=2,
        )
        assert "42" in third.output
        assert third.next_cursor > second.next_cursor
        killed = service.kill(handle.process_id, owner_session_id="session-a")
        assert killed.status == ProcessStatus.KILLED.value
    finally:
        service.close()
    assert not service.list(active_only=True)


def test_process_read_cancellation_stops_wait_without_killing_process(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(
        "import time\nprint('READY', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    handle = service.start(
        _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
    )
    first = service.read(
        handle.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
    )
    cancel = threading.Event()
    cancel.set()
    stopped = service.read(
        handle.process_id,
        owner_session_id="session-a",
        cursor=first.next_cursor,
        wait_seconds=10,
        cancel_event=cancel,
    )
    assert stopped.cancelled is True
    assert service.get(handle.process_id, owner_session_id="session-a").status == "running"
    service.close()


def test_process_service_bounded_buffer_reports_expired_cursor(tmp_path):
    script = tmp_path / "output.py"
    script.write_text("print('x' * 5000, flush=True)\n", encoding="utf-8")
    service = ProcessService(tmp_path, buffer_bytes=1024)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        _wait_status(service, handle.process_id, {"exited"})
        result = service.read(
            handle.process_id,
            owner_session_id="session-a",
            cursor=0,
            max_bytes=4096,
        )
        assert result.truncated_before_cursor is True
        assert result.buffer_start_cursor > 0
        assert len(result.output.encode()) <= 1024
        assert result.next_cursor == result.buffer_end_cursor
    finally:
        service.close()


def test_process_service_crash_status_and_output_are_retained(tmp_path):
    script = tmp_path / "crash.py"
    script.write_text(
        "import sys\nprint('CRASHING', flush=True)\nsys.exit(7)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        assert _wait_status(service, handle.process_id, {"exited"}) == "exited"
        status = service.get(handle.process_id, owner_session_id="session-a")
        output = service.read(handle.process_id, owner_session_id="session-a")
        assert status.exit_code == 7
        assert "CRASHING" in output.output
    finally:
        service.close()


def test_process_service_decodes_configured_legacy_output(tmp_path, monkeypatch):
    from nz_coder import config

    payload = "中文错误".encode("gbk")
    script = tmp_path / "legacy.py"
    script.write_text(
        f"import os\nos.write(1, {payload!r})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROCESS_OUTPUT_ENCODING", "cp936", raising=False)
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        assert _wait_status(service, handle.process_id, {"exited"}) == "exited"
        result = service.read(handle.process_id, owner_session_id="session-a")
        assert result.output == "中文错误"
    finally:
        service.close()


def test_windows_conpty_unavailable_uses_actionable_pipe_fallback(tmp_path, monkeypatch):
    class PipeFallback:
        pid = 4321
        tty = False
        os_name = "nt"

        def read_bytes(self, _size):
            return b""

        def write_bytes(self, _data):
            return None

        def resize(self, **_kwargs):
            raise RuntimeError("unavailable")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate_tree(self, *, grace_seconds):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        "nz_coder.runtime.process_service.create_process_backend",
        lambda *_args, **_kwargs: PipeFallback(),
    )
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            "python server.py",
            cwd=tmp_path,
            owner_session_id="session-a",
            tty=True,
        )
        result = service.read(handle.process_id, owner_session_id="session-a")

        assert handle.tty is False
        assert "Interactive terminal features unavailable" in result.output
        assert "pip install pywinpty" in result.output
    finally:
        service.close()


def test_multiple_processes_are_isolated_by_id_and_session(tmp_path):
    script = tmp_path / "labeled.py"
    script.write_text(
        "import sys, time\nprint(sys.argv[1], flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        first = service.start(
            _python(script, "SERVICE_A"), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        second = service.start(
            _python(script, "SERVICE_B"), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        first_output = service.read(
            first.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
        )
        second_output = service.read(
            second.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
        )
        assert "SERVICE_A" in first_output.output and "SERVICE_B" not in first_output.output
        assert "SERVICE_B" in second_output.output and "SERVICE_A" not in second_output.output
        with pytest.raises(ProcessOwnershipError):
            service.get(first.process_id, owner_session_id="session-b")
        assert len(service.list(owner_session_id="session-a", active_only=True)) == 2
    finally:
        service.close()
    assert not service.list(active_only=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY resize contract")
def test_posix_pty_resize_is_supported(tmp_path):
    script = tmp_path / "wait.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=True,
        )
        resized = service.resize(
            handle.process_id, rows=40, cols=120, owner_session_id="session-a",
        )
        assert resized.tty is True
    finally:
        service.close()


def test_workspace_registry_shares_service_and_session_cleanup_kills_only_owner(tmp_path):
    script = tmp_path / "wait.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    first = workspace_process_service(tmp_path)
    second = workspace_process_service(tmp_path / ".")
    assert first is second
    a = first.start(
        _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
    )
    b = first.start(
        _python(script), cwd=tmp_path, owner_session_id="session-b", tty=False,
    )
    try:
        assert dispose_session_processes(tmp_path, "session-a") == 1
        assert first.get(a.process_id, owner_session_id="session-a").status == "cancelled"
        assert first.get(b.process_id, owner_session_id="session-b").status == "running"
    finally:
        assert close_workspace_process_service(tmp_path) == 1
    assert not first.list(active_only=True)


def test_process_events_do_not_include_output_contents(tmp_path):
    from nz_coder.session_events import SessionEventBus

    script = tmp_path / "events.py"
    script.write_text("print('secret-value', flush=True)\n", encoding="utf-8")
    bus = SessionEventBus(session_id="session-a")
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
            event_bus=bus,
        )
        _wait_status(service, handle.process_id, {"exited"})
        events = [event.to_dict() for event in bus.recent(20)]
        assert any(event["type"] == "process.started" for event in events)
        assert any(event["type"] == "process.output" for event in events)
        assert any(event["type"] == "process.exited" for event in events)
        assert "secret-value" not in json.dumps(events)
    finally:
        service.close()
        bus.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_service_close_kills_spawned_descendant_process_group(tmp_path):
    parent = tmp_path / "parent.py"
    pid_file = tmp_path / "child.pid"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "print('CHILD_READY', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    handle = service.start(
        _python(parent), cwd=tmp_path, owner_session_id="session-a", tty=False,
    )
    output = service.read(
        handle.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
    )
    assert "CHILD_READY" in output.output
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    service.close()

    deadline = time.monotonic() + 2
    state = ""
    while time.monotonic() < deadline:
        status_path = Path(f"/proc/{child_pid}/status")
        if not status_path.exists():
            break
        try:
            status_text = status_path.read_text(errors="replace")
        except FileNotFoundError:
            break
        state = next(
            (
                line for line in status_text.splitlines()
                if line.startswith("State:")
            ),
            "",
        )
        if "Z (zombie)" in state:
            break
        time.sleep(0.02)
    assert not Path(f"/proc/{child_pid}").exists() or "Z (zombie)" in state


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_naturally_exited_shell_sweeps_daemonized_child(tmp_path):
    pid_file = tmp_path / "daemon.pid"
    command = (
        f"{shlex.quote(sys.executable)} -c \"import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path('daemon.pid').write_text(str(child.pid))\""
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        handle = service.start(
            command, cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        assert _wait_status(service, handle.process_id, {"exited"}) == "exited"
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        state = ""
        while time.monotonic() < deadline:
            status_path = Path(f"/proc/{child_pid}/status")
            if not status_path.exists():
                break
            try:
                status_text = status_path.read_text(errors="replace")
            except FileNotFoundError:
                break
            state = next(
                (line for line in status_text.splitlines() if line.startswith("State:")),
                "",
            )
            if "Z (zombie)" in state:
                break
            time.sleep(0.02)
        assert not Path(f"/proc/{child_pid}").exists() or "Z (zombie)" in state
    finally:
        service.close()
