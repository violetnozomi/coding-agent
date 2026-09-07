"""Small offline preparation driver for the frozen Linux V1 coding baseline.

This module owns task copies, artifacts and independent acceptance, never an
Agent loop. Paid execution stays fail-closed until model/pricing authorization
is frozen; fake-provider attempts are always separate from the twelve results.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from .catalog import AGENT_REVISION, TASK_SPECS, digest, manifest, source, task_files


ROOT = Path(__file__).resolve().parents[2]


def environment_versions() -> dict:
    packages = {}
    for name in ("pytest", "openai", "rich", "prompt_toolkit", "PyYAML", "tree-sitter", "watchfiles"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": platform.python_version(), "os": platform.system(), "packages": packages}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def isolated_environment(home: Path, python_path: Path | None = None) -> dict[str, str]:
    """Do not inherit API keys, proxy credentials, pytest plugins or NZ settings."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"), "XDG_CONFIG_HOME": str(home / "config"),
        "TMPDIR": str(home / "tmp"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "NZ_MCP_ENABLED": "0",
        "NZ_REFLECTION_ENABLED": "0", "NZ_PLANNING_ENABLED": "0",
    }
    (home / "tmp").mkdir(exist_ok=True)
    if python_path:
        env["PYTHONPATH"] = str(python_path)
    return env


def git(repo: Path, *args: str, env: dict | None = None) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo,
        env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    ).stdout


def materialize(spec: tuple, repo: Path, home: Path) -> str:
    """Create a fresh initial repository, never undo a previous attempt."""
    repo.mkdir(parents=True, mode=0o700)  # Existing directories are an error.
    for name, text in task_files(spec).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    env = isolated_environment(home)
    env.update(GIT_AUTHOR_NAME="Baseline Fixture", GIT_COMMITTER_NAME="Baseline Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.invalid", GIT_COMMITTER_EMAIL="fixture@example.invalid",
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
    git(repo, "init", "--quiet", env=env)
    git(repo, "add", "--all", env=env)
    git(repo, "commit", "--quiet", "-m", "Frozen synthetic initial task", env=env)
    return git(repo, "rev-parse", "HEAD", env=env).decode().strip()


def process(command: list[str], cwd: Path, env: dict, output: Path, timeout: float) -> dict:
    """Retain raw output privately; terminate only this fresh process group."""
    start = time.monotonic()
    record = {"exit_code": None, "timed_out": False, "exception": None,
              "cleanup_error": None, "elapsed_seconds": None}
    child = None
    try:
        with output.open("xb") as stream:
            output.chmod(0o600)
            child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                record["exit_code"] = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                record["timed_out"] = True
    except Exception as exc:
        record["exception"] = type(exc).__name__
    finally:
        if child is not None:
            # Even a normally exiting parent may have left owned descendants.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                record["cleanup_error"] = type(exc).__name__
            try:
                child.wait(timeout=2)
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except (subprocess.TimeoutExpired, OSError):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired, OSError) as exc:
                    record["cleanup_error"] = type(exc).__name__
        record["elapsed_seconds"] = round(time.monotonic() - start, 4)
    return record


def test_result(xml: Path, execution: dict) -> dict:
    """JUnit test counts, never text 'passed' or exit zero alone, decide acceptance."""
    counts = dict(collected=0, passed=0, failed=0, errors=0, skipped=0)
    try:
        cases = list(ET.parse(xml).getroot().iter("testcase"))
        for case in cases:
            counts["collected"] += 1
            key = ("errors" if case.find("error") is not None else
                   "failed" if case.find("failure") is not None else
                   "skipped" if case.find("skipped") is not None else "passed")
            counts[key] += 1
    except (OSError, ET.ParseError):
        counts["errors"] += 1
    passed = (execution["exit_code"] == 0 and not execution["timed_out"]
              and execution["exception"] is None and not execution["cleanup_error"]
              and counts["passed"] > 0 and not counts["failed"]
              and not counts["errors"] and not counts["skipped"])
    return {**execution, **counts, "accepted": passed}


