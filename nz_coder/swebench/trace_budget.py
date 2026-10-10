"""Bounded diagnostic trace retention for SWE-bench runs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


GIB = 1024 ** 3


@dataclass(frozen=True)
class TraceBudget:
    """Run-scoped archive thresholds expressed as exact byte counts."""

    archive_root: Path
    warning_bytes: int = 18 * GIB
    hard_limit_bytes: int = 20 * GIB
    cleanup_target_bytes: int = 15 * GIB

    def __post_init__(self) -> None:
        if not (
            0 < self.cleanup_target_bytes
            < self.warning_bytes
            < self.hard_limit_bytes
        ):
            raise ValueError(
                "trace budget requires 0 < cleanup_target_bytes "
                "< warning_bytes < hard_limit_bytes"
            )
        object.__setattr__(self, "archive_root", Path(self.archive_root))


@dataclass(frozen=True)
class TraceBudgetDecision:
    """Current archive pressure at an exact measurement boundary."""

    used_bytes: int
    warning: bool
    hard_limit_reached: bool


@dataclass(frozen=True)
class TraceArchiveResult:
    """Published bundle path plus the resulting archive pressure."""

    bundle_path: Path
    used_bytes: int
    warning: bool
    hard_limit_reached: bool
    cleanup_safe: bool = False


def measure_trace_archive(archive_root: Path) -> int:
    """Return bytes occupied by regular files without following symlinks."""
    root = Path(archive_root)
    if not root.exists():
        return 0
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def evaluate_trace_budget(
    budget: TraceBudget,
    *,
    used_bytes: int | None = None,
) -> TraceBudgetDecision:
    """Classify measured usage against warning and hard thresholds."""
    used = (
        measure_trace_archive(budget.archive_root)
        if used_bytes is None
        else max(0, int(used_bytes))
    )
    return TraceBudgetDecision(
        used_bytes=used,
        warning=used >= budget.warning_bytes,
        hard_limit_reached=used >= budget.hard_limit_bytes,
    )


def write_trace_budget_report(
    budget: TraceBudget,
    decision: TraceBudgetDecision,
) -> Path:
    """Atomically persist the current pressure state for operators."""
    root = Path(budget.archive_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "trace-budget-report.json"
    temporary = root / ".trace-budget-report.json.tmp"
    temporary.write_text(
        json.dumps({
            "archive_root": str(root),
            "used_bytes": decision.used_bytes,
            "warning_bytes": budget.warning_bytes,
            "hard_limit_bytes": budget.hard_limit_bytes,
            "cleanup_target_bytes": budget.cleanup_target_bytes,
            "warning": decision.warning,
            "hard_limit_reached": decision.hard_limit_reached,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    descriptor = os.open(temporary, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    _fsync_directory(root)
    return target


def archive_instance_diagnostics(
    *,
    instance_id: str,
    workdir: Path,
    run_root: Path,
    trace_path: Path,
    public_input_path: Path | None,
    metadata: dict,
    budget: TraceBudget,
) -> TraceArchiveResult:
    """Atomically publish one diagnostic bundle without deleting its source."""
    root = Path(run_root).resolve()
    workspace = Path(workdir).resolve()
    if workspace.parent != root:
        raise ValueError(
            f"refusing archive outside a direct child of run root: {workspace}"
        )
    safe_id = str(instance_id or "")
    if not safe_id or Path(safe_id).name != safe_id or safe_id in {".", ".."}:
        raise ValueError(f"invalid trace archive instance_id: {safe_id!r}")

    trace = _workspace_file(trace_path, workspace, "raw trace")
    public_input = (
        _workspace_file(public_input_path, workspace, "public inference input")
        if public_input_path is not None and Path(public_input_path).is_file()
        else None
    )
    archive_root = Path(budget.archive_root).resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    target = archive_root / safe_id
    if target.exists():
        raise ValueError(f"trace bundle already exists: {target}")
    temporary = archive_root / f".{safe_id}.tmp-{uuid.uuid4().hex}"

    try:
        temporary.mkdir(mode=0o700)
        _copy_with_quota(trace, temporary / "raw-trace.jsonl", temporary, budget)
        if public_input is not None:
            _copy_with_quota(public_input, temporary / "public-inference-input.json", temporary, budget)
        facts = trace.parent / "execution-facts.jsonl"
        if facts.is_file():
            facts = _workspace_file(facts, workspace, "execution facts")
            _copy_with_quota(facts, temporary / "execution-facts.jsonl", temporary, budget)
        # Session 证据在工作区外；只收集本次绑定的 Session，不打包整个私有目录。
        session_id = str(metadata.get("session_id") or "")
        evidence = {"trace_kind": "projected_and_bounded", "http_capture": "unavailable",
                    "session_id": session_id or None, "session_available": False, "artifact_ids": {}}
        if session_id:
            from nz_coder.runtime.process.workdir import scoped_workdir
            from nz_coder.state.sessions import session_artifact_dir, session_dir

            if not session_id.replace("-", "").replace("_", "").replace(".", "").isalnum() or session_id in {".", ".."}:
                raise ValueError("invalid archive Session identity")
            with scoped_workdir(workspace):
                sessions = session_dir()
                owned = session_artifact_dir(session_id)
            session_file = sessions / f"{session_id}.json"
            if session_file.is_file():
                _reject_symlink_path(session_file, sessions)
                recorded = json.loads(session_file.read_text(encoding="utf-8"))
                if Path(recorded["workspace"]).resolve() != workspace:
                    raise ValueError("archive Session belongs to another workspace")
                destination = temporary / "sessions" / session_file.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_with_quota(session_file, destination, temporary, budget)
                evidence["session_available"] = True
            if owned.is_dir():
                _reject_symlink_path(owned, sessions)
                _copy_regular_tree(owned, temporary / "sessions" / "_artifacts" / session_id,
                                   archive=temporary, budget=budget)
            manifest = temporary / "sessions" / "_artifacts" / session_id / "runtime" / "tool-results" / "manifest.json"
            if manifest.is_file():
                entries = json.loads(manifest.read_text(encoding="utf-8"))["entries"]
                for artifact_id, entry in entries.items():
                    path = manifest.parent / str(entry["filename"])
                    if path.parent != manifest.parent or not path.is_file():
                        raise ValueError("archive artifact reference is unavailable")
                    evidence["artifact_ids"][artifact_id] = {
                        "path": str(path.relative_to(temporary)),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        # 原始 HTTP 未被收集时明确标缺失，不以裁剪 trace 冒充无损请求。
        _write_with_quota(temporary / "evidence-index.json", evidence, budget)
        payload = {
            **dict(metadata),
            "instance_id": safe_id,
        }
        _write_with_quota(temporary / "metadata.json", payload, budget)
        if measure_trace_archive(archive_root) > budget.hard_limit_bytes:
            raise OSError("trace archive quota exceeded; source evidence retained")
        _fsync_tree(temporary)
        os.replace(temporary, target)
        _fsync_directory(archive_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    decision = evaluate_trace_budget(budget)
    return TraceArchiveResult(
        bundle_path=target,
        used_bytes=decision.used_bytes,
        warning=decision.warning,
        hard_limit_reached=decision.hard_limit_reached,
        cleanup_safe=bool(evidence["session_available"]),
    )


def _workspace_file(path: Path, workspace: Path, label: str) -> Path:
    _reject_symlink_path(Path(path).absolute(), workspace)
    candidate = Path(path).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(f"{label} is outside the instance workdir: {candidate}")
    if not candidate.is_file() or candidate.is_symlink():
        raise FileNotFoundError(f"{label} not found: {candidate}")
    return candidate


def _reject_symlink_path(path: Path, root: Path) -> None:
    if any(parent.is_symlink() for parent in (root, *root.parents)):
        raise ValueError("archive source contains a symbolic link")
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("archive source contains a symbolic link")


def _copy_with_quota(source: Path, target: Path, archive: Path, budget: TraceBudget) -> None:
    remaining = budget.hard_limit_bytes - measure_trace_archive(budget.archive_root)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while chunk := incoming.read(min(65536, max(1, remaining + 1))):
            if len(chunk) > remaining:
                raise OSError("trace archive quota exceeded; source evidence retained")
            outgoing.write(chunk)
            remaining -= len(chunk)


def _write_with_quota(target: Path, value: dict, budget: TraceBudget) -> None:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) + measure_trace_archive(budget.archive_root) > budget.hard_limit_bytes:
        raise OSError("trace archive quota exceeded; source evidence retained")
    target.write_bytes(raw)


def _copy_regular_tree(source: Path, target: Path, *, archive: Path, budget: TraceBudget) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copy_with_quota(path, destination, archive, budget)


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _fsync_directory(path)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
