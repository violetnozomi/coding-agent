"""Aider benchmark readiness helper for NZ-Coder.

The official Aider benchmark harness lives in the Aider repository and is
designed to run Aider itself. This helper makes the environment requirements
explicit before we build an NZ-Coder adapter for the same polyglot exercises.
"""
from __future__ import annotations

import asyncio

import argparse
import json
import importlib.util
import shutil
import subprocess
import sys
import time
from pathlib import Path

from nz_coder import config
from nz_coder.prompt import build
from nz_coder.runtime.composition import build_product_environment
from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.trace import TraceRecorder


AIDER_REPO = "https://github.com/Aider-AI/aider.git"
POLYGLOT_REPO = "https://github.com/Aider-AI/polyglot-benchmark.git"
BENCH_DIR = config.WORKDIR / ".nz-coder" / "aider-benchmark"
AIDER_DIR = BENCH_DIR / "aider"
POLYGLOT_DIR = BENCH_DIR / "polyglot-benchmark"
RUNS_DIR = BENCH_DIR / "runs"


def check_environment() -> int:
    """Print local readiness for Aider's official polyglot benchmark."""
    rows = [
        _check_python(),
        _check_executable("git"),
        _check_executable("docker"),
        _check_module("aider"),
        _check_path("aider repo", AIDER_DIR),
        _check_path("polyglot exercises", POLYGLOT_DIR),
        _check_executable("node"),
        _check_executable("npm"),
        _check_executable("go"),
        _check_executable("cargo"),
        _check_executable("javac"),
    ]
    print("# Aider benchmark readiness\n")
    for ok, name, detail in rows:
        status = "OK" if ok else "MISSING"
        print(f"- [{status}] {name}: {detail}")

    print("\n# Official setup commands\n")
    print(f"git clone {AIDER_REPO} {AIDER_DIR}")
    print(f"git clone {POLYGLOT_REPO} {POLYGLOT_DIR}")
    print(f"cd {AIDER_DIR}")
    print("benchmark\\docker_build.sh  # or ./benchmark/docker_build.sh inside a Unix shell")
    print("benchmark\\docker.sh        # official benchmark runs inside Docker")

    required = {
        "python",
        "git",
        "docker",
        "aider repo",
        "polyglot exercises",
    }
    blockers = [name for ok, name, _ in rows if name in required and not ok]
    if blockers:
        print("\nNot ready for official Aider benchmark.")
        print("Blocking items: " + ", ".join(blockers))
        return 1
    print("\nReady to attempt the official Aider benchmark harness.")
    return 0


def official_command(args: argparse.Namespace) -> int:
    """Print the official benchmark command for an existing Aider checkout."""
    model = args.model
    edit_format = args.edit_format
    threads = args.threads
    name = args.name
    num_tests = args.num_tests
    command = [
        "./benchmark/benchmark.py",
        name,
        "--model",
        model,
        "--edit-format",
        edit_format,
        "--threads",
        str(threads),
        "--exercises-dir",
        "polyglot-benchmark",
    ]
    if num_tests:
        command.extend(["--num-tests", str(num_tests)])
    if args.keywords:
        command.extend(["--keywords", args.keywords])

    print("Run inside the official Aider Docker container from the Aider repo root:")
    print(" ".join(command))
    return 0


def run_git_setup() -> int:
    """Clone Aider and polyglot benchmark repos into .nz-coder/aider-benchmark."""
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    steps = [
        (AIDER_DIR, ["git", "clone", AIDER_REPO, str(AIDER_DIR)]),
        (POLYGLOT_DIR, ["git", "clone", POLYGLOT_REPO, str(POLYGLOT_DIR)]),
    ]
    for target, cmd in steps:
        if target.exists():
            print(f"Exists: {target}")
            continue
        print("Running: " + " ".join(cmd))
        result = subprocess.run(cmd, cwd=str(BENCH_DIR))
        if result.returncode != 0:
            return result.returncode
    return 0


