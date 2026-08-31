#!/usr/bin/env python3
"""Measure the bounded TUI RC performance cases without contacting a model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nz_coder.interface.commands import build_default_registry
from nz_coder.interface.terminal_input import TerminalCompleter, scan_workspace_files
from nz_coder.interface.timeline import format_transcript
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import list_sessions, session_dir
from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector


ROOT = Path(__file__).resolve().parents[1]


def _measure(callback, repetitions: int = 1):  # noqa: ANN001, ANN202
    values = []
    result = None
    for _index in range(max(1, repetitions)):
        started = time.perf_counter()
        result = callback()
        values.append((time.perf_counter() - started) * 1000)
    return result, {
        "median_ms": round(statistics.median(values), 3),
        "max_ms": round(max(values), 3),
        "samples": len(values),
    }


def run_benchmark() -> dict:
    startup_values = []
    for _index in range(4):
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-m", "nz_coder", "--help"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
        )
        if completed.returncode != 0:
            raise RuntimeError("CLI startup smoke failed")
        startup_values.append((time.perf_counter() - started) * 1000)

    registry = build_default_registry()
    files = tuple(f"src/file-{index:05d}.py" for index in range(10_000))
    completer = TerminalCompleter(
        registry,
        ROOT,
        file_provider=lambda: files,
        session_provider=lambda: (),
    )

    def complete_file():
        document = Document("inspect @src/file-09", len("inspect @src/file-09"))
        return list(completer.get_completions(document, CompleteEvent()))

    completions, completion_metric = _measure(complete_file, repetitions=30)

    projector = ToolResultProjector(budget=ToolResultBudget(max_tokens=512))
    output_metrics = {}
    for size in (100_000, 1_000_000):
        _value, metric = _measure(
            lambda size=size: projector.project(
                f"perf-{size}",
                "HEAD\n" + ("x" * size) + "\nTAIL",
                tool_name="bash",
            ),
            repetitions=3,
        )
        output_metrics[str(size)] = metric

    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"Turn {index} 中文 🚀"}
        for index in range(2_000)
    ]
    transcript, transcript_metric = _measure(
        lambda: format_transcript("perf-session", messages), repetitions=3,
    )

    with tempfile.TemporaryDirectory(prefix="nz-tui-perf-") as directory:
        workspace = Path(directory)
        source = workspace / "src"
        source.mkdir()
        for index in range(10_050):
            (source / f"file-{index:05d}.py").touch()
        scanned, scan_metric = _measure(lambda: scan_workspace_files(workspace))
        with scoped_workdir(workspace):
            storage = session_dir()
            storage.mkdir(parents=True)
            for index in range(1_000):
                (storage / f"session-{index:04d}.json").write_text(
                    json.dumps({"session_id": f"session-{index:04d}"}),
                    encoding="utf-8",
                )
            sessions, session_metric = _measure(lambda: list_sessions(limit=1_000))

    metrics = {
        "cold_startup_ms": round(startup_values[0], 3),
        "warm_startup_ms": round(statistics.median(startup_values[1:]), 3),
        "typing_file_completion_10k": completion_metric,
        "tool_output_projection": output_metrics,
        "large_transcript_2000_messages": transcript_metric,
        "workspace_scan_10k": scan_metric,
        "session_listing_1k": session_metric,
    }
    checks = {
        "startup_under_2s": max(startup_values) < 2_000,
        "typing_p95_proxy_under_50ms": completion_metric["max_ms"] < 50,
        "output_1m_under_500ms": output_metrics["1000000"]["max_ms"] < 500,
        "large_transcript_under_2s": transcript_metric["max_ms"] < 2_000,
        "workspace_scan_under_5s": scan_metric["max_ms"] < 5_000,
        "session_listing_under_3s": session_metric["max_ms"] < 3_000,
    }
    return {
        "schema_version": 1,
        "environment": {
            "platform": platform.system().lower(),
            "python_version": platform.python_version(),
        },
        "success": all(checks.values()),
        "checks": checks,
        "metrics": metrics,
        "observations": {
            "completion_count": len(completions),
            "transcript_characters": len(transcript),
            "scanned_files": len(scanned),
            "listed_sessions": len(sessions),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    report = run_benchmark()
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
