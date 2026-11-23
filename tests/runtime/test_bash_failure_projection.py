"""用真实子进程检查非零退出在模型消息和已保存会话中的分类。"""
import json

import pytest

from nz_coder.permissions import PermissionManager
from nz_coder.protocol.message_schema import PARTS_KEY, attach_message_identity, message_records
from nz_coder.runtime.conversation.message_projection import project_provider_messages
from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.state.sessions import load_session, save_session, scoped_session
from nz_coder.tools import bash  # noqa: F401  # 注册内置命令工具。


def _run_and_restore(workspace, command):
    session_id = "bash-failure-projection"
    call = {"id": "check", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}
    assistant = {"role": "assistant", "content": "", "tool_calls": [call]}
    attach_message_identity(assistant, session_id=session_id)
    messages = [assistant]
    processor = SessionProcessor(assistant)
    processor.register_tool_calls([call])
    processor.start_tools([call])
    projection = ToolProjectionContext(
        signal_from_metadata=lambda _: None,
        record_result=lambda result: result.dispatch_failed,
        trace_result=lambda *args, **kwargs: None,
        after_result=lambda *args: None,
    )
    with scoped_workdir(workspace), scoped_session(session_id):
        executor = ToolExecutor(PermissionManager("auto", asker=lambda *_: True))
        result = executor.execute_one(call, 0)
        ProductionToolResultProjector().consume(projection, [(0, call, result)], messages, processor=processor)
        save_session(messages, session_id=session_id, activate=False)
        restored = load_session(session_id)["messages"]
    assert result.executed and not result.dispatch_failed and result.command_failed
    assert processor.process_result() == "continue"
    state = next(part["state"] for part in restored[0][PARTS_KEY] if part["type"] == "tool")
    assert state["status"] == "error"
    public = next(part["state"] for part in message_records(restored, session_id)[0]["parts"] if part["type"] == "tool")
    assert public["error"] == state["error"]
    assert public["output"] == state["error"]["message"]
    model_result = next(msg for msg in project_provider_messages(restored) if msg["role"] == "tool")
    assert model_result["tool_call_id"] == call["id"]
    return state["error"], model_result["content"], result.execution_facts[0]


def test_invalid_pytest_argument_is_a_command_error(tmp_path):
    error, details, receipt = _run_and_restore(tmp_path, "pytest --不存在的参数")
    assert error["code"] == "command_invalid_arguments"
    assert error["metadata"]["exit_code"] == receipt.exit_code == 4
    assert error["metadata"]["tests_ran"] is False
    assert receipt.test_count == 0
    assert "参数无效，测试未运行" in error["message"]
    assert error["message"] in details and "unrecognized arguments" in details


@pytest.mark.parametrize("runner", ["pytest", "python -m pytest"])
def test_failed_pytest_cases_keep_counts_and_assertions(tmp_path, runner):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_cache.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('slot', range(8))\n"
        "def test_cache(slot):\n    assert slot < 6\n",
        encoding="utf-8",
    )
    error, details, receipt = _run_and_restore(tmp_path, f"{runner} tests/test_cache.py -q")
    assert error["code"] == "test_failed"
    assert error["metadata"]["exit_code"] == receipt.exit_code == 1
    assert error["metadata"]["tests_ran"] is True
    assert error["metadata"]["failed"] == 2 and error["metadata"]["passed"] == 6
    assert receipt.test_count == 8
    assert "2 项未通过，6 项通过" in error["message"]
    assert error["message"] in details
    assert "test_cache[6]" in details and "test_cache[7]" in details
    assert "assert 6 < 6" in details and "assert 7 < 6" in details


def test_assertion_message_cannot_turn_a_failed_run_into_zero_tests(tmp_path):
    (tmp_path / "test_message.py").write_text(
        "def test_message():\n    assert False, 'no tests ran'\n", encoding="utf-8",
    )
    error, details, receipt = _run_and_restore(tmp_path, "pytest -q test_message.py --color=yes")
    assert receipt.test_count == 1
    assert error["metadata"]["failed"] == 1 and error["metadata"]["passed"] == 0
    assert "no tests ran" in details


def test_missing_pytest_target_is_not_an_invalid_option_or_a_case_failure(tmp_path):
    error, details, receipt = _run_and_restore(tmp_path, "pytest -q missing_test_file.py")
    assert error["code"] == "command_usage_error"
    assert error["metadata"]["exit_code"] == receipt.exit_code == 4
    assert "file or directory not found" in details


def test_quiet_pytest_keeps_failed_case_without_guessing_totals(tmp_path):
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert 1 == 2\n", encoding="utf-8")
    error, details, receipt = _run_and_restore(tmp_path, "pytest -qq test_one.py")
    assert error["code"] == "test_failed"
    assert error["metadata"] == {"runner": "pytest", "exit_code": 1, "tests_ran": True}
    assert receipt.test_count is None
    assert "test_one.py::test_one" in details


def test_deselected_pytest_cases_are_not_failures(tmp_path):
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    error, details, receipt = _run_and_restore(tmp_path, "pytest -q -k missing_name")
    assert error["code"] == "no_tests_collected"
    assert error["metadata"] == {"runner": "pytest", "exit_code": 5, "tests_ran": False}
    assert receipt.test_count == 0
    assert "1 deselected" in details


def test_pytest_setup_error_is_separate_from_assertion_failure(tmp_path):
    (tmp_path / "test_setup.py").write_text("def test_one(missing_fixture):\n    pass\n", encoding="utf-8")
    error, details, receipt = _run_and_restore(tmp_path, "pytest -q test_setup.py")
    assert error["code"] == "test_execution_error"
    assert error["metadata"] == {"runner": "pytest", "exit_code": 1, "errors": 1}
    assert receipt.test_count is None
    assert "fixture 'missing_fixture' not found" in details


@pytest.mark.parametrize("prefix", ["", "pytest --version && "])
def test_shell_exit_with_summary_text_is_not_a_failed_test(tmp_path, prefix):
    command = prefix + '''python -c "print('2 failed, 6 passed in 0.01s'); raise SystemExit(1)"'''
    error, details, receipt = _run_and_restore(tmp_path, command)
    assert error["code"] == "command_failed"
    assert error["metadata"] == {"exit_code": receipt.exit_code} == {"exit_code": 1}
    assert receipt.test_count is None
    assert "2 failed, 6 passed" in details
