"""Repeatable local measurements for the Phase 2 terminal product path."""
from __future__ import annotations

import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

from nz_coder.http_service.client import NZCoderClient
from nz_coder.http_service.daemon import start_daemon, stop_daemon


def run_phase2_terminal_product_benchmark(
    *,
    repetitions: int = 3,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Measure the real daemon, HTTP attach, and terminal attach path locally.

    This benchmark deliberately does not call a model. Product transport and
    reconnect latency are independent of model intelligence, while the R1-R10
    integration suite owns interaction/process correctness evidence.
    """
    count = max(1, min(int(repetitions), 100))
    repository = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="nz-phase2-product-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        state_root = root / "daemon"
        workspace.mkdir()
        daemon = start_daemon(
            state_root=state_root,
            port=0,
            workspaces=[str(workspace)],
            startup_timeout=timeout_seconds,
        )
        try:
            token = Path(daemon["token_path"]).read_text(encoding="utf-8").strip()
            client = NZCoderClient(daemon["endpoint"], token, timeout=timeout_seconds)
            workspace_id = next(
                item["id"]
                for item in client.list_workspaces()
                if Path(item["path"]) == workspace
            )
            session = client.create_session("default", workspace_id)
            snapshot_ms: list[float] = []
            attach_ms: list[float] = []
            reconnect_ms: list[float] = []
            invocations: list[dict[str, Any]] = []
            environment = dict(os.environ)
            existing_pythonpath = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(repository), existing_pythonpath) if item
            )

            for repetition in range(1, count + 1):
                started = time.perf_counter()
                snapshot = client.attach_snapshot(session["id"])
                snapshot_ms.append((time.perf_counter() - started) * 1000)
                if snapshot.get("session", {}).get("id") != session["id"]:
                    raise RuntimeError("attach snapshot returned the wrong Session")

                first = _run_terminal_attach(
                    repository=repository,
                    state_root=state_root,
                    session_id=session["id"],
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                )
                second = _run_terminal_attach(
                    repository=repository,
                    state_root=state_root,
                    session_id=session["id"],
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                )
                attach_ms.append(first["elapsed_ms"])
                reconnect_ms.append(second["elapsed_ms"])
                invocations.append({
                    "repetition": repetition,
                    "attach": first,
                    "reconnect": second,
                })

            return {
                "schema_version": 1,
                "benchmark": "terminal-product-phase2-local-attach",
                "product_path": "real daemon -> authenticated HTTP -> nz-coder attach",
                "repetitions": count,
                "session_id": session["id"],
                "success": all(
                    item[phase]["returncode"] == 0
                    and item[phase]["session_visible"]
                    for item in invocations
                    for phase in ("attach", "reconnect")
                ),
                "metrics": {
                    "attach_snapshot_latency_ms": _distribution(snapshot_ms),
                    "attach_terminal_latency_ms": _distribution(attach_ms),
                    "reconnect_terminal_latency_ms": _distribution(reconnect_ms),
                    "session_resume_success": sum(
                        1 for item in invocations if item["reconnect"]["session_visible"]
                    ),
                    "session_resume_attempts": count,
                },
                "invocations": invocations,
                "limitations": [
                    "Local loopback measurement; it is not WAN latency.",
                    "No model call is made, so token and task-success metrics do not apply.",
                    "Permission, question, gap, process, and child lifecycle metrics remain in R1-R10.",
                ],
            }
        finally:
            stop_daemon(state_root=state_root)


def _run_terminal_attach(
    *,
    repository: Path,
    state_root: Path,
    session_id: str,
    environment: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "nz_coder",
        "attach",
        session_id,
        "--state-root",
        str(state_root),
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=repository,
        env=environment,
        input="/status\n/exit\n",
        text=True,
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
        check=False,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    combined = f"{completed.stdout}\n{completed.stderr}"
    return {
        "returncode": completed.returncode,
        "elapsed_ms": round(elapsed_ms, 3),
        "session_visible": session_id in combined,
        "stderr_excerpt": completed.stderr[-500:],
    }


def _distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.95)))
    return {
        "min": round(ordered[0], 3),
        "median": round(float(statistics.median(ordered)), 3),
        "p95": round(ordered[index], 3),
        "max": round(ordered[-1], 3),
    }


__all__ = ["run_phase2_terminal_product_benchmark"]
