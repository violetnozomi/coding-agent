"""Secret-free reproducibility manifests for benchmark artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
import uuid

from nz_coder import __version__


_RESUME_IDENTITY_FIELDS = (
    "benchmark_profile",
    "dataset",
    "split",
    "instance_ids",
    "dataset_instance_ids_sha256",
    "model_name_or_path",
    "provider",
    "model_id",
    "max_agent_turns",
    "agent_timeout_seconds",
    "source_sha256",
    "attempts_per_instance",
    "strict_mode",
    "trace_retention",
)


def source_tree_digest(package_root: Path | None = None) -> str:
    """Hash the installed Python source without requiring a Git checkout."""
    root = (package_root or Path(__file__).resolve().parents[1]).resolve()
    hasher = hashlib.sha256()
    files = sorted(root.rglob("*.py"))
    for path in files[:5000]:
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            raise ValueError(f"source file exceeds reproducibility limit: {relative.decode()}")
        hasher.update(len(relative).to_bytes(4, "big"))
        hasher.update(relative)
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


def build_swebench_manifest(
    *,
    run_id: str,
    dataset: str,
    split: str,
    instance_ids: list[str],
    model_name: str,
    provider: str,
    model_id: str,
    max_agent_turns: int,
    agent_timeout_seconds: int,
    benchmark_profile: str = "lite",
    expected_instances: int | None = None,
    strict: bool = False,
    partial_selection: bool = False,
    expected_instance_ids_sha256: str = "",
) -> dict:
    """Build the fixed, credential-free identity of one first-pass run."""
    from nz_coder.swebench.profiles import instance_ids_digest

    actual_ids_sha256 = instance_ids_digest(instance_ids)
    return {
        "schema_version": 2,
        "benchmark": f"swe-bench-{benchmark_profile}",
        "benchmark_profile": str(benchmark_profile),
        "dataset": str(dataset),
        "split": str(split),
        "run_id": str(run_id),
        "created_at": time.time(),
        "instance_count": len(instance_ids),
        "expected_instance_count": (
            int(expected_instances) if expected_instances is not None else len(instance_ids)
        ),
        "instance_ids": list(instance_ids),
        "dataset_instance_ids_sha256": actual_ids_sha256,
        "expected_instance_ids_sha256": str(expected_instance_ids_sha256),
        "model_name_or_path": str(model_name),
        "provider": str(provider),
        "model_id": str(model_id),
        "max_agent_turns": int(max_agent_turns),
        "agent_timeout_seconds": int(agent_timeout_seconds),
        "nz_coder_version": __version__,
        "source_sha256": source_tree_digest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "process": {"executable": Path(sys.executable).name, "pid": os.getpid()},
        "result_policy": "strict pass@1; one committed inference attempt per instance",
        "attempts_per_instance": 1,
        "strict_mode": bool(strict),
        "partial_selection": bool(partial_selection),
        "hints_used": False,
        "official_test_knowledge_used": False,
        "answer_search_network_enabled": False,
        "public_trajectories": True,
        "leaderboard_eligible": bool(
            strict
            and benchmark_profile == "verified"
            and not partial_selection
            and expected_instances is not None
            and len(instance_ids) == int(expected_instances)
            and bool(expected_instance_ids_sha256)
            and actual_ids_sha256 == str(expected_instance_ids_sha256)
        ),
    }


def write_reproducibility_manifest(path: Path, manifest: dict) -> Path:
    """Atomically persist one bounded manifest next to benchmark artifacts."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("reproducibility manifest exceeds 2 MiB")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    return target


def validate_swebench_resume(existing: dict, candidate: dict) -> list[str]:
    """Return identity changes that would mix incomparable pass@1 attempts."""
    errors = []
    for key in _RESUME_IDENTITY_FIELDS:
        before = existing.get(key)
        after = candidate.get(key)
        if before != after:
            errors.append(f"{key} changed: {before!r} -> {after!r}")
    return errors
