"""真实编辑和测试不能因为宿主清理备份而产生新的验收版本。"""
from __future__ import annotations

import tempfile

import pytest

from test_node_verification_runtime import _run, _tool
from test_workspace_scope_execution import _execute, _scope_host

from nz_coder.runtime.process.workdir import scoped_workdir


@pytest.mark.parametrize("failed_demo_first", [False, True])
def test_private_transaction_cleanup_does_not_invalidate_real_unittest(tmp_path, monkeypatch, failed_demo_first):
    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    local_tmp = tmp_path / ".tmp"
    local_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(local_tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(local_tmp))
    (tmp_path / "app.py").write_text("value = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("")
    (tmp_path / "tests/test_app.py").write_text(
        "import unittest\nfrom app import value\n"
        "class TestApp(unittest.TestCase):\n"
        "    def test_value(self): self.assertEqual(value, 2)\n"
    )
    command = "python -m unittest tests/test_app.py -v"
    actions = [
        [_tool("read_file", path="app.py")],
        [_tool("edit_file", path="app.py", old_text="value = 1", new_text="value = 2")],
        [_tool("bash", command=command)],
        "The changed application passed its declared test.",
    ]
    demo = "python -c 'import nonexistent_investigation_module'"
    if failed_demo_first:
        actions.insert(0, [_tool("bash", command=demo)])
    result, state, trace, requests, *_ = _run(
        monkeypatch, tmp_path, actions,
        task=f"Change app.py value to 2. Run {command}.",
        allowed_commands=(command, "python -m py_compile app.py", demo), limit=len(actions),
    )
    assert (tmp_path / "app.py").read_text() == "value = 2\n"
    assert any(item.get("event") == "verification_result" and item.get("status") == "passed" for item in trace)
    assert result.status.value == "completed", (result.error, result.final_text)
    assert state["verification_generation"] == state["mutation_generation"]
    assert state["mutation_generation"] == 1
    assert not list(local_tmp.glob("nzcoder_txn_*"))
    assert len(requests) <= len(actions)
    if failed_demo_first:
        observed = [item for item in trace if item.get("event") == "tool_call"
                    and item.get("input", {}).get("command") == demo]
        assert len(observed) == 1
        assert observed[0]["executed"] is True
        assert observed[0]["command_failed"] is True


def test_user_file_named_like_internal_backup_still_advances_generation(tmp_path):
    folder = tmp_path / ".tmp/nzcoder_txn_user"
    folder.mkdir(parents=True)
    target = folder / "app.py"
    target.write_text("value = 1\n")
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        command = "printf 'value = 2\\n' > .tmp/nzcoder_txn_user/app.py"
        _execute(host, command)
        assert host.runtime_state.mutation_generation == 1
        assert ".tmp/nzcoder_txn_user/app.py" in host.runtime_state.last_mutation_paths
        assert host.verification_runtime.status()["verification_needed"] is True
