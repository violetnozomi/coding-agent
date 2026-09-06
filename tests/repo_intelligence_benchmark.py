"""Fixed, offline temporary-workspace benchmark; run with python -m tests.repo_intelligence_benchmark."""
from __future__ import annotations

import json
import platform
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from nz_coder.intelligence.service import RepoIntelligenceService


def trial():
    with tempfile.TemporaryDirectory(prefix="nz-index-benchmark-") as directory:
        root = Path(directory)
        for i in range(40):
            (root / f"mod{i}.py").write_text(f"def fn{i}(): return {i}\n", encoding="utf-8")
        service = RepoIntelligenceService(root)
        try:
            start = time.perf_counter()
            assert service.prewarm(max_files=100).result(10).status == "ready"
            cold = (time.perf_counter() - start) * 1000
            edits = []
            for i in range(20):
                (root / "mod0.py").write_text(f"def version{i}(): return {i}\n", encoding="utf-8")
                start = time.perf_counter()
                service._apply_incremental(("mod0.py",), 100)
                edits.append((time.perf_counter() - start) * 1000)
            assert service.symbol_context("version19")["definition"]["path"] == "mod0.py"
            service.symbol_context("fn1")
            hits = service.metrics()["cache_hit"]
            start = time.perf_counter()
            for _ in range(200):
                assert service.symbol_context("fn1")["definition"]["path"] == "mod1.py"
            hot = (time.perf_counter() - start) * 1000 / 200
            assert service.metrics()["cache_hit"] - hits == 200
            entered, release = Event(), Event()
            original = service.index.update_paths

            def paused(paths):
                result = original(paths)
                entered.set()
                assert release.wait(5)
                return result

            service.index.update_paths = paused
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(service._apply_incremental, ("mod0.py",), 100)
                try:
                    assert entered.wait(3)
                    start = time.perf_counter()
                    result = pool.submit(service.symbol_context, "fn1", wait_budget_ms=20).result(1)
                    during = (time.perf_counter() - start) * 1000
                finally:
                    release.set()
                future.result(3)
            return {"cold_ms": cold, "incremental_median_ms": statistics.median(edits),
                    "hot_ms": hot, "hot_hits": 200, "during_ms": during,
                    "during_fallback": bool(result.get("fallback")), "final_visible": True}
        finally:
            service.close()


if __name__ == "__main__":
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                      "files": 40, "edits": 20, "hot_queries": 200,
                      "wait_budget_ms": 20, "trials": [trial() for _ in range(3)]}, indent=2))