def acceptance(spec: tuple, repo: Path, evaluator: Path) -> dict:
    """Original regressions and target assertions execute OUTSIDE model workspace."""
    evaluator.mkdir(parents=True, mode=0o700)
    env = isolated_environment(evaluator / "home", repo)
    records = {}
    for kind, text in (("target", source(spec[5])),
                       ("regression", task_files(spec)["tests/test_public.py"])):
        test_path = evaluator / f"test_{kind}.py"
        test_path.write_text(text, encoding="utf-8")
        xml = evaluator / f"{kind}.xml"
        outcome = process(
            [sys.executable, "-m", "pytest", "-q", "-c", "/dev/null", "--import-mode=importlib",
             "-p", "no:cacheprovider", f"--junitxml={xml}", str(test_path)],
            evaluator, env, evaluator / f"{kind}.log", 30,
        )
        records[kind] = test_result(xml, outcome)
    return records


def export_patch(repo: Path, destination: Path, home: Path) -> dict:
    """Owned fixture only: stage untracked files; include binary and mode changes."""
    env = isolated_environment(home)
    git(repo, "add", "--all", "--", ".", env=env)
    patch = git(repo, "diff", "--cached", "--binary", "--full-index", "HEAD", env=env)
    destination.write_bytes(patch)
    destination.chmod(0o600)
    names = git(repo, "diff", "--cached", "--name-only", "-z", "HEAD", env=env)
    changes = [name.decode("utf-8", errors="surrogateescape") for name in names.split(b"\0") if name]
    return {"file": destination.name, "sha256": hashlib.sha256(patch).hexdigest(),
            "bytes": len(patch), "changed_files": changes}


def replay(spec: tuple, patch: Path, directory: Path) -> dict:
    repo = directory / "repo"
    materialize(spec, repo, directory / "home")
    if patch.stat().st_size:
        git(repo, "apply", "--index", "--binary", str(patch.resolve()),
            env=isolated_environment(directory / "home"))
    return acceptance(spec, repo, directory / "evaluator")


class BudgetExceeded(RuntimeError):
    """No request may start without a known, affordable upper-bound reservation."""


class RequestBudget:
    """Conservative non-refundable reservations; offline protocol check, not pricing.

    No refund on unknown usage or failed requests. A live provider adapter must
    supply independently validated token/cost upper bounds for EVERY attempt,
    including retries and auxiliaries, before this can authorize paid execution.
    """

    def __init__(self, tokens: int, cost: Decimal | None):
        import threading
        self.tokens = tokens
        self.cost = cost
        self.reserved_tokens = 0
        self.reserved_cost = Decimal(0)
        self._lock = threading.Lock()

    def reserve(self, tokens: int, cost: Decimal | None) -> None:
        with self._lock:
            if (tokens <= 0 or cost is None or not cost.is_finite() or cost < 0
                    or self.cost is None or not self.cost.is_finite()
                    or self.reserved_tokens + tokens > self.tokens
                    or self.reserved_cost + cost > self.cost):
                raise BudgetExceeded("request reservation unavailable or exceeds budget")
            self.reserved_tokens += tokens
            self.reserved_cost += cost


def classify(*, execution: dict, runtime_status: str | None, patch_verified: bool,
             within_budget: bool | None, valid: bool = True) -> str:
    if not valid:
        return "environment_or_task_invalid"
    if within_budget is False or execution["timed_out"] or runtime_status in {"max_turns", "max_tokens"}:
        return "budget_exceeded"
    if (execution["exception"] or execution["cleanup_error"]
            or execution["exit_code"] != 0 or runtime_status != "completed"):
        return "runtime_error"
    if patch_verified and within_budget is True:
        return "success"
    return "functional_failure" if not patch_verified else "runtime_error"


