"""Bounded R1 product invocation helpers; never implement fixture solutions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from fixtures import PROMPTS


def environment(root: Path, *, provider="r1-metered") -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update({
        "MODEL_PROVIDER": provider, "MODEL_ID": "deepseek-v4-flash",
        "NZ_R1_LEDGER": str(root / "cost-ledger.jsonl"),
        "PERMISSION_MODE": "acceptEdits", "MAX_AGENT_TURNS": "12",
        "NZ_NOMINAL_AGENT_TURNS": "12", "MAX_PARALLEL_TASKS": "1",
        "NZ_PROVIDER_MAX_RETRIES": "0", "NZ_PROVIDER_HARD_TIMEOUT_SECONDS": "90",
        "NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS": "45",
        "NZ_PROVIDER_NON_STREAMING_FALLBACK": "0", "BASH_TIMEOUT_SECONDS": "30",
        "MAX_TOOL_CALLS_PER_RESPONSE": "3", "MAX_CONTEXT_TOKENS": "65536",
        "MAX_OUTPUT_TOKENS": "4096", "SYSTEM_CONTEXT_BUDGET_TOKENS": "6000",
        "NZ_PLANNING_ENABLED": "0", "NZ_REFLECTION_ENABLED": "0",
        "MEMORY_LLM_RERANK": "0", "MEMORY_LLM_EXTRACT": "0",
        "MEMORY_AUTO_EXTRACT": "0", "MEMORY_AUTO_DREAM": "0",
        "NZ_LSP_WRITE_DIAGNOSTICS_ENABLED": "0", "TRACE_ENABLED": "0",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "TERM": "xterm-256color",
    })
    return env


def save(root: Path, name: str, value) -> None:
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))


def headless(root: Path, case: str = "T01") -> None:
    if (root / f"{case}-headless.raw").exists():
        raise FileExistsError("Preserve previous R1 attempt; use a new attempt directory")
    workspace = root / "projects" / case
    python = str(root / "venv/bin/python")
    command = [python, "-m", "nz_coder", "run", "--cwd", str(workspace),
               "--permission-mode", "acceptEdits", "--max-turns", "12",
               "--output", "jsonl", "-p", PROMPTS[case]]
    started = time.time()
    # Raw output remains private, outside the source/fixture repository.
    with (root / f"{case}-headless.raw").open("wb") as output:
        process = subprocess.Popen(command, cwd=workspace, env=environment(root),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=output, start_new_session=True)
        try:
            stdout, _ = process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGINT)
            try:
                stdout, _ = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, _ = process.communicate(timeout=5)
        output.write(stdout)
        code = process.returncode
    save(root, f"{case}-timing.json", {"start": started, "exit_code": code,
         "duration": time.time()-started, "frontend_feedback":"UNKNOWN"})
    print(json.dumps({"case": case, "exit_code": code, "elapsed": time.time()-started}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    headless(args.root)
