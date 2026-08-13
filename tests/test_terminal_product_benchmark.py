from __future__ import annotations

import json

from nz_coder.evaluation.terminal_product import run_phase2_terminal_product_benchmark


def test_phase2_terminal_product_benchmark_drives_real_daemon_and_attach():
    report = run_phase2_terminal_product_benchmark(repetitions=1, timeout_seconds=20)

    assert report["success"] is True
    assert report["repetitions"] == 1
    assert report["metrics"]["session_resume_success"] == 1
    assert report["metrics"]["attach_snapshot_latency_ms"]["median"] >= 0
    assert report["metrics"]["attach_terminal_latency_ms"]["median"] > 0
    assert report["metrics"]["reconnect_terminal_latency_ms"]["median"] > 0
    print("NZ_PRODUCT_METRICS " + json.dumps({
        "attach_latency_ms": report["metrics"]["attach_terminal_latency_ms"],
        "reconnect_latency_ms": report["metrics"]["reconnect_terminal_latency_ms"],
    }, separators=(",", ":")))
