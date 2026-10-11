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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "resume-preparation"])
    args = parser.parse_args()
    driver.prepare(resume=driver.OUT.exists()) if args.action == "prepare" else driver.execute(args.action == "resume-preparation")
