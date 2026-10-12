"""第四批20题：复用既有并行评估入口，排除前三批80个固定题号。"""
from pathlib import Path
import argparse
import importlib.util
import json

DOC = Path(__file__).resolve().parent
previous = DOC.parent / "swebench-pro-40-batch3-2026-10-11/run-parallel.py"
spec = importlib.util.spec_from_file_location("pro_batch4_existing_driver", previous)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
driver = module.driver
driver.DOC = DOC
driver.OUT = driver.OLD.with_name("nz-swebench-pro-v2-20-batch4-2026-10-12")
driver.TASK_COUNT, driver.BATCH, driver.WORKERS = 20, 4, 4
driver.PRIOR_MANIFESTS = tuple(DOC.parent / folder / "task-manifest.json" for folder in (
    "swebench-pro-20-2026-10-11", "swebench-pro-20-batch2-2026-10-11",
    "swebench-pro-40-batch3-2026-10-11"))
driver.pro.OUT, driver.pro.DOC = driver.OUT, DOC
driver.continuation.OUT, driver.continuation.pro = driver.OUT, driver.pro


def continue_small():
    """原阶段闭账后，只首次运行尚无模型调用的普通工作区题。"""
    manifest = json.loads((driver.OUT / "task-manifest.json").read_text())
    plan = driver.preparation_remainder(driver.OUT / "live", manifest, include_unbilled_sdk=True)
    plan["tasks"] = [(number, task) for number, task in plan["tasks"] if number not in (9, 15)]
    plan.update(execution_stage="p2", new_authorization_cny=50,
        original_same_authorization=True,
        authorization_text="本批总人民币50元，只首次运行零模型调用题；扣除已结算占用，不增加授权，不携带前三批余款。9/15另按已批准内存工作区串行执行。")
    driver.execute(additional_plan=plan)


def memory():
    """复用既有剩额和内存接入；仅本批9/15使用内存源码。"""
    authorization = json.loads((DOC / "memory-workspace-authorization.json").read_text())
    if not authorization["authorized"] or authorization["numbers"] != [9, 15]:
        raise RuntimeError("memory_workspace_authorization_mismatch")
    manifest = json.loads((driver.OUT / "task-manifest.json").read_text())
    sources = [driver.OUT / name for name in ("live", "p2")]
    plan = driver.combine_closed_sources(sources, manifest, driver.OUT / "memory-source", include_unbilled_sdk=True)
    plan["deferred_other_zero_model_tasks"] = [number for number, _ in plan["tasks"] if number not in (9, 15)]
    plan["tasks"] = [(number, task) for number, task in plan["tasks"] if number in (9, 15)]
    temporary_root = Path(authorization["temporary_root"])
    temporary_root.mkdir(mode=0o700)
    if driver.shutil.disk_usage(temporary_root).free < 32 * 1024 ** 3:
        raise RuntimeError("authorized_memory_workspace_capacity_insufficient")
    plan.update(execution_stage="supplement-memory", temporary_workspace_root=str(temporary_root),
        new_authorization_cny=50, original_same_authorization=True,
        authorization_text="本批人民币50元总预算不变，扣除前序全部占用，仅首次运行9/15；用户已批准两题临时内存工作区，持久证据落盘，归档成功后清理，前三批余款不携带。")
    driver.WORKERS = 1
    driver.execute(additional_plan=plan)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "resume-preparation", "continue-small", "memory"])
    args = parser.parse_args()
    grant = json.loads((DOC / "authorization-record.json").read_text())
    if grant["cost_limit_cny"] != 50 or grant["model"] != driver.pro.MODEL or not grant["authorized"]:
        raise RuntimeError("batch_authorization_mismatch")
    if args.action == "prepare":
        driver.prepare(resume=driver.OUT.exists())
    elif args.action == "continue-small":
        continue_small()
    elif args.action == "memory":
        memory()
    else:
        driver.execute(args.action == "resume-preparation")
