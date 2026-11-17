"""中型台账发现的普通 unittest discovery 验收与真实原生收尾回归。"""
from __future__ import annotations

import pytest

from nz_coder.runtime.verification.verification_contract import extract_verification_contracts
from test_node_verification_runtime import _run, _tool


TARGET = "python -m unittest tests.test_orders -v"
DISCOVERY = "python -m unittest discover -s tests -v"
TASK = (
    "Validate the saved SQLite inventory and order ledger. Both exact acceptance commands below are required.\n"
    "python -m unittest tests.test_orders -v\n"
    "python -m unittest discover -s tests -v\n"
    "Report both real results. Do not modify any files."
)


def test_original_multiline_request_preserves_both_commands_without_inventing_discovery_targets():
    contracts = extract_verification_contracts(TASK)
    assert [item.command for item in contracts] == [TARGET, DISCOVERY]
    assert contracts[0].targets == ("tests.test_orders",)
    assert contracts[1].targets == ()


@pytest.mark.parametrize("command", [
    "python -m unittest discover -s tests -v",
    "python3 -m unittest discover --start-directory 'test suite' --pattern 'check_*.py' --top-level-directory . --verbose",
    "python -m unittest discover -s tests -p 'test_*.py' -t . -q",
    "python -m unittest discover 'test suite' 'check_*.py' . -v",
    "python -m unittest discover --start-directory=tests --failfast",
])
def test_natural_discovery_keeps_complete_valid_argument_binding(command):
    contracts = extract_verification_contracts("Run " + command)
    assert len(contracts) == 1
    assert contracts[0].command == command
    assert contracts[0].targets == ()


@pytest.mark.parametrize("text", [
    "Run python -m unittest discover --unknown tests",
    "Run python -m unittest discover -s",
    "Run python -m unittest discover -s --verbose",
    "Run python -m unittest discover --start-directory=",
    "Run python -m unittest discover -s tests && python -m unittest tests.test_orders",
    "Run python -m unittest discover -s tests | tail -5",
    "Run env MODE=test python -m unittest discover -s tests",
    "Run MODE=test python -m unittest discover -s tests",
    "Run bash -c 'python -m unittest discover -s tests'",
    "Do not run python -m unittest discover -s tests -v",
    "不要运行 python -m unittest discover -s tests -v",
    "Do not run the following checks:\n```sh\npython -m unittest discover -s tests -v\n```",
])
def test_discovery_does_not_extract_invalid_wrapped_composed_or_negative_commands(text):
    assert extract_verification_contracts(text) == ()


@pytest.fixture
def ledger_checks(monkeypatch, tmp_path):
    for name in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED",
                 "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(name, "0")
    (tmp_path / "app.py").write_text(
        '"""台账金额计算的最小真实业务边界。"""\n'
        "def stock_value(quantity, cents):\n    return quantity * cents\n"
        "def sale_total(lines):\n    return sum(stock_value(quantity, cents) for quantity, cents in lines)\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("")
    (tmp_path / "tests/test_orders.py").write_text(
        "import unittest\nfrom app import stock_value\n"
        "class Orders(unittest.TestCase):\n"
        "    def test_integer_cents(self):\n"
        "        self.assertEqual(stock_value(3, 125), 375)\n"
    )
    (tmp_path / "tests/test_reports.py").write_text(
        "import unittest\nfrom app import sale_total\n"
        "class Reports(unittest.TestCase):\n"
        "    def test_mixed_order_lines(self):\n"
        "        self.assertEqual(sale_total([(2, 125), (1, 90)]), 340)\n"
    )
    return tmp_path


def _user_checks(result):
    stages = result.metadata["verification"]["verification_pipeline"]["stages"]
    return {item["command"]: item for stage in stages for item in stage["commands"]
            if item.get("automation_provenance") == "user_declared"}


def test_native_target_pass_cannot_complete_without_declared_discovery(monkeypatch, ledger_checks):
    result, state, trace, *_ = _run(monkeypatch, ledger_checks, [
        [_tool("read_file", path="app.py")],
        [_tool("bash", command=TARGET)],
        "The order check passed; finish the validation now.",
    ], task=TASK, allowed_commands=(TARGET, DISCOVERY))
    assert result.status.value != "completed"
    assert state["task_contract"]["acceptance_commands"] == [TARGET, DISCOVERY]
    checks = _user_checks(result)
    assert set(checks) == {TARGET, DISCOVERY}
    assert checks[TARGET]["status"] == "passed"
    assert checks[DISCOVERY]["required"] is True
    assert checks[DISCOVERY]["status"] == "pending"
    assert not any(row.get("event") == "tool_call" and row.get("input", {}).get("command") == DISCOVERY
                   and row.get("executed") for row in trace)


def test_native_both_real_checks_pass_and_remain_user_declared(monkeypatch, ledger_checks):
    result, state, trace, *_ = _run(monkeypatch, ledger_checks, [
        [_tool("read_file", path="app.py")],
        [_tool("bash", command=TARGET)],
        [_tool("bash", command=DISCOVERY)],
        "Both declared ledger checks passed.",
    ], task=TASK, allowed_commands=(TARGET, DISCOVERY))
    assert result.status.value == "completed", (result.error, result.final_text)
    assert state["task_contract"]["acceptance_commands"] == [TARGET, DISCOVERY]
    checks = _user_checks(result)
    assert set(checks) == {TARGET, DISCOVERY}
    assert all(item["required"] and item["status"] == "passed" for item in checks.values())
    assert checks[DISCOVERY]["cwd"] == str(ledger_checks)
    assert checks[DISCOVERY]["targets"] == []
    calls = [row for row in trace if row.get("event") == "tool_call"
             and row.get("name") == "bash" and row.get("input", {}).get("command") == DISCOVERY]
    assert len(calls) == 1
    assert calls[0]["executed"] and not calls[0]["dispatch_failed"] and not calls[0]["command_failed"]
    assert "Ran 2 tests" in calls[0]["output"]


def test_native_narrower_discovery_does_not_settle_original_full_scope(monkeypatch, ledger_checks):
    narrower = "python -m unittest discover -s tests -p 'test_orders.py' -v"
    result, _, trace, *_ = _run(monkeypatch, ledger_checks, [
        [_tool("read_file", path="app.py")],
        [_tool("bash", command=TARGET)],
        [_tool("bash", command=narrower)],
        "The selected order discovery passed; finish now.",
    ], task=TASK, allowed_commands=(TARGET, DISCOVERY, narrower))
    assert result.status.value != "completed"
    checks = _user_checks(result)
    assert checks[DISCOVERY]["required"] is True
    assert checks[DISCOVERY]["status"] == "pending"
    actual = [row for row in trace if row.get("event") == "tool_call"
              and row.get("input", {}).get("command") == narrower]
    assert len(actual) == 1 and actual[0]["executed"] and not actual[0]["command_failed"]
    assert "Ran 1 test" in actual[0]["output"]
