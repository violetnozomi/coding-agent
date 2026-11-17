"""真实 Native 运行须消费同一域，模型答复不能覆盖执行失败。"""
from __future__ import annotations

from pathlib import Path


def _project(root: Path):
    (root / "tests").mkdir()
    (root / "tests/__init__.py").touch()
    (root / "tests/test_ok.py").write_text("import unittest\nclass TestOK(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
    (root / "quiet.py").write_text("raise SystemExit(7)\n")


def test_native_quiet_failure_cannot_reuse_previous_suite_pass(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    _project(tmp_path)
    suite, quiet = "python -m unittest discover -s tests -t .", "python quiet.py"
    actions = [[_tool("bash", command=suite)], [_tool("bash", command=quiet)], *("All checks passed." for _ in range(5))]
    result, _state, trace, *_ = _run(monkeypatch, tmp_path, actions,
        task="Run the test suite and quiet.py and report actual results. Do not change files.",
        allowed_commands=(suite, quiet), limit=len(actions), semantic_response="accept")
    assert result.status.value != "completed", result.final_text
    verification = result.metadata["verification"]
    assert verification["domain_revision"] > 0
    assert verification["verification_needed"]
    assert any(item.get("event") == "verification_result" and item.get("command") == quiet
               and item.get("status") == "failed" for item in trace)


def test_native_frozen_result_uses_domain_revision(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    _project(tmp_path)
    suite = "python -m unittest discover -s tests -t ."
    result, _state, _trace, *_ = _run(monkeypatch, tmp_path,
        [[_tool("bash", command=suite)], "The test suite passed."],
        task="Run the existing unit tests and report the result. Do not change files.",
        allowed_commands=(suite,), limit=2, semantic_response="accept")
    assert result.status.value == "completed", result.final_text
    verification = result.metadata["verification"]
    assert verification["domain_revision"] > 0
    assert verification["verification_state"] == "passed"
    assert verification["terminal_reason"] == "completed"


def test_runtime_exact_acceptance_uses_real_failure_fact(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    (tmp_path / "value.py").write_text("value = 1\n")
    (tmp_path / "test_value.py").write_text("def test_value():\n assert False\n")
    command = "pytest test_value.py -q"
    result, state, trace, *_ = _run(monkeypatch, tmp_path,
        [[_tool("write_file", path="value.py", content="value = 2\n")], *("Done." for _ in range(4))],
        task="Update value.py. Acceptance command: `pytest test_value.py -q`.",
        allowed_commands=(command, "python -m py_compile value.py"), limit=5)
    assert result.status.value != "completed"
    assert state["verification_contract"]["passed"] is False
    assert state["verification_contract"]["source"] == "runtime"
    assert any(item["event"] == "verification_stage_executed" and not item["passed"] for item in trace)


def test_native_process_failure_is_recorded_without_read(monkeypatch, tmp_path):
    import threading
    from nz_coder.runtime.adapters.verification_runtime import VerificationRuntime
    from nz_coder.runtime.execution.loop import ProductRunEnvironment
    from test_node_verification_runtime import _run, _tool

    _project(tmp_path)
    delivered = threading.Event()
    terminal = VerificationRuntime._receive_local_terminal
    execute = ProductRunEnvironment._execute_tool_call_with_hooks

    def receive(self, fact):
        accepted = terminal(self, fact)
        delivered.set()
        return accepted

    def execute_and_wait(self, call, index, messages):
        result = execute(self, call, index, messages)
        if call["function"]["name"] == "process":
            assert result.executed, result.output
            assert delivered.wait(5), "terminal fact was not delivered"
        return result

    monkeypatch.setattr(VerificationRuntime, "_receive_local_terminal", receive)
    monkeypatch.setattr(ProductRunEnvironment, "_execute_tool_call_with_hooks", execute_and_wait)
    result, _state, trace, *_ = _run(monkeypatch, tmp_path,
        [[_tool("process", operation="start", command="python quiet.py", tty=False)], *("Done." for _ in range(3))],
        task="Run quiet.py in a process and report its actual exit. Do not modify files.",
        allowed_commands=("python quiet.py",), limit=4)
    assert result.status.value != "completed"
    assert any(item.get("event") == "verification_result" and item.get("status") == "failed"
               and item.get("command") == "python quiet.py" for item in trace)


def test_native_ast_syntax_failure_survives_unrelated_mutation_and_pass(monkeypatch, tmp_path):
    import nz_coder.tools.python_ast  # noqa: F401  # 注册可选内置工具，仍使用真实执行器。
    from test_node_verification_runtime import _run, _tool

    _project(tmp_path)
    (tmp_path / "broken.py").write_text("def broken(:\n")
    suite = "python -m unittest discover -s tests -t ."
    result, _state, trace, *_ = _run(monkeypatch, tmp_path,
        [[_tool("python_symbol_check", path="broken.py")],
         [_tool("write_file", path="marker.py", content="value = 2\n")],
         [_tool("bash", command=suite)], *("Done." for _ in range(3))],
        task="Inspect broken.py, update marker.py and run the local tests. Report actual failures.",
        allowed_commands=(suite, "python -m py_compile marker.py"), limit=6, semantic_response="blocked")
    assert result.status.value != "completed"
    assert any(item.get("event") == "verification_result" and item.get("status") == "failed"
               and item.get("stage") == "static" for item in trace)
    assert result.metadata["verification"]["test_obligations"] == []
    assert any("python_symbol_check" in item["command"] for item in result.metadata["verification"]["ordinary_failures"])


def test_native_reordered_ignore_arguments_cannot_clear_failed_selection(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    (tmp_path / "test_a.py").write_text("def test_a():\n assert False\n")
    (tmp_path / "test_b.py").write_text("def test_b():\n assert True\n")
    first, second = "pytest test_a.py --ignore test_b.py -q", "pytest test_b.py --ignore test_a.py -q"
    result, _state, trace, *_ = _run(monkeypatch, tmp_path,
        [[_tool("bash", command=first)], [_tool("bash", command=second)], *("Done." for _ in range(3))],
        task="Run the two selections and report their actual outcomes. Do not modify files.",
        allowed_commands=(first, second), limit=5, semantic_response="blocked")
    assert result.status.value != "completed"
    assert result.metadata["verification"]["test_obligations"] == []
    assert any(item["command"] == first for item in result.metadata["verification"]["ordinary_failures"])
    assert any(item.get("command") == second and item.get("status") == "passed" for item in trace)


def test_file_risk_refresh_cannot_drop_preceding_shell_owned_change(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    _project(tmp_path)
    create = "printf 'def broken(:\\n' > bad.py"
    suite = "python -m unittest discover -s tests -t ."
    result, state, *_ = _run(monkeypatch, tmp_path,
        [[_tool("bash", command=create)], [_tool("write_file", path="marker.md", content="note\n")],
         [_tool("read_file", path="marker.md")], [_tool("bash", command=suite)], "Done."],
        task="Create bad.py and marker.md for the project, and use local checks to validate the result.",
        allowed_commands=(create, suite, "python -m py_compile bad.py"), limit=5, semantic_response="blocked")
    assert "bad.py" in state["changed_files"]
    assert result.status.value != "completed"


def test_native_direct_shebang_failure_cannot_reuse_prior_suite_pass(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    _project(tmp_path)
    script = tmp_path / 'quiet.py'
    script.write_text('#!/usr/bin/env python3\nraise SystemExit(7)\n')
    script.chmod(0o755)
    suite = 'python -m unittest discover -s tests -t .'
    result, state, *_ = _run(monkeypatch, tmp_path,
        [[_tool('bash', command=suite)], [_tool('bash', command='./quiet.py')], 'All checks passed.'],
        task='Run the existing suite and ./quiet.py, report actual results. Do not change files.',
        allowed_commands=(suite, './quiet.py'), limit=3)
    assert result.status.value != 'completed'
    assert any(item['command'] == './quiet.py' and item['status'] == 'pending'
               for item in result.metadata['verification']['test_obligations'])


def test_native_declared_custom_acceptance_is_required_before_any_execution(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run
    (tmp_path / 'check.py').write_text('raise SystemExit(7)\n')
    result, state, *_ = _run(monkeypatch, tmp_path, ['Done.'],
        task='Run the explicit acceptance command: `python check.py`. Report the actual result. Do not modify files.',
        allowed_commands=('python check.py',), limit=1)
    assert result.status.value != 'completed'
    assert result.metadata['verification']['verification_needed']
    assert any(item['command'] == 'python check.py' and item['status'] == 'failed'
               for stage in result.metadata['verification']['verification_pipeline']['stages'] for item in stage['commands'])


def test_native_static_tool_cache_cannot_invalidate_its_own_check(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    (tmp_path / 'app.py').write_text('value = 1\n')
    result, state, *_ = _run(monkeypatch, tmp_path,
        [[_tool('edit_file', path='app.py', old_text='value = 1', new_text='value = 2')],
         [_tool('verify_changed_files')], 'Changed file compiles.'],
        task='Update app.py and compile the changed source. Use the existing static checker.', limit=3)
    assert result.status.value == 'completed', result.final_text
    assert state['mutation_generation'] == 1
    assert state['verification_generation'] == 1
    assert '__pycache__' not in state['changed_files']
