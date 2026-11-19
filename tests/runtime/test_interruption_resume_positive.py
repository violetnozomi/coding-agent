"""硬退出仅承诺恢复已提交检查点，不重建未落盘的最后一步。"""

import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
import threading
from dataclasses import replace
import os

import pytest

from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import session_runtime_state_path
from settlement_scenarios import setup, payload, _reply, _call


CHECK_COMMAND = "python -m unittest discover -s tests -v"
EDIT_COMMAND = "printf '\nv = 2\n' >> app.py"


def test_cancel_then_fresh_environment_resumes_durable_history(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            n = len(calls)
            if n == 1:
                return _reply(
                    kwargs,
                    calls=[
                        _call(
                            1, "write_file", {"path": "marker.txt", "content": "ready"}
                        )
                    ],
                )
            if n == 2:
                entered.set()
                assert release.wait(5)
                return _reply(kwargs, text="late answer")
            return _reply(
                kwargs, text="Existing marker retained; read-only continuation."
            )

    request, options = setup(monkeypatch, tmp_path, Provider())
    request = replace(
        request,
        messages=[
            {
                "role": "user",
                "content": "Create marker.txt containing ready. Preserve the original result on continuation.",
            }
        ],
        tool_names=("write_file",),
    )
    runner = native_sdk.NativeSDKRunner()

    async def run():
        task = asyncio.create_task(runner.run_result(request, options))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        release.set()
        try:
            cancelled = await task
        except asyncio.CancelledError:
            pass
        else:
            assert cancelled.status.value == "cancelled"
        assert (tmp_path / "marker.txt").read_text() == "ready"
        continued = replace(
            request, messages=[{"role": "user", "content": "继续"}], tool_names=()
        )
        result = await runner.run_result(continued, options)
        assert result.status.value in {"completed", "completed_unverified"}
        assert len(calls) == 3
        assert "Create marker.txt" in str(calls[2]["messages"])
        assert (tmp_path / "marker.txt").read_text() == "ready"
        assert payload(tmp_path, request)["run_status"] in {
            "completed",
            "completed_unverified",
        }

    try:
        asyncio.run(run())
    finally:
        release.set()
        runner.retry_cleanup(tmp_path, request.session_id)


def test_hard_process_exit_then_resume_from_durable_checkpoint(monkeypatch, tmp_path):
    repo = Path(__file__).resolve().parents[2]
    child = tmp_path / "child_harness.py"
    child.write_text("""
import asyncio, os, sys
from dataclasses import replace
from pathlib import Path
import pytest
from settlement_scenarios import setup, _reply, _call, native_sdk
class Provider:
    name='offline'
    count=0
    def create_completion(self,_client,**kwargs):
        self.count+=1
        if self.count==1:return _reply(kwargs,calls=[_call(1,'write_file',{'path':'marker.txt','content':'durable-before-crash'})])
        os._exit(77)
mp=pytest.MonkeyPatch()
request,options=setup(mp,Path(sys.argv[1]),Provider())
request=replace(request,messages=[{'role':'user','content':'Create marker.txt; preserve it when resuming.'}],tool_names=('write_file',))
asyncio.run(native_sdk.NativeSDKRunner().run_result(request,options))
""")
    completed = subprocess.run(
        [sys.executable, "-B", str(child), str(tmp_path)],
        cwd=repo,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "tests/runtime"))),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 77, completed.stderr
    assert (tmp_path / "marker.txt").read_text() == "durable-before-crash"
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            return _reply(
                kwargs, text="Existing committed file retained after restart."
            )

    request, options = setup(monkeypatch, tmp_path, Provider())
    request = replace(
        request, messages=[{"role": "user", "content": "继续"}], tool_names=()
    )
    result = asyncio.run(native_sdk.NativeSDKRunner().run_result(request, options))
    assert result.status.value in {"completed", "completed_unverified"}
    assert len(requests) == 1 and "Create marker.txt" in str(requests[0]["messages"])
    assert (tmp_path / "marker.txt").read_text() == "durable-before-crash"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX Bash crash cut")