async def run_python_exercise(args: argparse.Namespace) -> int:
    """Run NZ-Coder against one Python exercise from Aider's polyglot benchmark."""
    exercise_src = POLYGLOT_DIR / "python" / "exercises" / "practice" / args.exercise
    if not exercise_src.exists():
        print(f"Error: exercise not found: {exercise_src}")
        print("Run `python -m nz_coder.aider_benchmark setup` first, then choose an exercise.")
        return 2

    run_id = time.strftime("%Y%m%d-%H%M%S") + f"--python--{args.exercise}"
    workdir = RUNS_DIR / run_id
    shutil.copytree(exercise_src, workdir)

    instructions = _read_exercise_instructions(workdir)
    before = _run_pytest(workdir)
    test_cmd = f'"{sys.executable}" -m pytest -q'

    with scoped_workdir(workdir):
        tracer = TraceRecorder(trace_dir=workdir / ".nz-coder-runs", enabled=True)
        system_prompt = build() + (
            f"\n\nYou are solving an Aider polyglot benchmark Python exercise in: {workdir}\n"
            "Edit the implementation files so the provided tests pass. Do not weaken tests. "
            f"Run `{test_cmd}` before finishing."
        )
        agent = build_product_environment(
            system_prompt, permission_mode="auto", tracer=tracer,
        )
    messages = [{
        "role": "user",
        "content": (
            "Solve this Exercism/Aider benchmark task.\n\n"
            f"{instructions}\n\n"
            f"Important: inspect the files, implement the missing code, and run `{test_cmd}`."
        ),
    }]

    tool_log = []

    def log_tool(name: str, output: str) -> None:
        tool_log.append({
            "tool": name,
            "status": _tool_status(output),
            "output_len": len(output),
        })
        print(f"  [{args.exercise}] {name}: {_safe_console(output[:100])}")

    started = time.time()
    try:
        status = await agent.run(messages, on_tool=log_tool, stream=False)
    finally:
        agent.close()

    after = _run_pytest(workdir)
    result = {
        "benchmark": "aider-polyglot-python-smoke",
        "exercise": args.exercise,
        "run_id": run_id,
        "workdir": str(workdir),
        "trace": str(tracer.path),
        "agent_status": status,
        "passed_before": before["passed"],
        "passed_after": after["passed"],
        "before": before,
        "after": after,
        "duration": round(time.time() - started, 1),
        "tool_calls": len(tool_log),
        "tool_errors": sum(1 for row in tool_log if row["status"] == "error"),
        "tool_nonzero": sum(1 for row in tool_log if row["status"] == "nonzero"),
    }
    report_path = workdir / "nz_coder_aider_result.json"
    report_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    outcome = "PASS" if after["passed"] else "FAIL"
    print(f"\n[{outcome}] {args.exercise}")
    print(f"Before: {before['summary']}")
    print(f"After:  {after['summary']}")
    print(f"Report: {report_path}")
    return 0 if after["passed"] else 1


def _check_python() -> tuple[bool, str, str]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    return sys.version_info >= (3, 9), "python", version


def _check_module(name: str) -> tuple[bool, str, str]:
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False, name, "not installed"
    return True, name, spec.origin or "installed"


def _check_executable(name: str) -> tuple[bool, str, str]:
    path = shutil.which(name)
    if path is None:
        return False, name, "not found on PATH"
    return True, name, path


def _check_path(name: str, path: Path) -> tuple[bool, str, str]:
    if path.exists():
        return True, name, str(path)
    return False, name, f"missing: {path}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NZ-Coder Aider benchmark helper")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("check", help="Check local prerequisites")
    subparsers.add_parser("setup", help="Clone Aider and polyglot benchmark repos")

    run_py = subparsers.add_parser(
        "run-python",
        help="Run NZ-Coder on one Python exercise from Aider's polyglot benchmark",
    )
    run_py.add_argument("--exercise", default="affine-cipher", help="Exercise directory name.")

    cmd = subparsers.add_parser("official-command", help="Print official Aider benchmark command")
    cmd.add_argument("--name", default="nz-coder-aider-bench")
    cmd.add_argument("--model", default="gpt-4o")
    cmd.add_argument("--edit-format", default="whole")
    cmd.add_argument("--threads", type=int, default=1)
    cmd.add_argument("--num-tests", type=int)
    cmd.add_argument("--keywords")

    return parser


async def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return check_environment()
    if args.command == "setup":
        return run_git_setup()
    if args.command == "run-python":
        return await run_python_exercise(args)
    if args.command == "official-command":
        return official_command(args)
    parser.print_help()
    return 2


def _read_exercise_instructions(workdir: Path) -> str:
    candidates = [
        workdir / ".docs" / "instructions.md",
        workdir / "README.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
    return "No instructions file found. Inspect the tests and implementation files."


def _run_pytest(workdir: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    output = (result.stdout + result.stderr).strip()
    lines = output.splitlines()
    summary = lines[-1] if lines else f"exit code {result.returncode}"
    return {
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "summary": summary,
        "output_tail": output[-4000:],
    }


def _safe_console(text: str) -> str:
    encoding = sys.stdout.encoding or "utf-8"
    return str(text).encode(encoding, errors="replace").decode(encoding, errors="replace")


def _tool_status(output: str) -> str:
    if output.startswith("Error:") or output.startswith("Denied"):
        return "error"
    if output.startswith("Command exited with code"):
        return "nonzero"
    return "ok"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
