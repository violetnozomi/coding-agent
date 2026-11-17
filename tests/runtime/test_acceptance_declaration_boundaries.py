"""用户的完整验收声明必须保留来源、原文与独立检查身份。"""
from __future__ import annotations

import pytest

from nz_coder.runtime.verification import verification_contract as contracts
from nz_coder.runtime.agent.task_contract import derive_task_contract, parse_task_contract


@pytest.mark.parametrize("text", [
    "Use the installed local checks. Run node --test literal.test.cjs.",
    "Continue the fix. Do not modify tests. Run node --test literal.test.cjs.",
    "Modify literal.test.cjs and run node --test literal.test.cjs.",
    "Checks:\n- Run node --test literal.test.cjs.",
    "Run the following:\n```sh\nnode --test literal.test.cjs\n```",
])
def test_clear_instruction_retains_the_complete_command(text):
    result = contracts.extract_verification_contract(text)
    assert result is not None
    assert result.command == "node --test literal.test.cjs"


@pytest.mark.parametrize("text", [
    "Do not run the following command:\n`node --test literal.test.cjs`.",
    "Previously ran:\n`node --test literal.test.cjs`.",
    "Example only:\n```sh\nnode --test literal.test.cjs\n```",
    "示例：\n```sh\nnode --test literal.test.cjs\n```",
    "Explain the command `node --test literal.test.cjs` without running it.",
    "Do not run these commands:\n`node --test first.test.cjs`\n`node --test second.test.cjs`",
])
def test_examples_and_negative_headers_do_not_authorize_execution(text):
    assert contracts.extract_verification_contract(text) is None


@pytest.mark.parametrize("text", [
    "Run node --test first.test.cjs. Run node --test second.test.cjs.",
    "Run node --test first.test.cjs.\nRun node --test second.test.cjs.",
    "Acceptance command: `node --test first.test.cjs`.\nAcceptance command: `node --test second.test.cjs`.",
])
def test_all_independent_commands_keep_source_order(text):
    assert tuple(item.command for item in contracts.extract_verification_contracts(text)) == (
        "node --test first.test.cjs", "node --test second.test.cjs",
    )


def test_new_positive_instruction_ends_example_scope():
    text = "Example only:\n```sh\nnode --test old.test.cjs\n```\nRun node --test current.test.cjs."
    result = contracts.extract_verification_contract(text)
    assert result is not None and result.command == "node --test current.test.cjs"


@pytest.mark.parametrize("text", [
    "Run env -C . node --test literal.test.cjs.",
    "Run TEST_MODE=run node --test literal.test.cjs.",
    "Run uv run python -m pytest tests/test_api.py.",
    'Run printf "Run node --test literal.test.cjs".',
    'Run printf ". Run node --test literal.test.cjs".',
    "Run printf '. Run node --test literal.test.cjs'.",
    "Run printf '`node --test literal.test.cjs`'.",
    'Run printf "Acceptance command: `touch arbitrary-file`".',
    'Run printf "escaped \\"`node --test literal.test.cjs`".',
    "Run printf harmless; node --test literal.test.cjs.",
    "Run node --test literal.test.cjs; printf harmless.",
])
def test_shell_data_is_not_a_second_instruction(text):
    assert contracts.extract_verification_contract(text) is None


def test_contract_preserves_all_commands_and_quoted_whitespace(tmp_path):
    commands = tuple(f"LABEL='a  b' node --test test_{index}.test.cjs" for index in range(12))
    contract = derive_task_contract("Fix behavior.", acceptance_commands=commands, workspace=tmp_path)
    assert contract.acceptance_commands == commands
    assert parse_task_contract(contract.to_dict()).acceptance_commands == commands
    long_command = "node --test " + "x" * 2200 + ".test.cjs"
    assert parse_task_contract({"acceptance_commands": [long_command]}).acceptance_commands == (long_command,)


@pytest.mark.parametrize("command", [
    'node --test "foo and bar.test.cjs"',
    'node --test "foo,bar.test.cjs"',
    "python -m pytest -k 'this and that' tests/test_api.py",
])
def test_quoted_arguments_are_command_data_not_sentence_boundaries(command):
    result = contracts.extract_verification_contract("Run " + command + ".")
    assert result is not None
    assert result.command == command