def test_resume_rechecks_unrecorded_shell_change_without_replay(monkeypatch, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    app = workspace / "app.py"
    app.write_text("v = 1\n")
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "import unittest\nfrom app import v\n"
        "class Acceptance(unittest.TestCase):\n"
        "    def test_value(self): self.assertEqual(v, 1)\n"
    )
    initial = f"Keep the app valid.\nAcceptance command: `{CHECK_COMMAND}`"
    harness = tmp_path / "crash_before_receipt.py"
    harness.write_text("""
import asyncio, os, sys
from dataclasses import replace
from pathlib import Path
import pytest
from nz_coder.runtime.adapters import verification_facts
from settlement_scenarios import setup, native_sdk, _call, _reply

workspace, command, edit, initial = Path(sys.argv[1]), *sys.argv[2:]
original_receipt = verification_facts.command_receipt

def receipt(tool, cmd, cwd, **kwargs):
    if cmd == edit:
        assert kwargs['exit_code'] == 0
        assert (workspace / 'app.py').read_text() == 'v = 1\\n\\nv = 2\\n'
        os._exit(78)
    return original_receipt(tool, cmd, cwd, **kwargs)

class Provider:
    calls = 0
    def create_completion(self, _client, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return _reply(kwargs, calls=[_call(1, 'bash', {'command': command})])
        assert self.calls == 2
        return _reply(kwargs, calls=[_call(2, 'bash', {'command': edit})])

patch = pytest.MonkeyPatch()
patch.setattr(verification_facts, 'command_receipt', receipt)
request, options = setup(patch, workspace, Provider())
request = replace(request, tool_names=('bash',),
                  messages=({'role': 'user', 'content': initial},))
asyncio.run(native_sdk.NativeSDKRunner().run_result(request, options))
""")
    repo = Path(__file__).resolve().parents[2]
    user_config = workspace.parent / (workspace.name + "-XDG_CONFIG_HOME") / "config.env"
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join((str(repo), str(repo / "tests/runtime"))),
        NZ_CODER_USER_CONFIG=str(user_config),
        NZ_CODER_WORKSPACE_TRUST_STORE=str(user_config.with_name("workspace-trust.json")),
    )
    crashed = subprocess.run(
        [sys.executable, "-B", str(harness), str(workspace), CHECK_COMMAND, EDIT_COMMAND, initial],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=20,
    )
    assert crashed.returncode == 78, (crashed.stdout, crashed.stderr)
    after_edit = "v = 1\n\nv = 2\n"
    assert app.read_text() == after_edit

    class Provider:
        calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            assert self.calls <= 6, "unexpected continuation requests"
            return _reply(kwargs, text="The old validation passed.")

    provider = Provider()
    request, options = setup(monkeypatch, workspace, provider)
    durable = payload(workspace, request)
    results = {item["tool_call_id"]: item for item in durable["messages"] if item["role"] == "tool"}
    assert results["memory-1"]["_nz_verification_passed"] is True
    assert "OK" in results["memory-1"]["content"]
    assert "memory-2" not in results
    with scoped_workdir(workspace):
        state_file = session_runtime_state_path(request.session_id)
    before = json.loads(state_file.read_text())["verification_domain"]
    old_check, = [fact for fact in before["facts"] if fact["command"]["original"] == CHECK_COMMAND]
    assert old_check["exit_code"] == 0 and old_check["test_count"] == 1

    request = replace(request, tool_names=("bash",), messages=({"role": "user", "content": "继续"},))
    result = asyncio.run(native_sdk.NativeSDKRunner().run_result(request, options))
    assert result.status.value in {"blocked", "max_turns"}, result
    verification = result.metadata["verification"]
    assert verification["tests_passed"] is False
    assert verification["completion_ready"] is False
    assert verification["last_verification"]["status"] == "failed"
    assert app.read_text() == after_edit
    saved_after = payload(workspace, request)
    results_after = {item["tool_call_id"]: item for item in saved_after["messages"] if item["role"] == "tool"}
    # 验证域可以淘汰已经被替代的快照事实，已落盘的原工具结果仍可追溯。
    assert results_after["memory-1"]["content"] == results["memory-1"]["content"]
    assert results_after["memory-1"]["_nz_verification_passed"] is True
    assert "memory-2" not in results_after
    after = json.loads(state_file.read_text())["verification_domain"]
    current_failures = [fact for fact in after["facts"]
                        if fact["command"]["original"] == CHECK_COMMAND and fact["exit_code"] == 1]
    assert current_failures and current_failures[-1]["test_count"] == 1
    assert after["version"]["source_generation"] > before["version"]["source_generation"]
