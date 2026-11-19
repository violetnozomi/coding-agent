"""Linux 终端入口的权限快照、选择器和实际进程取消回归。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.completion import DummyCompleter
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style

from nz_coder.interface.fullscreen import FullscreenComposer
from nz_coder.interface.interactions import TerminalInteractionBridge
from nz_coder.runtime.process.workdir import scoped_workdir


async def _until(predicate, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("等待终端行为超时")
        await asyncio.sleep(0.01)


def _composer(pipe):
    return FullscreenComposer(
        transcript_provider=lambda: "Session",
        status_provider=lambda: "offline/model · default",
        sidebar_provider=None, sidebar_mode="hide", completer=DummyCompleter(),
        history=None, style=Style.from_dict({}), mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False, clear_exit_request=lambda: None,
        input=pipe, output=DummyOutput(),
    )


@contextmanager
def _environment(workspace, monkeypatch):
    from nz_coder.runtime.execution.loop import AgentLoop
    from tests.test_loop_fake import FakeClient

    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(workspace.parent / "user.env"))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(workspace.parent / "trust.json"))
    with scoped_workdir(workspace):
        environment = AgentLoop(
            "offline", permission_mode="default", client=FakeClient([]),
            trace_enabled=False, sidecar_verifier=False,
        )
        try:
            yield environment
        finally:
            environment.close()


def test_permission_callback_survives_runs_rebind_and_clear(tmp_path, monkeypatch):
    calls = []
    def first(*args):
        calls.append(("first", args))
        return "once"
    def second(*args):
        calls.append(("second", args))
        return "reject"
    with _environment(tmp_path, monkeypatch) as environment:
        environment.set_interaction_askers(permission_asker=first)
        previous = environment.permissions
        for _ in range(2):
            bundle = environment.prepare_run_control()
            try:
                assert bundle.permissions is not previous
                assert bundle.permissions.ask_user("write_file", {"path": "one"})
                previous = bundle.permissions
            finally:
                environment.retire_run_control(bundle)
        environment.set_interaction_askers(permission_asker=second)
        bundle = environment.prepare_run_control()
        try:
            assert not bundle.permissions.ask_user("write_file", {"path": "two"})
        finally:
            environment.retire_run_control(bundle)
        environment.set_interaction_askers(permission_asker=None)
        bundle = environment.prepare_run_control()
        try:
            fallback = []
            monkeypatch.setattr(bundle.permissions, "_read_permission_answer", lambda summary: fallback.append(summary) or "n")
            assert not bundle.permissions.ask_user("write_file", {"path": "three"})
            assert len(fallback) == 1
        finally:
            environment.retire_run_control(bundle)
    assert [name for name, _ in calls] == ["first", "first", "second"]


def test_terminal_controller_two_runs_use_bound_permission_selector(tmp_path, monkeypatch):
    from nz_coder.interface.session_controller import TerminalSessionController
    from tests.test_loop_fake import FakeMessage, FakeResponse, FakeToolCall

    async def scenario(environment):
        with create_pipe_input() as pipe:
            composer = _composer(pipe)
            renderer = SimpleNamespace(pause=lambda: None, resume=lambda: None)
            bridge = TerminalInteractionBridge(composer, renderer, asyncio.get_running_loop())
            environment.set_interaction_askers(permission_asker=bridge.ask_permission)
            controller = TerminalSessionController(environment)
            history = []
            try:
                for index in range(2):
                    name = f"run-{index}.txt"
                    environment.client.chat.completions.items.extend([
                        FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
                            "write_file", {"path": name, "content": f"run {index}"},
                            call_id=f"write-{index}",
                        )])),
                        FakeResponse(FakeMessage("done")),
                    ])
                    history.append({"role": "user", "content": f"Write {name}."})
                    task = asyncio.create_task(controller.run(history, stream=False))
                    await _until(lambda: composer._dialog_future is not None)
                    pipe.send_text("\r")
                    result = await asyncio.wait_for(task, 5)
                    assert result["status"] == "completed_unverified"
                    assert result["verification"]["review_pending"] is True
                    assert (tmp_path / name).read_text() == f"run {index}"
                    assert composer._dialog_kind == ""
            finally:
                await composer.close_async()
    with _environment(tmp_path, monkeypatch) as environment:
        asyncio.run(scenario(environment))


def test_permission_always_reloads_without_reviving_project_rules(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".nz-coder" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["bash(git *)"]}}))
    with _environment(workspace, monkeypatch) as environment:
        store = WorkspaceTrustStore()
        snapshot = load_config_snapshot(workspace)
        store.trust(workspace, "workspace-control", snapshot.control_fingerprint)
        environment.set_interaction_askers(permission_asker=lambda *_: "always")
        bundle = environment.prepare_run_control()
        try:
            assert bundle.permissions.check("bash", {"command": "git status"})["behavior"] == "allow"
            assert bundle.permissions.ask_user("write_file", {"path": "approved.txt"})
            bundle.permissions.add_allow("bash(python *)")
        finally:
            environment.retire_run_control(bundle)
        assert store.remove(workspace, "workspace-control")
        for changed in (False, True):
            if changed:
                settings.write_text(json.dumps({"permissions": {"allow": ["bash(curl *)"]}}))
            bundle = environment.prepare_run_control()
            try:
                permissions = bundle.permissions
                assert permissions.check("write_file", {"path": "approved.txt"})["behavior"] == "allow"
                assert permissions.check("write_file", {"path": "other.txt"})["behavior"] == "ask"
                for command in ("git status", "python build.py", "curl https://example.invalid"):
                    assert permissions.check("bash", {"command": command})["behavior"] == "ask"
            finally:
                environment.retire_run_control(bundle)


def test_cancelled_selector_releases_overlay_and_keeps_new_owner():
    async def scenario():
        with create_pipe_input() as pipe:
            composer = _composer(pipe)
            try:
                first = asyncio.create_task(composer.select_async(title="old", text="old", values=[("once", "Allow")]))
                await _until(lambda: composer._dialog_future is not None)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                assert composer._dialog_kind == ""
                assert composer._selector is None
                second = asyncio.create_task(composer.select_async(title="new", text="new", values=[("once", "Allow")]))
                await _until(lambda: composer._dialog_future is not None)
                old_future = composer._dialog_future
                newer = asyncio.create_task(composer.select_async(title="newest", text="newest", values=[("always", "Allow")]))
                await _until(lambda: composer._dialog_future is not None and composer._dialog_future is not old_future)
                # 旧 Task 退出时，新的 overlay 已经获得所有权。
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
                await _until(lambda: composer._dialog_future is not None and composer._dialog_future is not old_future)
                pipe.send_text("\r")
                assert await asyncio.wait_for(newer, 2) == "always"
                reader = asyncio.create_task(composer.read_async())
                pipe.send_text("/status\r")
                assert await asyncio.wait_for(reader, 2) == "/status"
            finally:
                await composer.close_async()
    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["\x1b", "\x03"])
def test_local_permission_keys_reject_only_operation(tmp_path, key):
    from nz_coder.interface.direct_shell import execute_direct_shell
    from nz_coder.permissions import PermissionManager

    async def scenario():
        with create_pipe_input() as pipe, scoped_workdir(tmp_path):
            composer = _composer(pipe)
            renderer = SimpleNamespace(pause=lambda: None, resume=lambda: None)
            bridge = TerminalInteractionBridge(composer, renderer, asyncio.get_running_loop())
            cancelled = []
            composer.begin_run()
            composer.set_cancel_run(lambda: cancelled.append(True))
            try:
                task = asyncio.create_task(asyncio.to_thread(
                    execute_direct_shell, "touch forbidden", permissions=PermissionManager("default", asker=bridge.ask_permission),
                ))
                await _until(lambda: composer._dialog_future is not None)
                pipe.send_text(key)
                result = await asyncio.wait_for(task, 3)
                assert result.permission_denied
                assert cancelled == []
                assert not (tmp_path / "forbidden").exists()
            finally:
                composer.end_run()
                await composer.close_async()
    asyncio.run(scenario())


def test_tool_stop_releases_real_permission_selector_and_worker(tmp_path):
    from nz_coder.interface.direct_shell import execute_direct_shell
    from nz_coder.permissions import PermissionManager
    from nz_coder.tools import scoped_tool_cancellation

    async def scenario():
        with create_pipe_input() as pipe, scoped_workdir(tmp_path):
            composer = _composer(pipe)
            resumed = threading.Event()
            renderer = SimpleNamespace(pause=lambda: None, resume=resumed.set)
            bridge = TerminalInteractionBridge(composer, renderer, asyncio.get_running_loop())
            stop = threading.Event()
            worker_done = threading.Event()
            def worker():
                try:
                    return execute_direct_shell("touch forbidden", permissions=PermissionManager("default", asker=bridge.ask_permission))
                finally:
                    worker_done.set()
            try:
                with scoped_tool_cancellation(stop):
                    task = asyncio.create_task(asyncio.to_thread(worker))
                await _until(lambda: composer._dialog_future is not None)
                stop.set()
                await asyncio.sleep(0.25)
                try:
                    assert worker_done.is_set(), "审批 worker 未观察工具停止信号"
                    assert resumed.is_set(), "权限协程尚未真正退出"
                    assert composer._dialog_kind == ""
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert not (tmp_path / "forbidden").exists()
                    reader = asyncio.create_task(composer.read_async())
                    pipe.send_text("/status\r")
                    assert await asyncio.wait_for(reader, 2) == "/status"
                finally:
                    if not task.done():
                        composer._resolve_dialog(None)
                        await task
            finally:
                await composer.close_async()
    asyncio.run(scenario())


def test_bridge_waits_for_permission_coroutine_cleanup():
    from nz_coder.tools import scoped_tool_cancellation

    async def scenario():
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        resumed = threading.Event()
        done = threading.Event()
        class Terminal:
            async def select_async(self, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
        bridge = TerminalInteractionBridge(
            Terminal(), SimpleNamespace(pause=lambda: None, resume=resumed.set),
            asyncio.get_running_loop(),
        )
        stop = threading.Event()
        def worker():
            try:
                return bridge.ask_permission("write_file", {"path": "one"})
            finally:
                done.set()
        with scoped_tool_cancellation(stop):
            task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            await started.wait()
            stop.set()
            await asyncio.wait_for(cleaning.wait(), 1)
            await asyncio.sleep(0.1)
            assert not done.is_set(), "权限协程清理结束前不得释放 worker"
            assert not resumed.is_set()
        finally:
            release.set()
            with pytest.raises(RuntimeError, match="cancelled"):
                await asyncio.wait_for(task, 1)
        assert resumed.is_set()
        assert done.is_set()
    asyncio.run(scenario())


def _alive(pid):
    try:
        os.kill(pid, 0)
        # Linux 容器的 PID 1 可能延迟回收孤儿僵尸；僵尸已经退出。
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="检查 Linux 实际父子 PID")
@pytest.mark.parametrize("cancel_mode", ["keyboard", "repeated_task", "approval"])
def test_cli_direct_shell_cancel_settles_before_next_input(tmp_path, monkeypatch, cancel_mode):
    from nz_coder.interface import cli
    from nz_coder.interface import direct_shell

    async def scenario(environment):
        with create_pipe_input() as pipe:
            composer = _composer(pipe)
            reads = []
            worker_done = threading.Event()
            worker_started = threading.Event()
            captured_stop = []
            worker_results = []
            real_execute = direct_shell.execute_direct_shell
            def observed_execute(*args, **kwargs):
                from nz_coder.tools import current_tool_cancel_event
                captured_stop.append(current_tool_cancel_event())
                worker_started.set()
                try:
                    result = real_execute(*args, **kwargs)
                    worker_results.append(result)
                    return result
                finally:
                    worker_done.set()
            monkeypatch.setattr(direct_shell, "execute_direct_shell", observed_execute)
            class Input:
                fullscreen = composer
                interactive = True
                preferences = SimpleNamespace(tool_details="compact")
                async def read_async(self, **kwargs):
                    reads.append(worker_done.is_set())
                    return await composer.read_async(**kwargs)
                async def select_async(self, **kwargs):
                    return await composer.select_async(**kwargs)
                def has_pending_submission(self):
                    return composer.has_pending_submission()
                def refresh_view(self):
                    pass
            monkeypatch.setattr(cli, "TerminalInput", lambda **kwargs: Input())
            monkeypatch.setattr(cli, "_build_agent", lambda *args, **kwargs: environment)
            monkeypatch.setattr(cli, "record_recent_model", lambda *_: None)
            monkeypatch.setattr(cli, "current_memory_manager", lambda: SimpleNamespace(load_all=lambda: None))
            owner = []
            cli_task = asyncio.create_task(cli._run_cli_impl(owner))
            pids = []
            try:
                await _until(lambda: reads)
                if cancel_mode == "approval":
                    command = "touch forbidden"
                else:
                    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open('child.pid','w').write(str(__import__('os').getpid())); time.sleep(60)"
                    parent = "import os,signal,subprocess,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); open('parent.pid','w').write(str(os.getpid())); time.sleep(60)"
                    command = shlex.join([sys.executable, "-c", parent])
                pipe.send_text("!" + command + "\r")
                await _until(worker_started.is_set)
                await _until(lambda: composer._dialog_future is not None)
                if cancel_mode == "approval":
                    cli_task.cancel()
                else:
                    pipe.send_text("\r")
                    try:
                        await _until(lambda: (tmp_path / "child.pid").exists() and (tmp_path / "parent.pid").exists())
                    except TimeoutError:
                        pytest.fail(f"命令未就绪: {worker_results!r}")
                    pids = [int((tmp_path / name).read_text()) for name in ("parent.pid", "child.pid")]
                    assert all(_alive(pid) for pid in pids)
                    if cancel_mode == "keyboard":
                        pipe.send_text("\x03")
                        await asyncio.sleep(0.03)
                        pipe.send_text("\x03")
                    else:
                        cli_task.cancel()
                        await asyncio.sleep(0.03)
                        cli_task.cancel()
                # 原实现会在这里泄漏 worker，或把 Ctrl+C 当作空闲退出。
                await asyncio.sleep(0.15)
                if captured_stop[0] is not None:
                    assert captured_stop[0].is_set()
                if not worker_done.is_set():
                    assert len(reads) == 1, "工作线程尚活着却恢复了下一输入"
                    assert composer.run_active
                await _until(lambda: len(reads) == 2, timeout=5)
                assert worker_done.is_set()
                assert not any(thread.name == "nz-bash-output" and thread.is_alive() for thread in threading.enumerate())
                assert not any(_alive(pid) for pid in pids)
                assert not composer.run_active
                assert composer._dialog_kind == ""
                assert not (tmp_path / "forbidden").exists()
                pipe.send_text("/status\r")
                await _until(lambda: len(reads) == 3)
                notices = "".join(text for block in composer._notices for _style, text in block)
                assert "Status" in notices
                assert "Command cancelled" in notices
                pipe.send_text("exit\r")
                await asyncio.wait_for(cli_task, 3)
            finally:
                if captured_stop and captured_stop[0] is not None:
                    captured_stop[0].set()
                for name in ("parent.pid", "child.pid"):
                    path = tmp_path / name
                    if path.exists() and _alive(int(path.read_text())):
                        os.kill(int(path.read_text()), signal.SIGKILL)
                if composer._dialog_future is not None:
                    composer._resolve_dialog(None)
                if not cli_task.done():
                    pipe.send_text("exit\r")
                    cli_task.cancel()
                    try:
                        await asyncio.wait_for(cli_task, 3)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                else:
                    cli_task.result()
                await composer.close_async()

    with _environment(tmp_path, monkeypatch) as environment:
        asyncio.run(scenario(environment))


@pytest.mark.parametrize("automatic", [False, True])
def test_permission_details_expose_full_arguments(automatic):
    async def scenario():
        with create_pipe_input() as pipe:
            composer = _composer(pipe)
            bridge = TerminalInteractionBridge(composer, SimpleNamespace(pause=lambda: None, resume=lambda: None), asyncio.get_running_loop())
            payload = {"command": "\n".join("echo " + "x" * 100 for _ in range(80)) + "\necho LAST_PERMISSION_MARKER", "workdir": "target-directory"}
            if automatic:
                task = asyncio.create_task(bridge.ask_auto_permission("bash", payload, {"reason": "review"}))
            else:
                task = asyncio.create_task(bridge._ask_permission("bash", payload))
            try:
                await _until(lambda: composer._dialog_future is not None)
                assert composer._dialog_kind == "selector-detail"
                # overlay登记早于实际绘制；窗口就绪后再发送翻页键。
                await _until(lambda: composer._selector_detail_window.render_info is not None)
                assert "LAST_PERMISSION_MARKER" in composer._dialog_detail
                assert "target-directory" in composer._dialog_detail
                assert json.loads(composer._dialog_detail.split("```json\n", 1)[1].rsplit("```", 1)[0]) == payload
                before = composer._selector_detail_window.vertical_scroll
                pipe.send_text("\x1b[6~")
                await _until(lambda: composer._selector_detail_window.vertical_scroll > before)
                pipe.send_text("\x1b[F")
                await asyncio.sleep(0.05)
                assert "LAST_PERMISSION_MARKER" in "".join(text for line in composer._detail_content_lines(80) for _style, text in line)
                pipe.send_text("\r")
                assert await asyncio.wait_for(task, 2) == "once"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await composer.close_async()
    asyncio.run(scenario())
