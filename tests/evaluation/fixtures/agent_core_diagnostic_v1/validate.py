"""Offline fixture validation; no Agent runner, Provider, or online transport.

Use existing offline_exec seccomp and reference_adapter workspace hashing.
Only workspace/ is materialized. Oracle and checks stay evaluator-side.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

SUITE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[4]
OFFLINE = SUITE.parent / "offline_exec.py"
sys.path.insert(0, str(ROOT))

from nz_coder.evaluation.reference_adapter import _workspace_hashes  # noqa: E402

TASKS = ("A_unknown_location", "B_failure_recovery", "C_long_horizon")
CHECK_TIMEOUT = 8
PROJECT_TIMEOUT = 30


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def hashes(workspace):
    if any(p.is_symlink() for p in workspace.rglob("*")):
        raise ValueError("fixture/version symlink rejected")
    return dict(sorted(_workspace_hashes(workspace).items()))


def snapshot(source, destination):
    """Fail on symlinks, copy only workspace files, and check immutable source."""
    before = hashes(source)
    destination.mkdir(parents=True, exist_ok=False)
    for relative in before:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    assert hashes(source) == before == hashes(destination)
    return before


def prepare(task, destination):
    if task not in TASKS:
        raise ValueError("unknown diagnostic task")
    return snapshot(SUITE / task / "workspace", destination)


def apply_oracle(task, workspace):
    """Evaluator-only overlay, never part of prepare() or future Agent input."""
    overlay = SUITE / task / "oracle"
    for relative in hashes(overlay):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay / relative, target)


def execute(argv, workspace, timeout=CHECK_TIMEOUT):
    """No inherited secrets; inherited kernel network denial; bounded process group."""
    if timeout <= 0 or timeout > PROJECT_TIMEOUT:
        raise ValueError("unbounded subprocess budget")
    argv = [sys.executable if x == "python" else x for x in argv]
    with tempfile.TemporaryDirectory(prefix="diagnostic-process-") as home:
        env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
               "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8",
               "PYTHONPATH": str(workspace), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONHASHSEED": "0",
               "NO_COLOR": "1"}
        proc = subprocess.Popen([sys.executable, str(OFFLINE), *argv], cwd=workspace,
                                env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate(timeout=5)
        def redact(text):
            for old, new in ((str(workspace), "<WORKSPACE>"), (home, "<PROCESS_HOME>"),
                             (str(SUITE), "<SUITE>"), (str(ROOT), "<REPO>"),
                             (str(Path.home()), "<USER_HOME>")):
                text = text.replace(old, new)
            return text
        return {"argv": [redact(x) for x in argv], "exit": proc.returncode,
                "timed_out": timed_out, "timeout_seconds": timeout,
                "stdout": redact(stdout), "stderr": redact(stderr)}


def acceptance(task, workspace):
    original = hashes(workspace)
    checks = json.loads((SUITE / task / "acceptance/checks.json").read_text())
    rows = []
    for check in checks:
        with tempfile.TemporaryDirectory(prefix="diagnostic-check-") as td:
            ws = Path(td) / "workspace"
            snapshot(workspace, ws)
            argv = (["python", "-c", check["code"]] if check["language"] == "python"
                    else ["node", "-e", check["code"]])
            result = execute(argv, ws)
        rows.append({"name": check["name"], "requirement": check["requirement"],
                     "passed": result["exit"] == 0 and not result["timed_out"], **result})
    assert hashes(workspace) == original, "acceptance mutated captured workspace"
    passed = sum(row["passed"] for row in rows)
    return {"checks": rows, "passed": passed, "total": len(rows),
            "summary": f"{passed}/{len(rows)} passed", "exit": 0 if passed == len(rows) else 1}


def project_tests(task, workspace):
    manifest = json.loads((SUITE / task / "manifest.json").read_text())
    before = hashes(workspace)
    result = execute(manifest["project_test_command"], workspace, PROJECT_TIMEOUT)
    assert hashes(workspace) == before, "project tests changed workspace"
    return result


def capture_version(task, workspace, output, mutation_generation=None):
    """For a settled real-run boundary: snapshot first, evaluate the copy only."""
    source_hashes = snapshot(workspace, output / "workspace")
    result = acceptance(task, output / "workspace")
    assert hashes(workspace) == source_hashes
    facts = {"source_hashes": source_hashes, "mutation_generation": mutation_generation,
             "acceptance": result}
    save(output / "version.json", facts)
    return facts


def patch(before, after):
    parts = []
    for relative in sorted(set(hashes(before)) | set(hashes(after))):
        a, b = before / relative, after / relative
        old = a.read_text().splitlines(True) if a.exists() else []
        new = b.read_text().splitlines(True) if b.exists() else []
        parts.extend(difflib.unified_diff(old, new, fromfile="a/" + relative, tofile="b/" + relative, n=0))
    return "".join(parts)


def validate_task(task, output=None):
    manifest = json.loads((SUITE / task / "manifest.json").read_text())
    assert manifest["network_required"] is False
    with tempfile.TemporaryDirectory(prefix="diagnostic-validation-") as td:
        initial, oracle, repeated = (Path(td) / name for name in ("initial", "oracle", "repeated"))
        initial_hash = prepare(task, initial)
        assert prepare(task, oracle) == prepare(task, repeated) == initial_hash
        baseline_tests = project_tests(task, initial)
        initial_result = acceptance(task, initial)
        assert baseline_tests["exit"] == 0, baseline_tests
        assert initial_result["summary"] == manifest["expected_initial_acceptance"] + " passed", initial_result
        assert initial_result["exit"] == 1
        apply_oracle(task, oracle)
        oracle_tests, oracle_result = project_tests(task, oracle), acceptance(task, oracle)
        assert oracle_tests["exit"] == 0, oracle_tests
        assert oracle_result["summary"] == manifest["expected_oracle_acceptance"] + " passed", oracle_result
        assert oracle_result["exit"] == 0
        apply_oracle(task, repeated)
        repeat_tests, repeat_result = project_tests(task, repeated), acceptance(task, repeated)
        assert repeat_tests["exit"] == repeat_result["exit"] == 0
        assert hashes(oracle) == hashes(repeated)
        assert [x["passed"] for x in oracle_result["checks"]] == [x["passed"] for x in repeat_result["checks"]]
        for reference in manifest["authoritative_references"]:
            assert hashes(oracle)[reference] == initial_hash[reference]
        result = {"id": task, "initial_project": baseline_tests, "initial_acceptance": initial_result,
                  "oracle_project": oracle_tests, "oracle_acceptance": oracle_result,
                  "repeat_project": repeat_tests, "repeat_acceptance": repeat_result,
                  "initial_hashes": initial_hash, "oracle_hashes": hashes(oracle),
                  "deterministic_rebuild": True, "paid_model_requests": 0}
        if output:
            save(output / "manifest.json", manifest)
            save(output / "validation.json", result)
            save(output / "initial-hashes.json", initial_hash)
            for name, value in (("initial-acceptance", initial_result), ("oracle-acceptance", oracle_result),
                                ("baseline-tests", baseline_tests), ("oracle-tests", oracle_tests),
                                ("repeat-acceptance", repeat_result)):
                save(output / (name + ".json"), value)
            (output / "oracle-diff.patch").write_text(patch(initial, oracle))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "acceptance", "prepare", "snapshot"])
    parser.add_argument("task", choices=[*TASKS, "all"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mutation-generation", type=int)
    args = parser.parse_args()
    if args.action == "validate":
        tasks = TASKS if args.task == "all" else [args.task]
        for task in tasks:
            result = validate_task(task, args.path / task)
            print(task, result["initial_acceptance"]["summary"], "->", result["oracle_acceptance"]["summary"])
        return 0
    if args.task == "all":
        parser.error("all applies only to validation")
    if args.action == "prepare":
        print(json.dumps(prepare(args.task, args.path), indent=2))
        return 0
    if args.action == "snapshot":
        if args.output is None:
            parser.error("snapshot requires a new --output directory")
        result = capture_version(args.task, args.path, args.output, args.mutation_generation)
        print(result["acceptance"]["summary"])
        return result["acceptance"]["exit"]
    result = acceptance(args.task, args.path)
    print(json.dumps(result, indent=2))
    print(result["summary"])
    return result["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
