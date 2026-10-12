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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "resume-preparation"])
    args = parser.parse_args()
    grant = json.loads((DOC / "authorization-record.json").read_text())
    if grant["cost_limit_cny"] != 50 or grant["model"] != driver.pro.MODEL or not grant["authorized"]:
        raise RuntimeError("batch_authorization_mismatch")
    if args.action == "prepare":
        driver.prepare(resume=driver.OUT.exists())
    else:
        driver.execute(args.action == "resume-preparation")
