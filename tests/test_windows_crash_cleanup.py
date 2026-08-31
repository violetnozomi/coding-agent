"""Native Windows Job Object crash and descendant-cleanup acceptance tests."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows jobs")


def _python_sleep_command(seconds: int = 60) -> str:
    return subprocess.list2cmdline([
        sys.executable,
        "-c",
        f"import time; time.sleep({seconds})",
    ])


def _pid_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def _assert_pid_dead(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    assert not _pid_alive(pid), f"owned process {pid} remained alive"


def _wait_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.05)
    assert path.exists(), f"timed out waiting for {path}"


def test_wc1_normal_kill_leaves_zero_owned_processes(tmp_path):
    from nz_coder.runtime.process.process_service import ProcessService

    service = ProcessService(tmp_path, kill_grace_seconds=0.1)
    handle = service.start(
        _python_sleep_command(), cwd=tmp_path, owner_session_id="wc1", tty=False,
    )
    assert handle.pid is not None and _pid_alive(handle.pid)
    service.kill(handle.process_id, owner_session_id="wc1")
    _assert_pid_dead(handle.pid)
    service.close()


def test_wc2_session_cleanup_kills_only_owned_session_then_close_kills_rest(tmp_path):
    from nz_coder.runtime.process.process_service import ProcessService

    service = ProcessService(tmp_path, kill_grace_seconds=0.1)
    first = service.start(
        _python_sleep_command(), cwd=tmp_path, owner_session_id="wc2-a", tty=False,
    )
    second = service.start(
        _python_sleep_command(), cwd=tmp_path, owner_session_id="wc2-b", tty=False,
    )
    assert service.kill_session("wc2-a") == 1
    _assert_pid_dead(int(first.pid))
    assert _pid_alive(int(second.pid))
    service.close()
    _assert_pid_dead(int(second.pid))


def test_wc3_daemon_graceful_shutdown_leaves_zero_daemon_processes(tmp_path):
    from nz_coder.http_service.daemon import start_daemon, stop_daemon

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state = start_daemon(
        state_root=state_root,
        port=0,
        workspaces=[str(workspace)],
        startup_timeout=20,
    )
    pid = int(state["pid"])
    assert _pid_alive(pid)
    result = stop_daemon(state_root=state_root)
    assert result["stopped"] is True
    _assert_pid_dead(pid)


def test_wc4_parent_forced_termination_closes_job_and_child(tmp_path):
    helper = tmp_path / "forced_parent.py"
    pid_file = tmp_path / "owned.pid"
    helper.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "from nz_coder.runtime.process.process_service import ProcessService\n"
        "root=pathlib.Path(sys.argv[1])\n"
        "service=ProcessService(root)\n"
        "command=subprocess.list2cmdline([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "handle=service.start(command, cwd=root, owner_session_id='forced', tty=False)\n"
        "pathlib.Path(sys.argv[2]).write_text(str(handle.pid), encoding='utf-8')\n"
        "time.sleep(0.2)\n"
        "os._exit(17)\n",
        encoding="utf-8",
    )
    # Keep the helper itself outside ProcessService: its abnormal exit must
    # prove JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, not normal service.close().
    source_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((
        str(source_root), environment.get("PYTHONPATH", ""),
    )).rstrip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, str(helper), str(tmp_path), str(pid_file)],
        cwd=tmp_path,
        env=environment,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 17
    _wait_file(pid_file)
    _assert_pid_dead(int(pid_file.read_text(encoding="utf-8")))


def test_wc5_npm_node_child_tree_leaves_zero_orphans(tmp_path):
    from nz_coder.runtime.process.process_service import ProcessService

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    assert npm is not None, "Node/npm must be installed on the Windows RC runner"
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "node parent.js"}}),
        encoding="utf-8",
    )
    (tmp_path / "parent.js").write_text(
        "const fs=require('fs'); const {spawn}=require('child_process');\n"
        "const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});\n"
        "fs.writeFileSync('node-pids.json',JSON.stringify([process.pid,child.pid]));\n"
        "setInterval(()=>{},1000);\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.2)
    handle = service.start(
        f'"{npm}" run dev', cwd=tmp_path, owner_session_id="wc5", tty=False,
    )
    pid_file = tmp_path / "node-pids.json"
    _wait_file(pid_file, timeout=10)
    node_pids = [int(value) for value in json.loads(pid_file.read_text(encoding="utf-8"))]
    try:
        assert all(_pid_alive(pid) for pid in node_pids)
        service.kill(handle.process_id, owner_session_id="wc5")
        for pid in node_pids:
            _assert_pid_dead(pid)
    finally:
        service.close()
