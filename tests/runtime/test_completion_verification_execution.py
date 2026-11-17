"""自然收尾直接执行已知检查，保留普通审批及域事实裁决。"""
from __future__ import annotations

import pytest


def _static(root, command, *, passed=False):
    return {"id": command, "command": command, "retry_command": command, "cwd": str(root),
            "scope": str(root), "required": True, "status": "passed" if passed else "pending",
            "capability": "syntax", "automation_provenance": "planner", "retry_eligible": True}


def _run_strict(*args, **kwargs):
    from test_node_verification_runtime import _run
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    with scoped_runtime_overrides(strict_local_tools=True):
        return _run(*args, **kwargs)


def test_completion_selection_ignores_passed_unknown_and_optional_checks(tmp_path):
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    status = {"strict": True, "verification_pipeline": {"stages": [{"name": "static", "commands": [
        _static(tmp_path, "python -m py_compile first.py"),
        _static(tmp_path, "python -m py_compile already.py", passed=True),
        _static(tmp_path, "python custom_verifier.py"),
        {**_static(tmp_path, "python -m py_compile optional.py"), "required": False},
    ]}]}, "test_obligations": []}
    actions = VerificationScheduler().completion_actions(status, mutation_generation=3)
    assert [(a.stage, a.command, a.cwd) for a in actions] == [
        ("static", "python -m py_compile first.py", str(tmp_path)),
    ]


@pytest.mark.parametrize("case", ["pass", "fail", "deny", "empty"])
def test_native_completion_executes_declared_discovery_without_file_selector(monkeypatch, tmp_path, case):
    from test_node_verification_runtime import _run, _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    command = "python -m unittest discover -s tests -v"
    content = "import unittest\nfrom app import VALUE\nclass AppTest(unittest.TestCase):\n"
    content += ("    pass\n" if case == "empty" else
                f"    def test_value(self):\n        self.assertEqual(VALUE, {2 if case == 'fail' else 1})\n")
    files = [{"path": "app.py", "content": "VALUE = 1\n"},
             {"path": "tests/test_app.py", "content": content}]
    result, state, trace, _requests, _reviews, permissions = _run(
        monkeypatch, tmp_path,
        [[_tool("write_files_batch", files=files)], *(["Implemented; please settle verification."] * 8)],
        task="创建 app.py 和 tests/test_app.py，提供 VALUE=1 并添加真实测试。验收命令：`" + command + "`。",
        allowed_commands=("python -m py_compile app.py", *(() if case == "deny" else (command,))),
        limit=9,
    )
    executed = [r for r in trace if r.get("event") == "tool_call" and r.get("name") == "bash"
                and r.get("input", {}).get("command") == command and r.get("executed")]
    assert any(args.get("command") == command for _name, args, _allowed in permissions)
    assert state["verification_contract"]["targets"] == []
    if case == "deny":
        assert not executed
    else:
        # 两个入口共用同检查、同版本去重，不再重复执行失败验收。
        assert len(executed) == 1
    if case == "pass":
        assert result.status.value == "completed", (result.final_text, state["requirement_ledger"])
        assert state["verification_contract"]["passed"] is True
    else:
        assert result.status.value != "completed"
        if result.status.value == "blocked":
            assert "work limit" not in result.final_text


