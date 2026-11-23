"""准备命令的真实输出、退出门槛和外部I/O故障。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace

import pytest

from tests.fixtures.process_lifecycle import make_preparation_scope, wait_until


@pytest.fixture
def git_program(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "git"
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\nimport sys\nexec(sys.argv[1])\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ.get("PATH", ""))
    return executable


def _call(scope, workspace, script, *, timeout=2):
    result = {}

    def run():
        try:
            result["value"] = scope.run(("git", script,), cwd=workspace, timeout=timeout)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_preparation_keeps_stdout_and_stderr_separate(tmp_path, git_program):
    scope = make_preparation_scope(tmp_path)
    try:
        result = scope.run(("git", "import os;os.write(1,b'out\\r\\n');os.write(2,b'err\\r\\n')",), cwd=tmp_path, timeout=2)
        assert (result.returncode, result.stdout, result.stderr) == (0, "out\n", "err\n")
        result = scope.run(("git", "print('next')",), cwd=tmp_path, timeout=2)
        assert result.stdout == "next\n"
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_task_interruption_returns_before_native_cleanup(tmp_path, git_program, monkeypatch):
    from nz_coder.runtime.process import preparation
    allow = threading.Event()
    original = preparation.terminate_process_tree
    monkeypatch.setattr(preparation, "terminate_process_tree", lambda *a, **k: original(*a, **k) if allow.is_set() else False)
    cancelled = threading.Event()
    scope = make_preparation_scope(tmp_path, cancel_event=cancelled)
    caller, result = _call(scope, tmp_path, "import time;time.sleep(30)")
    try:
        wait_until(lambda: scope._record is not None and scope._record.process is not None)
        owner = scope._record.thread
        cancelled.set()
        caller.join(.5)
        assert not caller.is_alive()
        assert isinstance(result["error"], preparation.PreparationInterrupted)
        assert result["error"].reason == "cancelled"
        assert scope.close(deadline=time.monotonic() + .02) is False
        assert owner.is_alive()
    finally:
        allow.set()
        assert scope.close(deadline=time.monotonic() + 2)
        caller.join(2)
    assert not owner.is_alive()


@pytest.mark.parametrize("total", [False, True])
def test_local_timeout_waits_for_retirement_but_task_deadline_interrupts(tmp_path, git_program, total):
    from nz_coder.runtime.process.preparation import PreparationInterrupted
    scope = make_preparation_scope(tmp_path, timeout=.08 if total else 3)
    try:
        with pytest.raises(PreparationInterrupted if total else subprocess.TimeoutExpired) as caught:
            scope.run(("git", "import time;time.sleep(30)",), cwd=tmp_path, timeout=2 if total else .05)
        if total:
            assert caught.value.reason == "timeout"
        else:
            assert scope._record is None
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("condition", ["system-error", "recovered", "job-draining"])
def test_preparation_timeout_explains_only_unconfirmed_group_cleanup(tmp_path, monkeypatch, condition):
    from concurrent.futures import ThreadPoolExecutor
    from nz_coder.runtime.process import preparation

    scope = make_preparation_scope(tmp_path)
    entered, release = threading.Event(), threading.Event()
    launch = preparation.start_owned_pipe_command
    terminate = preparation.terminate_process_tree
    native_job = None

    def start(*args, **kwargs):
        nonlocal native_job
        process, native_job = launch(*args, **kwargs)
        # POSIX 测试机也能注入 Windows 作业正常等待最后一个成员的返回值。
        return process, object() if condition == "job-draining" else native_job

    def sweep(process, *, force, windows_job):
        if not release.is_set():
            entered.set()
            if condition == "job-draining":
                return False
            raise OSError("test-only cleanup detail")
        return terminate(process, force=force, windows_job=native_job)

    monkeypatch.setattr(preparation, "start_owned_pipe_command", start)
    monkeypatch.setattr(preparation, "terminate_process_tree", sweep)
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(scope.run, (sys.executable, "-c", "print(7)"), cwd=tmp_path, timeout=4)
        try:
            assert entered.wait(2)
            record = scope._record
            if condition != "job-draining":
                wait_until(lambda: record.cleanup_unconfirmed)
            if condition == "recovered":
                release.set()
                assert result.result(timeout=2).stdout == "7\n"
                assert not record.cleanup_unconfirmed
            else:
                scope._deadline = time.monotonic()
                with pytest.raises(preparation.PreparationInterrupted) as stopped:
                    result.result(timeout=2)
                assert stopped.value.reason == "timeout"
                assert ("process group cleanup" in str(stopped.value)) is (condition == "system-error")
                assert "test-only" not in str(stopped.value)
                assert scope._record is record and record.thread.is_alive()
        finally:
            release.set()
            assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("fault", ["construct", "start_before", "start_after", "reader_construct", "reader_start_after"])
def test_partial_start_and_reader_failure_keep_original_owner(tmp_path, git_program, monkeypatch, fault):
    from nz_coder.runtime.process import preparation
    original = preparation.threading.Thread
    threads = []

    def build(*args, **kwargs):
        index = len(threads)
        if (fault == "construct" and index == 0) or (fault == "reader_construct" and index == 1):
            raise RuntimeError("controlled constructor failure")
        thread = original(*args, **kwargs)
        threads.append(thread)
        start = thread.start

        def broken_start():
            if fault == "start_before" and index == 0:
                raise RuntimeError("controlled start failure")
            start()
            if (fault == "start_after" and index == 0) or (fault == "reader_start_after" and index == 1):
                raise RuntimeError("controlled partial start")
        thread.start = broken_start
        return thread

    scope = make_preparation_scope(tmp_path)
    monkeypatch.setattr(preparation.threading, "Thread", build)
    try:
        with pytest.raises(Exception):
            scope.run(("git", "import time;time.sleep(30)",), cwd=tmp_path, timeout=.3)
    finally:
        assert scope.close(deadline=time.monotonic() + 2)
    assert all(not t.is_alive() for t in threads)


@pytest.mark.parametrize("fault", ["poll", "wait", "group", "reader", "close", "owner_return"])
def test_wait_group_signal_reader_and_close_faults_do_not_retire(tmp_path, git_program, monkeypatch, fault):
    from nz_coder.runtime.process import preparation
    release = threading.Event()
    entered = threading.Event()
    real_popen = preparation.subprocess.Popen
    real_terminate = preparation.terminate_process_tree
    owners = []

    class Stream:
        def __init__(self, wrapped):
            self.wrapped = wrapped
        def read(self, size):
            if fault == "reader":
                entered.set()
                release.wait()
            return self.wrapped.read(size)
        def close(self):
            if fault == "close" and not release.is_set():
                entered.set()
                raise OSError("close unavailable")
            return self.wrapped.close()

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        for method in ("poll", "wait"):
            original = getattr(process, method)
            def call(*a, _original=original, _method=method, **k):
                owners.append(threading.get_ident())
                if fault == _method and not release.is_set():
                    entered.set()
                    raise OSError("controlled native failure")
                return _original(*a, **k)
            setattr(process, method, call)
        process.stdout = Stream(process.stdout)
        process.stderr = Stream(process.stderr)
        return process

    def terminate(*args, **kwargs):
        owners.append(threading.get_ident())
        if fault == "group" and not release.is_set():
            entered.set()
            return False
        return real_terminate(*args, **kwargs)

    monkeypatch.setattr(preparation.subprocess, "Popen", popen)
    monkeypatch.setattr(preparation, "terminate_process_tree", terminate)
    if fault == "owner_return":
        execute = preparation.PreparationScope._execute
        def blocked(scope, record):
            execute(scope, record)
            entered.set()
            release.wait()
        monkeypatch.setattr(preparation.PreparationScope, "_execute", blocked)
    scope = make_preparation_scope(tmp_path)
    caller, result = _call(scope, tmp_path, "print('done')")
    try:
        assert entered.wait(1)
        assert scope.close(deadline=time.monotonic() + .02) is False
        assert scope._record.thread is not None
    finally:
        release.set()
        assert scope.close(deadline=time.monotonic() + 2)
        caller.join(2)
    assert not caller.is_alive()
    assert len(set(owners)) == 1


def test_flood_limit_is_shared_and_never_returns_truncated_success(tmp_path, git_program):
    from nz_coder.runtime.core.run_settings import RunSettings
    from nz_coder.runtime.process.preparation import PreparationFailure
    settings = replace(RunSettings.from_legacy_globals(), bash_output_hard_limit_bytes=16384, process_kill_grace=.02)
    scope = make_preparation_scope(tmp_path, settings=settings)
    try:
        with pytest.raises(PreparationFailure) as caught:
            scope.run(("git", "import os;os.write(1,b'a'*10240);os.write(2,b'b'*10240)",), cwd=tmp_path, timeout=2)
        assert caught.value.public_error.code == "preparation_output_limit"
        assert scope._record is None
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("invalid", [False, True])
def test_default_encoding_and_decode_failure_match_text_mode(tmp_path, git_program, monkeypatch, invalid):
    from nz_coder.runtime.process import preparation
    monkeypatch.setattr(preparation, "_default_encoding", lambda: "ascii" if invalid else "latin-1")
    scope = make_preparation_scope(tmp_path)
    try:
        if invalid:
            with pytest.raises(UnicodeDecodeError):
                scope.run(("git", "import os;os.write(1,b'\\xff')",), cwd=tmp_path, timeout=2)
        else:
            result = scope.run(("git", "import os;os.write(1,b'caf\\xe9\\r\\n')",), cwd=tmp_path, timeout=2)
            assert result.stdout == "café\n"
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_scope_identity_stop_and_seal_gate_admission(tmp_path, git_program):
    from nz_coder.runtime.process.preparation import PreparationFailure, PreparationInterrupted
    scope = make_preparation_scope(tmp_path)
    try:
        with pytest.raises(PreparationFailure):
            scope.run(("git", "print('wrong')",), cwd=tmp_path.parent, timeout=2)
        scope.seal()
        with pytest.raises(PreparationFailure):
            scope.run(("git", "print('late')",), cwd=tmp_path, timeout=2)
        scope.request_stop()
        with pytest.raises(PreparationInterrupted):
            scope.checkpoint()
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_parent_directory_replacement_is_rejected(tmp_path, git_program):
    from nz_coder.runtime.process.preparation import PreparationFailure
    scope = make_preparation_scope(tmp_path)
    old = tmp_path.with_name(tmp_path.name + "-old")
    tmp_path.rename(old)
    tmp_path.mkdir()
    try:
        with pytest.raises(PreparationFailure):
            scope.checkpoint()
    finally:
        assert scope.close(deadline=time.monotonic() + 2)
        tmp_path.rmdir()
        old.rename(tmp_path)


def test_target_recording_rejects_other_child_and_directory_rebind(tmp_path, git_program):
    from nz_coder.runtime.process.preparation import PreparationFailure
    from nz_coder.runtime.worktree import Worktree, WorktreeManager
    saved = []
    scope = make_preparation_scope(tmp_path, on_target=saved.append)
    managed = WorktreeManager(tmp_path).worktree_dir
    target = managed / "child"
    worktree = Worktree("child", str(target), "subagent-child", "HEAD", "")
    try:
        with pytest.raises(PreparationFailure):
            scope.record_target(Worktree("other", str(managed / "other"), "", "HEAD", ""))
        scope.record_target(worktree)
        assert saved[0].path == str(target)
        assert not target.exists()
        target.mkdir(parents=True)
        scope.checkpoint()
        target.rename(managed / "old-child")
        target.mkdir()
        with pytest.raises(PreparationFailure):
            scope.checkpoint()
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_parallel_command_is_rejected_until_original_retires(tmp_path, git_program):
    from nz_coder.runtime.process.preparation import PreparationFailure
    scope = make_preparation_scope(tmp_path)
    caller, result = _call(scope, tmp_path, "import time;time.sleep(30)")
    try:
        wait_until(lambda: scope._record is not None and scope._record.process is not None)
        process = scope._record.process
        with pytest.raises(PreparationFailure):
            scope.run(("git", "print('second')",), cwd=tmp_path, timeout=2)
        assert scope._record.process is process
    finally:
        assert scope.close(deadline=time.monotonic() + 2)
        caller.join(2)