def scope_violations(spec: tuple, changed_files: list[str]) -> list[str]:
    allowed = (spec[3], "tests/")
    return [name for name in changed_files if not any(
        name.startswith(prefix) if prefix.endswith("/") else name == prefix
        for prefix in allowed
    )]


def read_events(path: Path) -> tuple[dict | None, list[dict]]:
    events, result = [], None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "runtime_event":
            # No payloads, arguments, final text or private reasoning in public records.
            events.append({key: item.get(key) for key in ("event", "run_id", "session_id")})
        elif item.get("type") == "result":
            result = item
    return result, events


def offline_attempt(spec: tuple, directory: Path, *, max_requests: int = 6,
                    timeout: float = 90) -> dict:
    """One real native execution with ONLY the provider response boundary replaced."""
    directory.mkdir(parents=True, mode=0o700)
    repo, home = directory / "repo", directory / "home"
    materialize(spec, repo, home)
    session_id = f"baseline-offline-{uuid.uuid4().hex}"
    record = {"task_id": spec[0], "attempt_id": session_id, "evidence_kind": "offline/driver_validation",
              "counts_as_real_attempt": False, "usage": None, "cost": None,
              "started_at": datetime.now(timezone.utc).isoformat(), "patch": None,
              "patch_verified": False, "runtime_completed": False, "within_budget": None,
              "classification": "runtime_error", "cleanup": "workspace retained; no rollback"}
    execution = dict(exit_code=None, timed_out=False, exception=None, cleanup_error=None)
    try:
        env = isolated_environment(home, ROOT)
        execution = process(
            [sys.executable, "-m", "evaluation.linux_baseline.worker", str(repo),
             session_id, str(directory / "wiring.json"), str(max_requests)],
            repo, env, directory / "raw.jsonl", timeout,
        )
        result, events = read_events(directory / "raw.jsonl")
        wiring_path = directory / "wiring.json"
        wiring = json.loads(wiring_path.read_text()) if wiring_path.exists() else {}
        record.update(runtime_status=(result or {}).get("status"),
                      runtime_completed=bool(result and result.get("status") == "completed"),
                      events=events, wiring=wiring,
                      within_budget=False if wiring.get("budget_stopped") else
                      True if wiring else None)
        record["tool_calls"] = sum(e["event"] == "session.tool.started" for e in events)
        record["tool_failed_events"] = sum(e["event"] == "session.tool.failed" for e in events)
    except Exception as exc:
        execution["exception"] = type(exc).__name__
    finally:
        # Save even launch/parse exceptions BEFORE any optional cleanup.
        record["execution"] = execution
        write_json(directory / "result.json", record)
        try:
            record["patch"] = export_patch(repo, directory / "final.patch", home)
            record["scope_violations"] = scope_violations(spec, record["patch"]["changed_files"])
            write_json(directory / "result.json", record)
            record["acceptance"] = replay(spec, directory / "final.patch", directory / "replay")
            record["patch_verified"] = (not record["scope_violations"] and
                                        all(item["accepted"] for item in record["acceptance"].values()))
        except Exception as exc:
            record["artifact_error"] = type(exc).__name__
        record["classification"] = classify(
            execution=execution, runtime_status=record.get("runtime_status"),
            patch_verified=record["patch_verified"], within_budget=record["within_budget"],
        )
        if record.get("artifact_error"):
            record["classification"] = "runtime_error"
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(directory / "result.json", record)
    return record


