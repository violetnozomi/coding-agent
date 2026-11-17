"""同目录cd不能让已真实通过的用户验收丢失身份。"""
from __future__ import annotations

from dataclasses import replace
import shlex

import pytest

from nz_coder.runtime.verification.domain.identity import command_identity, verified_check_subject
from nz_coder.runtime.verification.domain.models import CheckSpec, ExecutionFact, RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.rules import evaluate_check


@pytest.mark.parametrize("prefix", ["dot", "absolute", "option", "space"])
def test_native_runner_accepts_same_cwd_without_duplicate_acceptance(monkeypatch, tmp_path, prefix):
    from test_node_verification_runtime import _run, _tool

    root = tmp_path / "work space" if prefix == "space" else tmp_path
    root.mkdir(exist_ok=True)
    for name in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    (root / "app.py").write_text("value = 0\n")
    (root / "tests").mkdir()
    (root / "tests/test_app.py").write_text(
        "import unittest\nfrom app import value\nclass AppTest(unittest.TestCase):\n"
        " def test_value(self): self.assertEqual(value, 123)\n")
    bare = "python -m unittest discover -s tests -v"
    target = "." if prefix == "dot" else shlex.quote(str(root))
    wrapped = f"cd {'-- ' if prefix == 'option' else ''}{target} && {bare}"
    result, _state, trace, _requests, _reviews, _permissions = _run(
        monkeypatch, root, [
            [_tool("read_file", path="app.py")],
            [_tool("edit_file", path="app.py", old_text="value = 0", new_text="value = 123")],
            [_tool("bash", command=wrapped)], "The value is fixed and the declared test passed.",
            "The value is fixed and the declared test passed.",
            "The value is fixed and the declared test passed.",
        ], task=f"Set app.py value to 123. Acceptance command: `{bare}`.",
        allowed_commands=(wrapped, bare, "python -m py_compile app.py"), limit=10,
    )
    assert result.status.value == "completed", (result.error, result.final_text)
    commands = [e.get("input", {}).get("command") for e in trace
                if e.get("event") == "tool_call" and e.get("name") == "bash" and e.get("executed")]
    assert commands.count(wrapped) == 1 and bare not in commands, commands
    required = [c for s in result.metadata["verification"]["verification_pipeline"]["stages"]
                for c in s["commands"] if c.get("automation_provenance") == "user_declared"]
    assert required and all(c["status"] == "passed" and c["last_command"] == wrapped for c in required)


@pytest.mark.parametrize("suffix", [" > output.log", " || true", " && touch changed", " | cat || true"])
def test_same_cwd_does_not_weaken_unsafe_wrapper_rejection(tmp_path, suffix):
    command = f"cd {shlex.quote(str(tmp_path))} && python -m unittest tests.test_app -v{suffix}"
    assert verified_check_subject(command_identity(command, str(tmp_path))) is None


@pytest.mark.parametrize("change", ["unreliable", "incomplete", "version", "env", "other-cwd", "dynamic"])
def test_wrapped_success_keeps_fact_and_command_guards(tmp_path, change):
    bare = "MODE=one python -m unittest tests.test_app -v"
    command = f"cd {shlex.quote(str(tmp_path))} && {bare}"
    if change == "env":
        command = command.replace("MODE=one", "MODE=two")
    elif change == "other-cwd":
        command = command.replace(shlex.quote(str(tmp_path)), shlex.quote(str(tmp_path.parent)))
    elif change == "dynamic":
        command = "cd $PWD && " + bare
    spec = CheckSpec("user", command_identity(bare, str(tmp_path)), "test", required=True, source="user_declared")
    version = WorkspaceVersion()
    scope = RunScope(TaskScope("session", "task", str(tmp_path)), "run", "agent")
    fact = ExecutionFact("success", scope, command_identity(command, str(tmp_path)), "succeeded", version, version,
                         capabilities=("test",), exit_code=0, test_count=1)
    if change == "unreliable":
        fact = replace(fact, reliable=False)
    elif change == "incomplete":
        fact = replace(fact, output_complete=False)
    elif change == "version":
        fact = replace(fact, after=WorkspaceVersion(1, 1, 1))
    assert evaluate_check(spec, fact, fact.after).status == "pending"
