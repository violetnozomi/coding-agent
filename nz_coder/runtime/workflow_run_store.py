"""Private artifacts, efficiency reports, and terminal workflow records."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")


class WorkflowRunStore:
    """Own durable, bounded non-executable output for one workflow run."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.artifacts_dir = self.run_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.run_dir.chmod(0o700)
            self.artifacts_dir.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def safe_name(name: str) -> str:
        cleaned = _SAFE_NAME.sub("_", str(name))[:120]
        return cleaned or "artifact"

    @staticmethod
    def _write(path: Path, value: Any, *, max_bytes: int) -> None:
        encoded = (json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ) + "\n").encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError(f"workflow output exceeds {max_bytes} bytes")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)

    def write_artifact(self, name: str, value: Any) -> dict:
        safe = self.safe_name(name)
        path = self.artifacts_dir / f"{safe}.json"
        self._write(path, value, max_bytes=_MAX_ARTIFACT_BYTES)
        return {"name": str(name)[:200], "path": f"artifacts/{safe}.json"}

    def write_terminal(self, record: dict) -> None:
        self._write(self.run_dir / "run.json", record, max_bytes=4 * 1024 * 1024)


def build_workflow_cost_report(states: list[dict], *, wall_clock_seconds: float) -> dict:
    """Aggregate typed child usage without inventing missing token coverage."""
    totals = {"input": 0, "output": 0, "cache_read": 0, "total": 0}
    missing: list[str] = []
    statuses: dict[str, int] = {}
    turns = 0
    for state in states:
        status = str(state.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        usage = state.get("tokens")
        if not isinstance(usage, dict):
            usage = (state.get("child_result") or {}).get("usage")
        if not isinstance(usage, dict) or not any(
            isinstance(value, (int, float)) for value in usage.values()
        ):
            missing.append(str(state.get("session_id") or ""))
        else:
            for key, aliases in {
                "input": ("input", "input_tokens"),
                "output": ("output", "output_tokens"),
                "cache_read": ("cache_read", "cache_read_tokens"),
                "total": ("total", "total_tokens"),
            }.items():
                value = next((usage.get(alias) for alias in aliases if alias in usage), 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] += max(0, int(value))
        iterations = state.get("iterations", 0)
        if isinstance(iterations, int) and not isinstance(iterations, bool):
            turns += max(0, iterations)
    if totals["total"] == 0:
        totals["total"] = totals["input"] + totals["output"]
    return {
        "model_tokens": totals,
        "agent_starts": len(states),
        "child_turns": turns,
        "statuses": statuses,
        "token_coverage": {
            "ok": not missing,
            "missing_task_ids": sorted(item for item in missing if item),
        },
        "wall_clock_duration_ms": max(0, int(wall_clock_seconds * 1000)),
        "generated_at": time.time(),
    }


def _safe_run_dir(runs_root: Path, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(str(run_id)) or ".." in str(run_id):
        raise ValueError("unsafe workflow run id")
    root = Path(runs_root).resolve()
    target = (root / str(run_id)).resolve()
    target.relative_to(root)
    return target


def read_workflow_run_record(runs_root: Path, run_id: str) -> dict:
    run_dir = _safe_run_dir(runs_root, run_id)
    path = run_dir / "run.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError(f"workflow run record not found: {run_id}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid workflow run record: {run_id}") from exc
    if not isinstance(value, dict) or value.get("run_id") != run_id:
        raise ValueError(f"invalid workflow run identity: {run_id}")
    return value


def list_workflow_run_records(runs_root: Path, limit: int = 100) -> list[dict]:
    root = Path(runs_root)
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    records = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or not _RUN_ID.fullmatch(entry.name):
            continue
        try:
            records.append(read_workflow_run_record(root, entry.name))
        except ValueError:
            continue
    records.sort(key=lambda item: float(item.get("ended_at") or 0), reverse=True)
    return records[:max(1, min(int(limit), 1000))]


def read_workflow_run_artifact(runs_root: Path, run_id: str, name: str) -> object:
    record = read_workflow_run_record(runs_root, run_id)
    reference = next(
        (
            item for item in record.get("artifacts", [])
            if isinstance(item, dict) and item.get("name") == name
        ),
        None,
    )
    if reference is None:
        raise ValueError(f"workflow artifact not found: {name}")
    run_dir = _safe_run_dir(runs_root, run_id)
    path = (run_dir / str(reference.get("path") or "")).resolve()
    path.relative_to((run_dir / "artifacts").resolve())
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"workflow artifact is unsafe: {name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid workflow artifact: {name}") from exc


def archive_workflow_run(runs_root: Path, run_id: str) -> Path:
    """Move one exact terminal run to recoverable trash instead of deleting it."""
    run_dir = _safe_run_dir(runs_root, run_id)
    read_workflow_run_record(runs_root, run_id)
    trash = Path(runs_root) / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    destination = trash / f"{run_id}-{time.time_ns()}"
    shutil.move(str(run_dir), str(destination))
    return destination