def prepare(output: Path, *, dry_run: bool = False) -> dict:
    output.mkdir(parents=True, mode=0o700)  # Never overwrite previous experiments.
    definitions = manifest()
    # Verify product files are unchanged relative to the frozen revision. Harness
    # commits need not have the same HEAD; record both revisions independently.
    dirty_product = git(ROOT, "diff", AGENT_REVISION, "--", "nz_coder", "pyproject.toml")
    if dirty_product:
        raise ValueError("Product changed: refreeze agent_revision before preparation")
    harness_revision = git(ROOT, "rev-parse", "HEAD").decode().strip()
    harness_dirty = bool(git(ROOT, "status", "--porcelain", "--", "evaluation/linux_baseline", "tests/evaluation/test_linux_baseline.py"))
    write_json(output / "manifest.json", definitions)
    checks, results = [], []
    for spec in TASK_SPECS:
        task_dir = output / "preparation" / spec[0]
        repo = task_dir / "repo"
        revision = materialize(spec, repo, task_dir / "home")
        check = acceptance(spec, repo, task_dir / "evaluator")
        target = check["target"]
        valid = (target["exit_code"] == 1 and target["failed"] > 0 and target["errors"] == 0
                 and not target["skipped"] and not target["timed_out"]
                 and check["regression"]["accepted"])
        checks.append(dict(task_id=spec[0], initial_git_revision=revision, valid=valid, **check))
        # Positive control uses a different copy and is never exposed to NZ-Coder.
        from .reference import apply_reference
        reference_dir = task_dir / "organizer-reference"
        materialize(spec, reference_dir / "repo", reference_dir / "home")
        apply_reference(spec[0], reference_dir / "repo")
        reference_patch = reference_dir / "reference.patch"
        export_patch(reference_dir / "repo", reference_patch, reference_dir / "home")
        reference_check = replay(spec, reference_patch, reference_dir / "replay")
        checks[-1]["organizer_reference_replay"] = reference_check
        checks[-1]["valid"] = valid and all(item["accepted"] for item in reference_check.values())
        results.append(dict(
            experiment_id=output.name, task_id=spec[0], attempt_id=None,
            agent_revision=AGENT_REVISION, harness_revision=harness_revision,
            task_revision=digest(task_files(spec)), classification="not_run",
            entry=definitions["entry"], runtime_profile="main (declared-agent-graph)",
            provider=None, model=None, effort=None, model_config_frozen=False,
            patch_verified=None, runtime_completed=None, within_budget=None,
            usage=None, usage_source=None, cost=None, elapsed_seconds=None,
            stop_reason="paid_call_authorization_and_total_budget_missing",
            independent_acceptance=None, initial_validation=check,
        ))
        write_json(output / "preparation.json", checks)
        write_json(output / "results.json", results)
    offline = []
    if dry_run and all(check["valid"] for check in checks):
        batch = RequestBudget(130, Decimal(13))
        for task_id in definitions["pilot_task_ids"]:
            spec = next(s for s in TASK_SPECS if s[0] == task_id)
            offline.append(admitted_offline_attempt(spec, output / "offline" / task_id, batch))
        offline.append(admitted_offline_attempt(TASK_SPECS[0], output / "offline" / "budget-stop", batch, max_requests=1))
    summary = dict(
        experiment_id=output.name, evidence_kind="offline/preparation",
        delivery="A: offline preparation; real baseline not executed",
        agent_revision=AGENT_REVISION, harness_revision=harness_revision,
        harness_dirty=harness_dirty, manifest_sha256=digest(definitions),
        environment=environment_versions(),
        model_config_status="not authorized/frozen", real_model_calls=0,
        paid_consumption=0, paid_consumption_basis="no real provider instantiated",
        planned=12, started=0, runtime_completed=0, end_to_end_success=0,
        patch_verified=0, failures=0, not_run=12, started_with_unknown_usage=0,
        success_rate=None, initial_tasks_valid=sum(c["valid"] for c in checks),
        offline_wiring_valid=offline_wiring_valid(offline) if dry_run else None,
        offline_attempts=offline,
        limitations=["synthetic local tasks, not open-source bug benchmark",
                     "no interactive CLI, daemon/HTTP, Windows or SWE-bench validation",
                     "temporary directories are not an OS sandbox",
                     "live pricing/admission adapter must be frozen and checked after authorization"],
    )
    write_json(output / "summary.json", summary)
    return summary


