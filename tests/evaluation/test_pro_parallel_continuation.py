"""并行 Pro 准备续跑只携带剩余额度；模型边界不出站。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
from types import SimpleNamespace
import time

import pytest

from nz_coder.evaluation.model_relay import InputAccounting, RelayBinding, RelayLedger, RelayLimits

spec = importlib.util.spec_from_file_location("parallel_pro", Path(__file__).resolve().parents[2]
    / "docs/evidence/swebench-pro-20-batch2-2026-10-11/run-parallel.py")
parallel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parallel)


def closed_source(tmp_path, *, pending=False, unknown=False):
    tasks = [{"instance_id": "instance_example__project-" + letter * 40} for letter in "abc"]
    manifest = {"tasks": tasks}
    grant = {"purpose": "swebench-pro-v2", "model": "deepseek-flash", "account": "offline-test",
        "scope": [task["instance_id"] for task in tasks], "instance_manifest_sha256": "a" * 64,
        "authorization_text": "offline fee test", "input_mode": "empirical", "empirical_risk_accepted": True,
        "contract_id": "offline", "cost_currency": "CNY", "cost_limit": 50,
        "valid_from": time.time() - 1, "valid_until": time.time() + 60,
        "prices_cny_per_million": {"input": 2, "cached_input": .04, "output": 8},
        "provider_output_reservation_tokens": 393216}
    ledger = RelayLedger(tmp_path / "ledger.jsonl", RelayLimits(output_total=2000000), empirical_authorization=grant)
    for number, task in enumerate(tasks[:2], 1):
        binding = RelayBinding("swebench-pro-v2", task["instance_id"], f"b2-{number:02}", "main", 40,
                               "deepseek-flash", 393216, time.monotonic() + 60, False)
        payload = {"model": binding.model, "messages": [{"role": "user", "content": "offline"}]}
        raw = json.dumps(payload).encode()
        accounting = InputAccounting(None, "offline", contract_id="offline",
                                     payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=10000)
        attempt = ledger.reserve(binding, str(number), raw, payload, accounting)
        if pending and number == 2:
            continue
        if unknown and number == 2:
            ledger.settle(attempt, status="request_timeout")
        else:
            ledger.settle(attempt, status="complete", usage={"prompt_tokens": 1000, "completion_tokens": 2,
                "total_tokens": 1002, "prompt_cache_hit_tokens": 50}, finish_reason="stop")
    summary = {"estimated_cost_occupied_cny": ledger.cost_occupied_cny, "accounting_failures": ledger.failed_contracts,
        "tasks": [{"number": i, "instance_id": task["instance_id"], "attempt": 1 if i < 3 else 0,
                   "resolved": False if i == 2 else None} for i, task in enumerate(tasks, 1)]}
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    ledger.close()
    return manifest, summary


def test_preparation_retry_carries_real_ledger_fee_and_excludes_paid_failure(tmp_path):
    manifest, summary = closed_source(tmp_path)
    plan = parallel.preparation_remainder(tmp_path, manifest)
    assert [number for number, _task in plan["tasks"]] == [3]
    assert plan["used_cny"] == pytest.approx(summary["estimated_cost_occupied_cny"])
    assert plan["remaining_cny"] + plan["used_cny"] == pytest.approx(50)
    assert plan["remaining_requests"] == 918
    assert plan["source_ledger_sha256"] == hashlib.sha256((tmp_path / "ledger.jsonl").read_bytes()).hexdigest()


@pytest.mark.parametrize("pending,unknown", [(True, False), (False, True)])
def test_preparation_retry_cannot_reset_pending_or_failed_accounting(tmp_path, pending, unknown):
    manifest, _summary = closed_source(tmp_path, pending=pending, unknown=unknown)
    with pytest.raises(RuntimeError, match="previous_stage_not_settled_or_accounting_stopped"):
        parallel.preparation_remainder(tmp_path, manifest)


def test_preparation_retry_rejects_changed_previous_fee_summary(tmp_path):
    manifest, summary = closed_source(tmp_path)
    summary["estimated_cost_occupied_cny"] = 0
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(RuntimeError, match="previous_fee_summary_mismatch"):
        parallel.preparation_remainder(tmp_path, manifest)


def test_long_evidence_path_binds_real_relays_without_changing_mount_identity(tmp_path, monkeypatch):
    case = tmp_path / ("evidence-" + "x" * 90) / "case"
    for name in ("input", "sockets", "workspace", "result", "home", "tmp", "driver"):
        (case / name).mkdir(parents=True, exist_ok=True)
    (case / "input/run.json").write_text('{"provider_default_output": true}')
    slot = case.parent
    (slot / "identity.json").write_text('{"instance_id": "offline"}')
    monkeypatch.setattr(parallel.pro, "OUT", tmp_path)
    monkeypatch.setattr(parallel.pro, "command", lambda *args, **kwargs: None)
    mounts = []

    class DockerProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            for arg in argv:
                if arg.startswith("type=bind,source="):
                    source, target = arg.removeprefix("type=bind,source=").split(",target=", 1)
                    mounts.append((source, target.split(",")[0]))
                    if target.startswith("/workspace"):
                        assert Path(source).samefile(case / "workspace")
                    if target.startswith("/model"):
                        assert (Path(source) / "main.sock").exists()
                        assert (Path(source) / "auxiliary.sock").exists()
                        assert len(str(Path(source) / "auxiliary.sock").encode()) < 108

        def poll(self):
            return 0

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(parallel.pro.subprocess, "Popen", DockerProcess)
    ledger = RelayLedger(tmp_path / "offline-ledger.jsonl", RelayLimits())
    run = getattr(parallel, "run_case_with_short_path", parallel.pro.run_case)
    try:
        run(slot, case, "offline-image", ledger, lambda *args: None,
            smoke=True, upstream="http://127.0.0.1:9/v1/chat/completions")
        assert ledger.attempts == {}
        assert mounts
        assert json.loads((slot / "agent-exit.json").read_text())["returncode"] == 0
    finally:
        ledger.close()


def test_combined_closed_phases_count_second_fee_and_exclude_paid_task(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    manifest, initial = closed_source(first)
    second = tmp_path / "second"
    second.mkdir()
    grant = json.loads((first / "ledger.jsonl").read_text().splitlines()[0])["fee_authorization"]
    grant["scope"] = [manifest["tasks"][2]["instance_id"]]
    grant["cost_limit"] = 50 - initial["estimated_cost_occupied_cny"]
    ledger = RelayLedger(second / "ledger.jsonl", RelayLimits(output_total=2000000), empirical_authorization=grant)
    binding = RelayBinding("swebench-pro-v2", grant["scope"][0], "b2-03", "main", 40,
                           "deepseek-flash", 393216, time.monotonic() + 60, False)
    payload = {"model": binding.model, "messages": [{"role": "user", "content": "offline"}]}
    raw = json.dumps(payload).encode()
    accounting = InputAccounting(None, "offline", contract_id="offline",
        payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=10000)
    attempt = ledger.reserve(binding, "third", raw, payload, accounting)
    ledger.settle(attempt, status="complete", usage={"prompt_tokens": 1000,
        "completion_tokens": 2, "total_tokens": 1002, "prompt_cache_hit_tokens": 50}, finish_reason="stop")
    fee = ledger.cost_occupied_cny
    (second / "summary.json").write_text(json.dumps({"tasks": [{"number": 3,
        "instance_id": grant["scope"][0], "attempt": 1, "resolved": False}],
        "estimated_cost_occupied_cny": fee, "accounting_failures": {}}))
    ledger.close()
    plan = parallel.combine_closed_sources([first, second], manifest, tmp_path / "combined")
    assert plan["tasks"] == []
    assert plan["used_cny"] == pytest.approx(initial["estimated_cost_occupied_cny"] + fee)
    assert plan["remaining_requests"] == 917


def test_forty_task_continuation_uses_current_authorized_request_limit(tmp_path):
    manifest, summary = closed_source(tmp_path)
    manifest.update(cost_limit=50, request_limit=1840)
    plan = parallel.preparation_remainder(tmp_path, manifest)
    assert plan["remaining_requests"] == 1838
    assert plan["remaining_cny"] == pytest.approx(50-summary["estimated_cost_occupied_cny"])


def test_additional_grant_can_include_sdk_launch_without_paid_request(tmp_path):
    manifest, summary = closed_source(tmp_path)
    # 第三题虽启动 SDK，但所有请求在本地被拒绝；第二题有真实付费请求。
    summary["tasks"][2]["attempt"] = 1
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    assert parallel.preparation_remainder(tmp_path, manifest)["tasks"] == []
    plan = parallel.preparation_remainder(tmp_path, manifest, include_unbilled_sdk=True)
    assert [number for number, _ in plan["tasks"]] == [3]
    assert plan["used_cny"] == pytest.approx(summary["estimated_cost_occupied_cny"])


@pytest.mark.parametrize("previous_paid", [False, True])
def test_supplement_export_replaces_only_zero_model_records(tmp_path, previous_paid):
    root = tmp_path / "repo"
    out = tmp_path / "private"
    doc = root / "docs/evidence/swebench-pro-40-batch3-2026-10-11"
    old = doc / "preparation-retry-attempts/b3-31"
    old.mkdir(parents=True)
    instance = "instance_example__project-" + "a" * 40
    (old / "task-outcome.json").write_text(json.dumps({"instance_id": instance,
        "actual_agent_attempt": 1, "main_model_was_called": previous_paid}))
    (doc / "task-manifest.json").write_text(json.dumps({"tasks": [{"instance_id": instance}]}))
    stage = out / "supplement"
    slot = stage / "b3-31"
    slot.mkdir(parents=True)
    (stage / "ledger.jsonl").write_text("")
    (slot / "report.json").write_text(json.dumps({"instance_id": instance,
        "attempt": 0, "status": "blocked"}))
    source = Path(__file__).resolve().parents[2] / "docs/evidence/swebench-pro-40-batch3-2026-10-11/publish.py"
    script = source.read_text().replace("/home/pyh/nzcoder", str(root)).replace(
        "/home/pyh/.codex/artifacts/nz-swebench-pro-v2-40-batch3-2026-10-11", str(out))
    exporter = tmp_path / "publish.py"
    exporter.write_text(script)
    result = subprocess.run([sys.executable, str(exporter), "supplement"], capture_output=True, text=True)
    if previous_paid:
        assert result.returncode != 0
        assert "refuse_to_replace_paid_attempt" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        current = json.loads((doc / "current-outcomes.json").read_text())
        assert current[0]["record_root"] == str(slot)
        assert current[0]["main_model_was_called"] is False


def test_real_docker_prepare_uses_authorized_temporary_workspace(tmp_path):
    if subprocess.run(["docker", "image", "inspect", "python:3.12-slim"], capture_output=True).returncode:
        pytest.skip("local Python image unavailable; test never downloads an image")
    source = tmp_path / "source"
    source.mkdir()
    (source / "example.txt").write_text("initial bytes\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=offline", "-c",
                    "user.email=offline@example.invalid", "commit", "-qm", "initial"], check=True)
    name = "nz-pro-prepare-test-" + str(time.time_ns())
    image = name + ":local"
    subprocess.run(["docker", "create", "--name", name, "python:3.12-slim"], check=True, capture_output=True)
    try:
        subprocess.run(["docker", "cp", str(source), name + ":/app"], check=True)
        subprocess.run(["docker", "commit", name, image], check=True, capture_output=True)
        slot = tmp_path / "slot"
        slot.mkdir()
        with tempfile.TemporaryDirectory(prefix="nz-pro-test-", dir="/dev/shm") as memory:
            case = parallel.pro.prepare_case(slot, image, {}, True, temporary_workspace_root=Path(memory))
            assert (case / "workspace").is_symlink()
            assert (case / "workspace/example.txt").read_text() == "initial bytes\n"
            assert (case / "workspace").resolve().is_relative_to(Path(memory))
            assert not (case / "result").is_symlink()
            assert (slot / "temporary-workspace.json").exists()
            (case / "result/runtime.jsonl").write_text('{"event":"run_end"}\n')
            parallel.pro.archive_case(slot, case, {"instance_id": "offline"}, "memory-test", None,
                lambda **kwargs: SimpleNamespace(bundle_path=tmp_path / "projection"))
            receipt = json.loads((slot / "archive-receipt.json").read_text())
            bundle = Path(receipt["bundle"])
            assert hashlib.sha256(bundle.read_bytes()).hexdigest() == receipt["sha256"]
            with tarfile.open(bundle) as archive:
                assert archive.extractfile("result/runtime.jsonl").read() == b'{"event":"run_end"}\n'
            assert not (case / "workspace").exists()
            assert list(Path(memory).iterdir()) == []
            assert (case / "result/runtime.jsonl").exists()
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
        subprocess.run(["docker", "image", "rm", image], check=False, capture_output=True)


def test_next_forty_selection_excludes_both_closed_batches():
    candidates = ["task-" + str(i) for i in range(100)]
    sums = {"tasks/"+i+"/instruction.md": "a"*64 for i in candidates}
    earlier = [{"selection":{"seed":"fixed"}, "tasks":[{"instance_id":i} for i in candidates[:20]]},
               {"tasks":[{"instance_id":i} for i in candidates[20:40]]}]
    selected, excluded, seed = parallel.select_tasks(sums, earlier, 40)
    assert len(selected) == 40 and len(set(selected)) == 40
    assert excluded == set(candidates[:40]) and not set(selected) & excluded
    assert seed == "fixed"
    assert selected == sorted(candidates[40:], key=lambda i: hashlib.sha256((seed+"\n"+i).encode()).hexdigest())[:40]