def test_native_discovery_cleanup_and_eight_module_checks_reach_current_completion(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    command = "python -m unittest discover -s tests -v"
    paths = [f"module{i}.py" for i in range(8)]
    compilers = tuple(f"python -m py_compile {path}" for path in paths)
    files = [{"path": path, "content": "VALUE = 1\n"} for path in paths]
    files.append({"path": "tests/test_modules.py", "content":
        "from pathlib import Path\nimport unittest\nimport module0\n"
        "class ModulesTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(module0.VALUE, 1)\n"
        "        Path('workspace-scratch').mkdir(exist_ok=True)\n"})
    cleanup = "rm -rf workspace-scratch __pycache__ tests/__pycache__"
    actions = [[_tool("write_files_batch", files=files)], [_tool("bash", command=command)],
               [_tool("bash", command=cleanup)], "All modules are implemented.",
               [_tool("bash", command=compilers[5])], [_tool("bash", command=compilers[6])],
               [_tool("bash", command=compilers[7])],
               *(["Implementation and current validation are ready."] * 8)]
    result, state, trace, requests, _reviews, _permissions = _run(
        monkeypatch, tmp_path, actions,
        task="创建并验证 " + "、".join(paths) + "，添加 tests/test_modules.py 的真实测试。验收命令：\n"
            + "\n".join(f"验收命令：`{cmd}`。" for cmd in (command, *compilers)),
        allowed_commands=(*compilers, command, cleanup), limit=14,
    )
    assert result.status.value == "completed", (result.final_text, state["requirement_ledger"])
    assert state["verification_contract"]["passed"] is True
    assert state["completion_gate_prompts"] <= 2
    assert not any("__pycache__" in p for p in state["last_mutation_paths"])
    real = [r for r in trace if r.get("event") == "tool_call" and r.get("name") == "bash" and r.get("executed")]
    assert {r["input"]["command"] for r in real if "py_compile" in r["input"]["command"]} == set(compilers)
    assert sum(r["input"]["command"] == command for r in real) >= 2
    assert len(requests) <= len(actions)


@pytest.mark.parametrize("command, capability", [
    ("cargo check", "build"), ("go test ./pkg -run '^$'", "build"),
    ("tsc --noEmit", "type"), ("ruff check src/app.py", "lint"),
])
def test_completion_selects_known_direct_required_static_checks(tmp_path, command, capability):
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler
    status = {"strict": True, "verification_pipeline": {"stages": [{"name": "static", "commands": [
        {**_static(tmp_path, command), "capability": capability},
    ]}]}}
    actions = VerificationScheduler().completion_actions(status, mutation_generation=1)
    assert len(actions) == 1 and actions[0].command == command


def test_completion_does_not_infer_native_entrypoint_from_script_arguments(tmp_path):
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler
    command = "python custom_verifier.py tests/runtests.py case_a"
    check = {**_static(tmp_path, command), "id": "unknown", "capability": "test",
             "automation_provenance": "model_execution"}
    status = {"strict": True, "verification_pipeline": {"stages": [{"name": "targeted", "commands": [check]}]},
              "test_obligations": [{"check_id": "unknown", "status": "pending", "failure_execution_ids": ["failure"]}]}
    assert not VerificationScheduler().completion_actions(status, mutation_generation=1)


@pytest.mark.parametrize("deny", [False, True])
def test_native_completion_checks_each_changed_module_without_model_reassembly(monkeypatch, tmp_path, deny):
    from test_node_verification_runtime import _run, _tool

    names = ["__init__", "core", "storage", "cli", "__main__"]
    commands = tuple(f"python -m py_compile tasknest/{name}.py" for name in names)
    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    files = [{"path": f"tasknest/{name}.py", "content": "value = 1\n"} for name in names]
    actions = [[_tool("write_files_batch", files=files)], "The requested modules are implemented."]
    if deny:
        actions += ["The compiler checks were denied."] * 3
    result, _state, trace, _requests, _reviews, permissions = _run(
        monkeypatch, tmp_path, actions,
        task="创建 tasknest 包及这五个模块：" + "、".join(f"tasknest/{n}.py" for n in names)
            + "。\n" + "\n".join(f"验收命令：`{cmd}`。" for cmd in commands),
        allowed_commands=() if deny else commands, limit=12,
    )
    if deny:
        assert result.status.value != "completed"
        assert not any(r.get("event") == "tool_call" and r.get("name") == "bash" and r.get("executed") for r in trace)
        assert len(permissions) == 1
        return
    assert result.status.value == "completed", (result.error, result.final_text)
    checks = [r for r in trace if r.get("event") == "tool_call" and r.get("name") == "bash" and r.get("executed")]
    assert {r["input"]["command"] for r in checks} == set(commands)
    assert len(checks) == 5
    assert len({r["tool_call_id"] for r in checks}) == 5
    assert all(r["input"]["workdir"] == str(tmp_path) for r in checks)
    assert len(permissions) == 5 and all(p[2] for p in permissions)


def test_completion_does_not_replay_optional_failed_test_selector(tmp_path):
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    command = 'python -m unittest -k "EmptyFile" tests.test_storage'
    check = {**_static(tmp_path, command), "id": "failed-test", "capability": "test",
             "required": False, "automation_provenance": "model_execution"}
    status = {"strict": True, "verification_pipeline": {"stages": [{"name": "targeted", "commands": [check]}]},
              "test_obligations": [{"check_id": "failed-test", "status": "pending", "failure_execution_ids": ["actual-failure"]}]}
    actions = VerificationScheduler().completion_actions(status, mutation_generation=2)
    assert actions == ()
    check["required"] = True
    actions = VerificationScheduler().completion_actions(status, mutation_generation=2)
    assert len(actions) == 1 and actions[0].command == command
    check["required"] = False
    status["test_obligations"] = []
    assert not VerificationScheduler().completion_actions(status, mutation_generation=2)


@pytest.mark.parametrize("case", ["deny", "fail", "many"])
def test_budget_stage_and_completion_share_attempt_limit_and_stop(monkeypatch, tmp_path, case):
    from test_node_verification_runtime import _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    paths = [f"mod{i}.py" for i in range(8 if case == "many" else 1)]
    files = [{"path": path, "content": "syntax error!\n" if case == "fail" else "value = 1\n"} for path in paths]
    reads = []
    for i in range(9):
        (tmp_path / f"context{i}.txt").write_text(f"context {i}\n")
        reads.append([_tool("read_file", path=f"context{i}.txt")])
    commands = tuple(f"python -m py_compile {path}" for path in paths)
    result, _state, trace, _requests, _reviews, permissions = _run_strict(
        monkeypatch, tmp_path,
        [[_tool("write_files_batch", files=files)], *reads, *(["Done."] * 4)],
        task="创建并验证：" + "、".join(paths), limit=15,
        allowed_commands=() if case == "deny" else commands,
    )
    assert result.status.value != "completed"
    assert len(permissions) == (6 if case == "many" else 1)
    checks = [r for r in trace if r.get("event") == "tool_call" and r.get("name") == "bash"]
    assert len({r["tool_call_id"] for r in checks}) == len(checks)


@pytest.mark.parametrize("invalid", [True, False])
def test_completion_stops_after_failure_and_caps_execution_across_reentry(monkeypatch, tmp_path, invalid):
    from test_node_verification_runtime import _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    paths = [f"mod{i}.py" for i in range(8)]
    commands = tuple(f"python -m py_compile {path}" for path in paths)
    files = [{"path": path, "content": "broken syntax!\n" if invalid and i == 0 else "value = 1\n"}
             for i, path in enumerate(paths)]
    result, _state, trace, _requests, _reviews, permissions = _run_strict(
        monkeypatch, tmp_path, [[_tool("write_files_batch", files=files)], *(["Done."] * 4)],
        task="创建以下模块并验证：" + "、".join(paths), allowed_commands=commands, limit=20,
    )
    checks = [r for r in trace if r.get("event") == "tool_call" and r.get("name") == "bash" and r.get("executed")]
    assert result.status.value != "completed"
    assert len(checks) == (1 if invalid else 6)
    assert len(permissions) == len(checks)
    assert len({r["input"]["command"] for r in checks}) == len(checks)


def test_original_minitasks_task_completes_without_modifying_acceptance_file(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    (tmp_path / "minitasks.py").write_text("value = 0\n")
    acceptance = tmp_path / "test_minitasks.py"
    acceptance.write_text("import unittest\nfrom minitasks import value\nclass Acceptance(unittest.TestCase):\n" + "".join(
        f"    def test_case_{i}(self):\n        self.assertEqual(value, 1)\n" for i in range(15)))
    before = acceptance.read_bytes()
    task = "修复 minitasks.py，使 test_minitasks.py 的15项验收全部通过。不要修改验收文件。运行 python -m unittest -v。"
    command = "python -m unittest -v test_minitasks.py"
    result, state, trace, *_ = _run(
        monkeypatch, tmp_path, [
            [_tool("read_file", path="minitasks.py"), _tool("read_file", path="test_minitasks.py")],
            [_tool("edit_file", path="minitasks.py", old_text="value = 0", new_text="value = 1")],
            [_tool("bash", command=command)], "15 tests passed.",
        ], task=task,
        allowed_commands=(command, "python -m py_compile minitasks.py"), limit=12,
        prior_messages=({"role": "user", "content": "修复 minitasks.py。"},
                        {"role": "assistant", "content": "请说明验收要求。"}),
    )
    assert result.status.value == "completed", (result.error, state["requirement_ledger"])
    assert acceptance.read_bytes() == before
    assert "test_minitasks.py" not in state["requested_paths"]
    assert state["requirement_ledger"]["items"]
    assert all("test_minitasks.py" not in item["requirement"]["expected_artifacts"]
               for item in state["requirement_ledger"]["items"])
    assert any(r.get("event") == "verification_result" and r.get("status") == "passed"
               and "Ran 15 tests" in r.get("output", "") for r in trace)
