"""The acceptance-only ledger must gate real requests before network dispatch."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(__import__("os").name != "posix", reason="R1 metering uses Linux flock")


def meter():
    path = Path(__file__).parent / "provider" / "r1_provider.py"
    assert path.exists(), "The before-dispatch budget gate is missing"
    spec = importlib.util.spec_from_file_location("r1_provider", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_budget_rejects_without_appending_or_refunding(tmp_path):
    reserve = meter().reserve
    ledger = tmp_path / "ledger.jsonl"
    reserve(ledger, 3_000_000, 4096)
    reserve(ledger, 3_000_000, 4096)
    reserve(ledger, 3_000_000, 4096)
    before = ledger.read_bytes()
    with pytest.raises(RuntimeError, match="budget"):
        reserve(ledger, 3_000_000, 4096)
    assert ledger.read_bytes() == before
    assert len(before.splitlines()) == 3


def test_budget_parallel_reservations_cannot_overdraw(tmp_path):
    reserve = meter().reserve
    ledger = tmp_path / "ledger.jsonl"

    def attempt(_):
        try:
            reserve(ledger, 3_000_000, 4096)
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 3
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert sum(row["reserved_usd"] for row in rows) < 5


def test_adapter_rejects_unknown_costs_and_endpoint_before_dispatch(tmp_path, monkeypatch):
    module = meter()
    monkeypatch.setenv("NZ_R1_LEDGER", str(tmp_path / "ledger.jsonl"))
    with pytest.raises(ValueError, match="endpoint"):
        module.factory(provider_name="r1-metered", api_key="fake", base_url="https://other.invalid")
    adapter = module.factory(provider_name="r1-metered", api_key="fake", base_url="https://api.deepseek.com")
    with pytest.raises(ValueError, match="model"):
        adapter.create_completion(None, model="unknown", messages=[], max_tokens=10)
    with pytest.raises(ValueError, match="output"):
        adapter.create_completion(None, model="deepseek-v4-flash", messages=[], max_tokens=9999)


def test_full_ledger_prevents_real_client_creation_call(tmp_path, monkeypatch):
    module = meter()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"reserved_usd":5}\n')
    monkeypatch.setenv("NZ_R1_LEDGER", str(ledger))
    adapter = module.factory(provider_name="r1-metered", api_key="fake", base_url="https://api.deepseek.com")
    # None is intentional: reaching client.chat would fail instead of budget rejection.
    with pytest.raises(RuntimeError, match="budget"):
        adapter.create_completion(None, model="deepseek-v4-flash", messages=[{"role":"user", "content":"hello"}], max_tokens=4096)
