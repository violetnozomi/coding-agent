"""真实跨 run 旅程验证追加验收与原要求、失败事实的保留。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import socket
from types import SimpleNamespace

from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from nz_coder.tools.scratchpad import scratchpad
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request


OLD_COMMAND = "python -m unittest tests.test_old -v"
NEW_COMMAND = "python -m unittest tests.test_new -v"
ORIGINAL_NOTE = "Keep the original task failure note across runs."


def _tool(name, **arguments):
    return name, arguments


def _acceptance_journey(monkeypatch, tmp_path, *, fail_old):
    workspace = tmp_path / "project"
    workspace.mkdir()
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (workspace / "app.py").write_text("def value(): return 0\n")
    for name, expected in (("old", 1), ("new", 22)):
        (tests / f"test_{name}.py").write_text(
            "import unittest\nfrom app import value\n"
            "class ValueTest(unittest.TestCase):\n"
            f"    def test_value(self): self.assertEqual(value(), {expected})\n"
        )
    original_tests = {path.name: path.read_bytes() for path in tests.glob("*.py")}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    for setting in (
        "NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED",
        "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM",
    ):
        monkeypatch.setenv(setting, "0")

    def deny_network(*_args, **_kwargs):
        raise AssertionError("offline continuation journey attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", deny_network)

    class Provider:
        name = "offline"
        phase = 0

        def __init__(self):
            self.calls = [0, 0]

        def create_completion(self, _client, **kwargs):
            names = [item["function"]["name"] for item in kwargs.get("tools", [])]
            if names == ["emit_sidecar_verdict"]:
                action = [_tool("emit_sidecar_verdict", verdict="accept", reason="Controlled offline review")]
            else:
                index = self.calls[self.phase]
                self.calls[self.phase] += 1
                assert self.calls[self.phase] <= 6, "unexpected unbounded coding requests"
                first = [_tool("read_file", path="app.py"),
                         _tool("update_scratchpad", category="failure", content=ORIGINAL_NOTE)]
                if fail_old:
                    first.append(_tool("bash", command=OLD_COMMAND))
                # 返回值长度发生变化，避免同秒真实子进程复用旧字节码。
                actions = [first] if self.phase == 0 else [
                    [_tool("read_file", path="app.py")],
                    [_tool("edit_file", path="app.py", old_text="return 0", new_text="return 22")],
                    [_tool("bash", command=NEW_COMMAND)],
                ]
                action = actions[index] if index < len(actions) else "The current request is complete."
            response = _completion(action if isinstance(action, str) else "")
            if not isinstance(action, str):
                response.choices[0].message.tool_calls = [SimpleNamespace(
                    id=f"call-{self.phase}-{self.calls[self.phase]}-{index}", type="function",
                    function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
                ) for index, (name, arguments) in enumerate(action)]
                response.choices[0].finish_reason = "tool_calls"
            return response

    provider = Provider()
    runtime = _install_runtime(monkeypatch, provider)
    monkeypatch.setattr("nz_coder.runtime.model_gateway.resolve_model_runtime", lambda *_a, **_k: runtime)
    initial = f"Fix app.py value to 22.\nRun {OLD_COMMAND}"
    request = replace(_request(workspace), session_id="acceptance-continuation",
        messages=({"role": "user", "content": initial},),
        metadata={"permission_mode": "auto", "persist_session": True})
    options = RunOptions(permission_asker=lambda *_args: True)

    async def run_one(current_request, max_turns):
        monkeypatch.setenv("MAX_AGENT_TURNS", str(max_turns))
        environment = native_sdk.build_product_run_environment(current_request, options)
        try:
            result = await native_sdk.NativeSDKRunner(environment).run_result(current_request, options)
            with scoped_workdir(workspace), sessions.scoped_session(request.session_id):
                notes = scratchpad.read()
            return {
                "result": result,
                "state": environment.runtime_state.to_dict(),
                "checkpoint": environment.verification_runtime.domain.checkpoint(),
                "verification": environment.verification_runtime.domain.snapshot().to_dict(),
                "notes": notes,
            }
        finally:
            environment.close()

    async def scenario():
        first = await run_one(request, 1)
        provider.phase = 1
        followup = (
            "Continue the app.py fix. Also require this additional acceptance command.\n"
            "Do not modify tests.\n"
            f"Acceptance command: `{NEW_COMMAND}`"
        )
        resumed = await run_one(replace(request, messages=({"role": "user", "content": followup},)), 5)
        return first, resumed

    first, resumed = asyncio.run(scenario())
    assert first["result"].status.value == "max_turns"
    assert first["checkpoint"]["scope"]["task"]["task_lineage_id"] == (
        resumed["checkpoint"]["scope"]["task"]["task_lineage_id"]
    )
    assert ORIGINAL_NOTE in first["notes"] and ORIGINAL_NOTE in resumed["notes"]
    assert resumed["state"]["initial_task_text"] == initial
    assert resumed["state"]["forbids_test_changes"] is True
    assert (workspace / "app.py").read_text() == "def value(): return 22\n"
    assert {path.name: path.read_bytes() for path in tests.glob("*.py")} == original_tests
    assert any(fact["command"]["original"] == NEW_COMMAND and fact["exit_code"] == 0
               and fact["test_count"] == 1 for fact in resumed["checkpoint"]["facts"])
    assert resumed["state"]["task_contract"]["acceptance_commands"] == [OLD_COMMAND, NEW_COMMAND]
    assert resumed["state"]["verification_contract"]["command"] == OLD_COMMAND
    assert resumed["state"]["verification_contract"]["passed"] is False
    return first, resumed


def test_resumed_task_adds_acceptance_without_retiring_unexecuted_original(monkeypatch, tmp_path):
    """新增验收通过不能替代尚未满足的原验收。"""
    first, resumed = _acceptance_journey(monkeypatch, tmp_path, fail_old=False)

    assert first["checkpoint"]["facts"] == []
    assert first["checkpoint"]["obligations"] == []
    assert resumed["result"].status.value in {"blocked", "max_turns"}
    assert resumed["verification"]["completion_ready"] is False


def test_added_acceptance_pass_does_not_erase_real_failure_from_previous_run(monkeypatch, tmp_path):
    """追加验收不能删掉原明确验收的真实失败及复测义务。"""
    first, resumed = _acceptance_journey(monkeypatch, tmp_path, fail_old=True)

    old_failure = next(fact for fact in first["checkpoint"]["facts"]
                       if fact["command"]["original"] == OLD_COMMAND)
    assert old_failure["exit_code"] == 1 and old_failure["test_count"] == 1
    assert resumed["result"].status.value in {"blocked", "max_turns"}
    assert resumed["verification"]["completion_ready"] is False
    assert any(fact["execution_id"] == old_failure["execution_id"]
               for fact in resumed["checkpoint"]["facts"])
    assert any(old_failure["execution_id"] in obligation["failure_execution_ids"]
               and not obligation["resolved"] for obligation in resumed["checkpoint"]["obligations"])
