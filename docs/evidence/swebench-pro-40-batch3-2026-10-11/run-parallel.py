"""第三批固定 40 题：复用既有并发执行器，不新增 Agent 或评测循环。"""
from pathlib import Path
import argparse
import importlib.util
import hashlib
import json
import os
import subprocess
import tempfile

DOC = Path(__file__).resolve().parent
ROOT = DOC.parents[2]
previous = DOC.parent / "swebench-pro-20-batch2-2026-10-11/run-parallel.py"
spec = importlib.util.spec_from_file_location("pro_batch3_existing_driver", previous)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
driver.DOC = DOC
driver.OUT = driver.OLD.with_name("nz-swebench-pro-v2-40-batch3-2026-10-11")
driver.TASK_COUNT, driver.BATCH, driver.WORKERS = 40, 3, 4
driver.PRIOR_MANIFESTS = (driver.OLD / "task-manifest.json",
    driver.OLD.with_name("nz-swebench-pro-v2-20-batch2-2026-10-11") / "task-manifest.json")
driver.pro.OUT, driver.pro.DOC = driver.OUT, DOC
driver.pro.CONTAINER_EVIDENCE_LIMIT_BYTES = 2 * 1024 ** 3
driver.continuation.OUT, driver.continuation.pro = driver.OUT, driver.pro

def download(relative, manifest, checksums):
    url = f'https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/{manifest["official_source_commit"]}/v2/{relative}'
    env = {k: os.environ[k] for k in ("PATH", "HOME")}
    with tempfile.TemporaryDirectory(prefix="nz-pro-download-") as directory:
        target = Path(directory) / "body"
        records = []
        for proxy in (None, "http://127.0.0.1:7890"):
            argv = ["curl", "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "4",
                "--max-time", "20", "--max-filesize", "4194304", "-o", str(target)]
            argv += ["--noproxy", "*"] if proxy is None else ["--proxy", proxy]
            result = subprocess.run(argv + [url], env=env, capture_output=True, text=True, timeout=25)
            records.append({"proxy_used": proxy is not None, "returncode": result.returncode})
            if result.returncode == 0:
                raw = target.read_bytes()
                if len(raw) > 4194304 or hashlib.sha256(raw).hexdigest() != checksums[relative]:
                    raise RuntimeError("official_file_checksum_mismatch")
                return raw
        raise RuntimeError("official_download_blocked: " + relative + " " + json.dumps(records))


driver.pro.download = download


def supplement():
    manifest = json.loads((driver.OUT / "task-manifest.json").read_text())
    closed = json.loads((DOC / "authorization-closed.json").read_text())
    if not closed["authorization_closed"] or not closed["all_admissions_settled"]:
        raise RuntimeError("original_authorization_not_closed")
    plan = driver.combine_closed_sources([driver.OUT / "live", driver.OUT / "p2"], manifest,
        driver.OUT / "supplement-source", include_unbilled_sdk=True)
    if abs(plan["used_cny"] - closed["occupied_cny"]) > 1e-8:
        raise RuntimeError("closed_original_fee_mismatch")
    # 新授权仅补零模型调用题；原授权余款已关闭，不并入新增 50 元。
    plan.update(remaining_cny=50, remaining_requests=len(plan["tasks"]) * 46,
        authorization_text="追加人民币50元，补齐原名单；仅首次调用尚未调用模型的题，已付费任务不重跑。复用此前接受的经验计量与止损口径。")
    driver.execute(additional_plan=plan)


def supplement_memory():
    source = driver.OUT / "supplement"
    handoff = json.loads((source / "memory-handoff.json").read_text())
    if not handoff["all_paid_requests_settled"] or not handoff["large_tasks_have_no_sdk_launch_or_model_call"]:
        raise RuntimeError("supplement_not_at_safe_memory_handoff")
    manifest = json.loads((driver.OUT / "task-manifest.json").read_text())
    rows = [json.loads(line) for line in (source / "ledger.jsonl").read_text().splitlines()]
    admissions = {row["attempt_id"]: row for row in rows if row["event"] == "admitted"}
    settlements = {row["attempt_id"]: row for row in rows if row["event"] == "settled"}
    if set(admissions) != set(settlements) or any(row["event"] == "accounting_contract_failed" for row in rows):
        raise RuntimeError("supplement_accounting_not_closed")
    fee = sum(row["estimated_cost_reported_cny"] if row["estimated_cost_reported_cny"] is not None
              else admissions[key]["estimated_cost_reserved_cny"] for key, row in settlements.items())
    if abs(fee - handoff["estimated_used_cny"]) > 1e-8:
        raise RuntimeError("supplement_handoff_fee_mismatch")
    tasks = [(number, manifest["tasks"][number - 1]) for number in (27, 28)]
    if any(row["unit"] in {task["instance_id"] for _, task in tasks} for row in admissions.values()):
        raise RuntimeError("large_task_already_called_model")
    temporary_root = Path("/dev/shm/nz-pro-batch3-supplement-20261012")
    temporary_root.mkdir(mode=0o700)
    if driver.shutil.disk_usage(temporary_root).free < 32 * 1024 ** 3:
        raise RuntimeError("temporary_workspace_capacity_insufficient")
    plan = {"tasks": tasks, "used_cny": fee, "remaining_cny": 50 - fee,
        "new_authorization_cny": 50,
        "remaining_requests": 11 * 46 - len(admissions), "source_ledger_sha256": hashlib.sha256((source / "ledger.jsonl").read_bytes()).hexdigest(),
        "execution_stage": "supplement-memory", "temporary_workspace_root": str(temporary_root),
        "authorization_text": "追加人民币50元，补齐原名单；允许临时内存工作区。只补27/28零模型任务，沿新增50元授权剩余额度继续，旧授权余款不携带。"}
    driver.WORKERS = 1
    driver.execute(additional_plan=plan)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "resume-preparation", "supplement", "supplement-memory"])
    args = parser.parse_args()
    if args.action == "supplement-memory":
        supplement_memory()
    elif args.action == "supplement":
        supplement()
    else:
        driver.prepare(resume=driver.OUT.exists()) if args.action == "prepare" else driver.execute(args.action == "resume-preparation")
