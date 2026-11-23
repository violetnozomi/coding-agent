"""Native Windows Job Object crash and descendant-cleanup acceptance tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
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
    import _winapi

    try:
        handle = _winapi.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    except OSError as exc:
        if exc.winerror == 87:  # 已经回收的 PID 没有可打开的进程对象。
            return False
        raise
    try:
        return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
    finally:
        _winapi.CloseHandle(handle)


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


@pytest.fixture(params=["preparation", "retrieval"])
def owned_pipe_runner(request, tmp_path):
    from tests.fixtures.process_lifecycle import make_auxiliary_runner, make_preparation_scope

    if request.param == "preparation":
        owner = make_preparation_scope(tmp_path, timeout=20)
        run = owner.run
    else:
        owner = make_auxiliary_runner(tmp_path, timeout=20)
        run = owner.capture
    try:
        yield owner, run
    finally:
        assert owner.close(deadline=time.monotonic() + 8)


def test_owned_pipe_quick_commands_keep_exit_code_and_both_streams(owned_pipe_runner, tmp_path):
    _, run = owned_pipe_runner
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(int(sys.argv[1]))"
    for code in (0, 7):
        result = run((sys.executable, "-c", script, str(code)), cwd=tmp_path, timeout=8)
        assert (result.returncode, result.stdout, result.stderr) == (code, "out\n", "err\n")


@pytest.mark.parametrize("action", ["root-exits", "cancel"])
def test_owned_pipe_retires_descendant_holding_output(owned_pipe_runner, tmp_path, action):
    from nz_coder.foundation.process_runner import ProcessInterrupted
    from nz_coder.runtime.process.preparation import PreparationInterrupted
    from tests.fixtures.process_lifecycle import wait_until

    owner, run = owned_pipe_runner
    script = tmp_path / "family.py"
    script.write_text(textwrap.dedent("""\
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time

        role = sys.argv[1]
        if role == "root":
            subprocess.Popen([sys.executable, __file__, "child"], stdin=subprocess.DEVNULL,
                             stdout=sys.stdout, stderr=sys.stderr)
        else:
            print("child has the pipe", flush=True)
            marker = Path("pids.tmp")
            marker.write_text(f"{os.getppid()} {os.getpid()}")
            marker.replace("pids")
        deadline = time.monotonic() + 25
        while not Path("finish-" + role).exists():
            if time.monotonic() >= deadline:
                raise SystemExit(91)
            time.sleep(0.025)
        """), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(run, (sys.executable, str(script), "root"), cwd=tmp_path, timeout=15)
        try:
            pids = tmp_path / "pids"
            wait_until(lambda: pids.exists() or result.done(), timeout=8)
            if not pids.exists():
                result.result()
                pytest.fail("the command returned before its descendant opened stdout")
            root, child = map(int, pids.read_text().split())
            assert _pid_alive(root) and _pid_alive(child)
            if action == "cancel":
                owner.request_stop()
                with pytest.raises((PreparationInterrupted, ProcessInterrupted)) as stopped:
                    result.result(timeout=8)
                assert stopped.value.reason == "cancelled"
                assert owner.close(deadline=time.monotonic() + 8)
            else:
                (tmp_path / "finish-root").touch()
                completed = result.result(timeout=8)
                assert (completed.returncode, completed.stdout) == (0, "child has the pipe\n")
            _assert_pid_dead(root)
            _assert_pid_dead(child)
        finally:
            # 即使被测实现未能回收后代，也让测试自己创建的两个程序正常退出。
            (tmp_path / "finish-root").touch()
            (tmp_path / "finish-child").touch()


def test_owned_pipe_rejected_job_never_executes_user_code(owned_pipe_runner, tmp_path, monkeypatch):
    from nz_coder.runtime.process import windows_job_control as native

    _, run = owned_pipe_runner
    attributes = native._startup_attributes
    attempted = []

    def invalid_job(kernel, job_handle, streams):
        attempted.append(job_handle)
        return attributes(kernel, 0, streams)

    monkeypatch.setattr(native, "_startup_attributes", invalid_job)
    script = "from pathlib import Path; Path('user-code-ran').touch()"
    with pytest.raises(OSError):
        run((sys.executable, "-c", script), cwd=tmp_path, timeout=8)
    assert len(attempted) == 1
    assert not (tmp_path / "user-code-ran").exists()


def test_owned_pipe_failed_resume_retires_suspended_process(tmp_path, monkeypatch):
    import _winapi
    from nz_coder.runtime.process import process_backends, windows_job_control

    observer = []

    def fail_resume(kernel, info):
        observer.append(_winapi.OpenProcess(0x00100000, False, info.pid))
        raise OSError("test resume failure")

    monkeypatch.setattr(windows_job_control, "_resume_primary_thread", fail_resume)
    try:
        with pytest.raises(OSError, match="test resume failure"):
            process_backends.start_owned_pipe_command(
                (sys.executable, "-c", "from pathlib import Path; Path('user-code-ran').touch()"),
                cwd=tmp_path, env=os.environ,
            )
        handle, = observer
        assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_OBJECT_0
        assert not (tmp_path / "user-code-ran").exists()
    finally:
        for handle in observer:
            _winapi.CloseHandle(handle)


def test_parent_death_at_create_process_return_retires_suspended_child(tmp_path):
    import _winapi

    helper = tmp_path / "parent.py"
    helper.write_text(textwrap.dedent("""\
        import ctypes
        from ctypes import wintypes
        import json
        import os
        from pathlib import Path
        import sys
        import time
        from nz_coder.runtime.process import process_backends as backends
        root = Path(sys.argv[1])
        create_job = backends._WindowsJob.create

        def observe_job(cls):
            job = create_job()
            def after_kernel_create(result, function, args):
                if result:
                    info = args[-1]._obj
                    inside = wintypes.BOOL()
                    member = job.kernel32.IsProcessInJob
                    member.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
                    member.restype = wintypes.BOOL
                    if not member(info.process, job.handle, ctypes.byref(inside)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    marker = root / "pid.tmp"
                    marker.write_text(json.dumps([info.pid, bool(inside.value)]))
                    marker.replace(root / "pid.json")
                    time.sleep(30)
                return result
            job.kernel32.CreateProcessW.errcheck = after_kernel_create
            return job

        backends._WindowsJob.create = classmethod(observe_job)
        backends.start_owned_pipe_command(
            (sys.executable, "-c", "from pathlib import Path; Path('user-code-ran').touch()"),
            cwd=root, env=os.environ,
        )
        """), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    with subprocess.Popen([sys.executable, str(helper), str(tmp_path)], cwd=tmp_path, env=env) as parent:
        child = None
        try:
            marker = tmp_path / "pid.json"
            _wait_file(marker, timeout=12)
            pid, in_our_job = json.loads(marker.read_text())
            child = _winapi.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | TERMINATE
            assert in_our_job and _winapi.WaitForSingleObject(child, 0) == _winapi.WAIT_TIMEOUT
            parent.kill()
            parent.wait(5)
            assert _winapi.WaitForSingleObject(child, 5000) == _winapi.WAIT_OBJECT_0
            assert not (tmp_path / "user-code-ran").exists()
        finally:
            if parent.poll() is None:
                parent.kill()
            if child:
                # 回归失败时也收回测试启动的同一进程。句柄不受进程号复用影响。
                if _winapi.WaitForSingleObject(child, 0) == _winapi.WAIT_TIMEOUT:
                    _winapi.TerminateProcess(child, 1)
                    assert _winapi.WaitForSingleObject(child, 5000) == _winapi.WAIT_OBJECT_0
                _winapi.CloseHandle(child)


def test_concurrent_owned_pipe_commands_keep_their_own_streams_and_environment(tmp_path, monkeypatch):
    import threading
    from nz_coder.runtime.process import windows_job_control
    from tests.fixtures.process_lifecycle import make_preparation_scope

    root = tmp_path / "含 空格"
    root.mkdir()
    quick_scope = make_preparation_scope(root, child="quick", timeout=30)
    slow_scope = make_preparation_scope(root, child="slow", timeout=30)
    barrier = threading.Barrier(2)
    create = windows_job_control._create_suspended_process

    def overlapping_create(*args):
        barrier.wait(8)
        create(*args)
        barrier.wait(8)  # 两套可继承句柄同时打开，确保真的测到交叉继承的风险。

    monkeypatch.setattr(windows_job_control, "_create_suspended_process", overlapping_create)
    gate = root / "finish"
    values = ("", '引号"空 格', "trailing slash " + chr(92), "a=b")
    quick_code = ("import os, sys, json; print(json.dumps([os.getcwd(), os.getenv('NZ_VALUE'), sys.argv[1:]]));"
                  "print('quick stderr', file=sys.stderr)")
    slow_code = ("from pathlib import Path\nimport time\n"
                 "while not Path('finish').exists(): time.sleep(.025)\nprint('slow stdout')")
    env = dict(os.environ, NZ_VALUE="overridden", nz_value="桃子")
    reference = subprocess.run((sys.executable, "-c", quick_code, *values), cwd=root,
                               env=env, capture_output=True, text=True, check=True, timeout=8)
    assert json.loads(reference.stdout) == [str(root), "桃子", list(values)]
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            slow = workers.submit(slow_scope.run, (sys.executable, "-c", slow_code), cwd=root, timeout=18)
            quick = workers.submit(quick_scope.run, (sys.executable, "-c", quick_code, *values),
                                   cwd=root, environment=env, timeout=14)
            try:
                result = quick.result(timeout=10)
                assert result.returncode == 0 and result.stderr == "quick stderr\n"
                assert (result.stdout, result.stderr) == (reference.stdout, reference.stderr)
                assert not slow.done()
            finally:
                gate.touch()
            assert slow.result(timeout=5).stdout == "slow stdout\n"
    finally:
        try:
            assert quick_scope.close(deadline=time.monotonic() + 8)
        finally:
            assert slow_scope.close(deadline=time.monotonic() + 8)