def publish(output: Path, destination: Path) -> None:
    """Publish only organizer-created structured projections, never raw model text."""
    summary = json.loads((output / "summary.json").read_text())
    if summary.get("evidence_kind") != "offline/preparation" or summary.get("real_model_calls") != 0:
        raise ValueError("This exporter is offline-only; real trajectories require a separate privacy review")
    destination.mkdir(parents=True, mode=0o700)
    for name in ("manifest.json", "preparation.json", "results.json", "summary.json"):
        payload = json.loads((output / name).read_text())
        encoded = json.dumps(payload, ensure_ascii=False)
        if "/home/" in encoded or "/tmp/" in encoded:
            raise ValueError("Private host path in public projection")
        write_json(destination / name, payload)
    for record in summary["offline_attempts"]:
        task_name = "budget-stop" if record["wiring"].get("budget_stopped") else record["task_id"]
        patch = (output / "offline" / task_name / "final.patch").read_bytes()
        if hashlib.sha256(patch).hexdigest() != record["patch"]["sha256"]:
            raise ValueError("Patch changed since evidence capture")
        (destination / f"offline-{task_name}.patch").write_bytes(patch)


def admitted_offline_attempt(spec: tuple, directory: Path, batch: RequestBudget, *, max_requests: int = 6) -> dict:
    """Reserve the whole offline task before allocating a process or workspace."""
    batch.reserve(max_requests * 10, Decimal(max_requests))
    return offline_attempt(spec, directory, max_requests=max_requests)


def offline_wiring_valid(records: list[dict]) -> bool:
    if len(records) != 3:
        return False
    for record in records[:2]:
        wiring = record.get("wiring", {})
        # A scripted provider may hit the real completion gate. Collecting that
        # terminal status faithfully is a valid wiring check, not a solved task.
        if (record.get("runtime_status") not in {"completed", "max_turns"} or
                record.get("execution", {}).get("exception") or record.get("artifact_error") or
                wiring.get("network_attempts") != 0 or not wiring.get("private_state_in_isolated_home") or
                not wiring.get("session_files_in_isolated_home") or
                "tests/driver_probe.txt" not in (record.get("patch") or {}).get("changed_files", [])):
            return False
    return (records[0]["attempt_id"] != records[1]["attempt_id"]
            and records[2]["classification"] == "budget_exceeded"
            and records[2].get("wiring", {}).get("provider_calls") == 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="also exercise real native chain with Fake Provider")
    parser.add_argument("--live", action="store_true", help="P1 only: explicit authorization, T01 then T04, one attempt each")
    parser.add_argument("--live-config", type=Path, help="user-confirmed P1 grant and frozen billing configuration; never implicit")
    parser.add_argument("--publish", type=Path, help="new durable directory for safe offline evidence")
    args = parser.parse_args(argv)
    if args.live:
        if args.live_config is None or args.dry_run or args.publish:
            parser.error("Paid execution disabled: require --live-config; cannot combine with offline options")
        from .live import run
        try:
            summary = run(args.output.resolve(), json.loads(args.live_config.read_text()))
        except Exception as exc:
            # Config values and upstream exception strings may contain credentials.
            parser.error(f"P1 stopped ({type(exc).__name__}); inspect the authorization and private evidence")
        print(json.dumps({"stopped": summary["stopped"], "tasks": {
            key: value["final_status"] for key, value in summary["tasks"].items()}}, indent=2))
        return 0 if all(summary["tasks"][key]["final_status"] == "success" for key in ("T01", "T04")) else 1
    if args.live_config:
        parser.error("--live-config requires --live; refusing to rebuild P0")
    summary = prepare(args.output.resolve(), dry_run=args.dry_run)
    if args.publish:
        publish(args.output.resolve(), args.publish.resolve())
    print(json.dumps({key: value for key, value in summary.items() if key != "offline_attempts"}, indent=2))
    return 0 if (summary["initial_tasks_valid"] == 12 and
                 (not args.dry_run or summary["offline_wiring_valid"])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
