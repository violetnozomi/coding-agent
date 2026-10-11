"""第二批固定 20 题：复用上一批执行入口，两个任务共用一份宿主账本。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import tomllib

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
DOC = Path(__file__).resolve().parent
OLD_DOC = DOC.parent / "swebench-pro-20-2026-10-11"
OLD = Path("/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-2026-10-11")
OUT = OLD.with_name("nz-swebench-pro-v2-20-batch2-2026-10-11")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pro = load("pro_batch2_base", OLD_DOC / "run-pro.py")
continuation = load("pro_batch2_environment", OLD_DOC / "continue-unattempted.py")
pro.OUT, pro.DOC = OUT, DOC
continuation.OUT, continuation.pro = OUT, pro


def prepare():
    OUT.mkdir(exist_ok=False)
    old_manifest = json.loads((OLD / "task-manifest.json").read_text())
    checksums = {}
    for line in (OLD / "official-SHA256SUMS").read_text().splitlines():
        digest, path = line.split(maxsplit=1)
        checksums[path.lstrip("*").removeprefix("./")] = digest
    candidates = sorted({path.split("/")[1] for path in checksums
                         if path.startswith("tasks/") and path.endswith("/instruction.md")})
    excluded = {row["instance_id"] for row in old_manifest["tasks"]}
    seed = old_manifest["selection"]["seed"]
    ordered = sorted(candidates, key=lambda task: hashlib.sha256((seed + "\n" + task).encode()).hexdigest())
    selected = [task for task in ordered if task not in excluded][:20]
    manifest = {"benchmark": "SWE-bench Pro V2 public", "batch": 2,
        "official_source_commit": old_manifest["official_source_commit"], "candidate_count": len(candidates),
        "selection": {"seed": seed, "method": "next 20 in existing public-ID hash order, excluding batch 1",
                      "excluded_manifest_sha256": hashlib.sha256((OLD / "task-manifest.json").read_bytes()).hexdigest(),
                      "uses_task_results": False},
        "model": pro.MODEL, "cost_limit": 50, "cost_currency": "CNY", "attempts_per_instance": 1,
        "authorization_text": "新增这20题：人民币50元，继续Pro V2／deepseek-flash。并行测试，保留每题记录。旧批次余额不沿用。",
        "tasks": []}
    shutil.copy2(OLD / "official-SHA256SUMS", OUT / "official-SHA256SUMS")

    def fetch(task):
        prefix = "tasks/" + task + "/"
        target = OUT / "public-tasks" / task
        target.mkdir(parents=True)
        data = {}
        for name in ("instruction.md", "task.toml"):
            raw = pro.download(prefix + name, manifest, checksums)
            (target / name).write_bytes(raw)
            data[name] = raw
        return {"instance_id": task, "instruction_sha256": hashlib.sha256(data["instruction.md"]).hexdigest(),
                "task_config_sha256": hashlib.sha256(data["task.toml"]).hexdigest(),
                "configuration": tomllib.loads(data["task.toml"].decode())}

    with ThreadPoolExecutor(max_workers=4) as pool:
        manifest["tasks"] = list(pool.map(fetch, selected))
    pro.save(OUT / "task-manifest.json", manifest)
    digest = hashlib.sha256((OUT / "task-manifest.json").read_bytes()).hexdigest()
    for target in (OUT, DOC):
        (target / "selected-ids.txt").write_text("\n".join(selected) + "\n")
        (target / "task-manifest.sha256").write_text(digest + "\n")
    shutil.copy2(OUT / "task-manifest.json", DOC / "task-manifest.json")
    shutil.copy2(OLD_DOC / "native-entry.py", DOC / "native-entry.py")
    shutil.copytree(OLD / "runtime", OUT / "runtime")
    shutil.copy2(OLD / "isolated-python-build-config.json", OUT / "isolated-python-build-config.json")
    code = OUT / "code/nz_coder"
    shutil.copytree(ROOT / "nz_coder", code, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # 不把宿主评测、隐藏评分入口或计费凭据挂入 Agent。
    for path in (code / "evaluation").iterdir():
        if path.name not in {"__init__.py", "reproducibility.py"}:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    print(json.dumps({"prepared": 20, "overlap": len(set(selected) & excluded), "manifest_sha256": digest}))


def execute():
    from dotenv import dotenv_values
    from nz_coder.evaluation.deepseek_counting import DeepSeekV41Counter
    from nz_coder.evaluation.model_relay import RelayLedger, RelayLimits
    from nz_coder.swebench.trace_budget import TraceBudget, archive_instance_diagnostics

    manifest = json.loads((OUT / "task-manifest.json").read_text())
    digest = hashlib.sha256((OUT / "task-manifest.json").read_bytes()).hexdigest()
    if digest != (DOC / "task-manifest.sha256").read_text().strip():
        raise RuntimeError("frozen_manifest_changed")
    sys.path.insert(0, str(OLD.parent / "nz-provider-contract-2026-10-09/official/recipe-site"))
    counter = DeepSeekV41Counter(OLD.parent / "nz-provider-contract-2026-10-09/official/audited-recipe/static/tokenizers/v41/tokenizer.json")
    key = os.environ.get("NZ_REVIEW_DEEPSEEK_API_KEY") or dotenv_values(ROOT / ".env").get("API_KEY")
    if not key:
        raise RuntimeError("authorized_host_key_unavailable")
    live = OUT / "live"
    live.mkdir(exist_ok=False)
    grant = {"purpose": "swebench-pro-v2", "model": pro.MODEL,
        "scope": [row["instance_id"] for row in manifest["tasks"]], "instance_manifest_sha256": digest,
        "account": "project-env:DeepSeek", "input_mode": "empirical", "empirical_risk_accepted": True,
        "contract_id": counter.contract_id, "cost_limit": 50, "cost_currency": "CNY",
        "authorization_text": manifest["authorization_text"] + "复用此前已接受的经验计量与止损口径。",
        "valid_from": time.time() - 1, "valid_until": time.time() + 86400 * 2,
        "prices_cny_per_million": {"input": 2, "cached_input": 0.04, "output": 8},
        "provider_output_reservation_tokens": 393216}
    limits = RelayLimits(requests=920, input_per_request=1000000,
        input_total=math.ceil(50 / .04 * 1000000), output_total=math.ceil(50 / 8 * 1000000), request_seconds=600)
    pro.save(live / "authorization-used.json", grant)
    pro.save(live / "execution-config.json", {"source_commit": pro.command(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "model": pro.MODEL, "limits": asdict(limits), "concurrency": 2, "shared_ledger": True,
        "serialized_image_preparation": True, "main_requests_per_task": 40, "auxiliary_requests_per_task": 6,
        "task_seconds": 3000, "wire_max_tokens": None, "stream": False, "thinking": None, "reasoning_effort": None,
        "output_fee_reservation_tokens": 393216, "native_entry_sha256": hashlib.sha256((DOC / "native-entry.py").read_bytes()).hexdigest(),
        "code_files_sha256": {str(path.relative_to(OUT / "code")): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted((OUT / "code").rglob("*.py"))}})
    checksums = {parts[1].lstrip("*").removeprefix("./"): parts[0]
                 for line in (OUT / "official-SHA256SUMS").read_text().splitlines()
                 if (parts := line.split(maxsplit=1))}
    ledger = RelayLedger(live / "ledger.jsonl", limits, empirical_authorization=grant)
    budget = TraceBudget(OUT / "archives", warning_bytes=858993459, hard_limit_bytes=1073741824, cleanup_target_bytes=644245094)
    preparation_lock, archive_lock, counter_lock, progress_lock = (threading.Lock() for _ in range(4))
    reports = []

    def count(raw, payload):
        with counter_lock:
            return counter(raw, payload)

    def run(number, task):
        slot = live / f"b2-{number:02}"
        slot.mkdir()
        report = {"number": number, "instance_id": task["instance_id"], "attempt": 0,
                  "agent_status": None, "resolved": None}
        pro.save(slot / "identity.json", report)
        base = tag = case = None
        print("START " + slot.name + " " + task["instance_id"], flush=True)
        try:
            # 镜像构建目录与磁盘准入共用锁；不串行化 Agent 的实际解题。
            with preparation_lock:
                with ledger.lock:
                    if ledger.failed_contracts:
                        raise RuntimeError("accounting_stop")
                    if ledger.cost_occupied_cny + 3.145728 > 50:
                        raise RuntimeError("remaining_global_budget_stop")
                if shutil.disk_usage(OUT).free < 2 * 1024 ** 3:
                    raise RuntimeError("storage_stop")
                base, tag, executable = continuation.isolated_image(task, slot)
                case = pro.prepare_case(slot, tag, task, False)
                config = json.loads((case / "input/run.json").read_text())
                config["provider_default_output"] = True
                pro.save(case / "input/run.json", config)
            report.update(attempt=1, agent_started_epoch=time.time())
            pro.run_case(slot, case, tag, ledger, count, key=key, agent_python=executable)
            report["agent_finished_epoch"] = time.time()
            patch = pro.freeze(slot, case)
            result_path = case / "result/result.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else None
            report.update(status="attempted", agent_status=result["status"] if result else "missing_result",
                          patch_sha256=hashlib.sha256(patch).hexdigest())
            verdict = pro.grade(slot, base, task, manifest, checksums)
            report.update(resolved=verdict["resolved"], eval_status=verdict["status"])
            with archive_lock:
                pro.archive_case(slot, case, task, "batch2", budget, archive_instance_diagnostics)
        except Exception as exc:
            report.update(status="blocked" if case is None else "attempt_or_delivery_error", error=repr(exc))
        finally:
            continuation.settle_failed_unit_boundary(ledger, slot.name)
            with ledger.lock:
                report["physical_requests"] = sum(row["run"] == slot.name for row in ledger.attempts.values())
            # 仅清理这个题目的镜像标签；Session、原始响应和补丁永久保留。
            with preparation_lock:
                if tag:
                    pro.command(["docker", "image", "rm", tag], log=slot / "image-cleanup.txt", check=False)
                if base:
                    pro.command(["docker", "image", "rm", base], log=slot / "base-cleanup.txt", check=False)
                    pro.command(["docker", "image", "rm", task["configuration"]["environment"]["docker_image"]],
                                log=slot / "base-tag-cleanup.txt", check=False)
            pro.save(slot / "report.json", report)
            with progress_lock:
                reports.append(report)
                pro.save(live / "progress.json", sorted(reports, key=lambda row: row["number"]))
            print("END " + json.dumps(report, ensure_ascii=False), flush=True)
        return report

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, number, task) for number, task in enumerate(manifest["tasks"], 1)]
            for future in as_completed(futures):
                future.result()
        with ledger.lock:
            pro.save(live / "summary.json", {"tasks": sorted(reports, key=lambda row: row["number"]),
                "physical_requests": len(ledger.attempts), "estimated_cost_occupied_cny": ledger.cost_occupied_cny,
                "accounting_failures": ledger.failed_contracts, "old_batch_balance_used": False})
    finally:
        ledger.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute"])
    args = parser.parse_args()
    prepare() if args.action == "prepare" else execute()
