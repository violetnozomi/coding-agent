"""Tests for run-local deterministic verification contracts."""
from __future__ import annotations

import pytest

from nz_coder.runtime.verification.verification_contract import (
    VerificationContract,
    extract_verification_contract,
)


def test_extracts_python_pytest_command_before_chinese_prose():
    contract = extract_verification_contract(
        "完成后必须运行 python -m pytest -q cron_engine/tests，并根据真实结果总结。"
    )

    assert contract is not None
    assert contract.command == "python -m pytest -q cron_engine/tests"
    assert contract.targets == ("cron_engine/tests",)


def test_extracts_multiple_relative_pytest_targets():
    contract = extract_verification_contract(
        "Run `pytest -q tests/test_parser.py tests/test_cli.py` when done."
    )

    assert contract is not None
    assert contract.command == "pytest -q tests/test_parser.py tests/test_cli.py"
    assert contract.targets == (
        "tests/test_parser.py",
        "tests/test_cli.py",
    )


def test_extracts_english_command_before_following_sentence():
    contract = extract_verification_contract(
        "Run the exact acceptance command python -m pytest -q cron_engine/tests. "
        "Do not claim completion without current evidence."
    )

    assert contract is not None
    assert contract.command == "python -m pytest -q cron_engine/tests"
    assert contract.targets == ("cron_engine/tests",)


@pytest.mark.parametrize(
    "text",
    [
        "Run pytest when done.",
        "Run pytest -q /tmp/tests.",
        "Run pytest -q ../other/tests.",
        "Run pytest -q tests | tail -1.",
        "Run pytest -q tests && echo done.",
        "Run pytest -q tests > result.txt.",
    ],
)
def test_rejects_unbounded_or_composed_commands(text):
    assert extract_verification_contract(text) is None


def test_contract_runs_once_per_mutation_generation_and_rearms_after_edit():
    contract = VerificationContract(
        command="pytest -q tests",
        targets=("tests",),
    )

    assert contract.is_due(zone="green", has_diff=True, mutation_generation=1) is False
    assert contract.is_due(zone="yellow", has_diff=False, mutation_generation=1) is False
    assert contract.is_due(zone="yellow", has_diff=True, mutation_generation=1) is False
    assert contract.is_due(zone="orange", has_diff=True, mutation_generation=1) is False
    assert contract.is_due(zone="red", has_diff=True, mutation_generation=1) is True
    assert contract.is_due(zone="completion", has_diff=True, mutation_generation=1) is True

    contract.record_attempt(1, passed=False, output="1 failed")

    assert contract.is_due(zone="red", has_diff=True, mutation_generation=1) is False
    assert contract.is_due(zone="red", has_diff=True, mutation_generation=2) is True


def test_contract_round_trip_preserves_attempt_evidence():
    contract = VerificationContract(
        command="python -m pytest -q tests",
        targets=("tests",),
    )
    contract.record_attempt(
        3,
        passed=True,
        output="42 passed",
        source="runtime",
        zone="completion",
    )

    restored = VerificationContract.from_dict(contract.to_dict())

    assert restored == contract


@pytest.mark.parametrize(
    "command",
    [
        "python   -m pytest -q cron_engine/tests",
        "python -m pytest -q 'cron_engine/tests'",
        'python -m pytest -q "cron_engine/tests"',
    ],
)
def test_contract_matches_token_equivalent_command(command):
    contract = VerificationContract(
        command="python -m pytest -q cron_engine/tests",
        targets=("cron_engine/tests",),
    )

    assert contract.matches_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q cron_engine/tests | tail -1",
        "python -m pytest -q cron_engine/tests > result.txt",
        "python -m pytest -q cron_engine/tests && echo done",
        "python -m pytest -q other/tests",
        "python -m pytest -q cron_engine/tests tests/extra",
        'python -m pytest -q "cron_engine/tests',
    ],
)
def test_contract_rejects_non_equivalent_command(command):
    contract = VerificationContract(
        command="python -m pytest -q cron_engine/tests",
        targets=("cron_engine/tests",),
    )

    assert contract.matches_command(command) is False
