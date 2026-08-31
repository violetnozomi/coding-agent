"""Validation and construction of official-style SWE-bench submissions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil

from nz_coder.swebench.artifacts import AttemptJournal
from nz_coder.swebench.profiles import BenchmarkProfile, instance_ids_digest
from nz_coder.swebench.policy import STRICT_ALLOWED_TOOLS


@dataclass(frozen=True)
class SubmissionValidation:
    """Fail-closed submission eligibility result."""

    eligible: bool
    errors: tuple[str, ...]
    prediction_count: int


def validate_submission_inputs(
    *,
    profile: BenchmarkProfile,
    predictions_path: Path,
    manifest: dict,
    trajectories_dir: Path,
    logs_dir: Path,
    attempt_journal_path: Path | None = None,
) -> SubmissionValidation:
    """Validate schema, pass@1 provenance, trajectories, and official logs."""
    errors: list[str] = []
    rows = _load_predictions(Path(predictions_path), errors)
    evaluation_run_id = _validate_evaluation_provenance(
        manifest,
        Path(predictions_path),
        errors,
    )
    ids = [str(row.get("instance_id") or "") for row in rows]
    unique_ids = set(ids)
    if len(rows) != profile.expected_instances:
        errors.append(
            f"expected {profile.expected_instances} predictions for {profile.name}, got {len(rows)}"
        )
    if len(unique_ids) != len(ids):
        errors.append("prediction instance_ids must be unique")
    for index, row in enumerate(rows, start=1):
        if set(row) != {"instance_id", "model_name_or_path", "model_patch"}:
            errors.append(f"prediction {index} has invalid schema")
        if not all(isinstance(row.get(key), str) for key in row):
            errors.append(f"prediction {index} fields must be strings")
    required_manifest = {
        "leaderboard_eligible": True,
        "strict_mode": True,
        "partial_selection": False,
        "hints_used": False,
        "official_test_knowledge_used": False,
        "answer_search_network_enabled": False,
        "public_trajectories": True,
        "attempts_per_instance": 1,
        "benchmark_profile": profile.name,
        "dataset": profile.dataset,
        "dataset_instance_ids_sha256": profile.instance_ids_sha256,
        "expected_instance_ids_sha256": profile.instance_ids_sha256,
    }
    for key, expected in required_manifest.items():
        if manifest.get(key) != expected:
            errors.append(
                f"manifest {key} must be {expected!r}, got {manifest.get(key)!r}"
            )
    manifest_ids = manifest.get("instance_ids")
    if not isinstance(manifest_ids, list) or len(manifest_ids) != len(set(manifest_ids)):
        errors.append("manifest instance_ids must be a unique array")
    elif set(str(item) for item in manifest_ids) != unique_ids:
        errors.append("manifest instance_ids do not match predictions")
    if profile.instance_ids_sha256 and instance_ids_digest(ids) != profile.instance_ids_sha256:
        errors.append("prediction instance_ids do not match the official profile set")
    source_digest = manifest.get("source_sha256")
    if not isinstance(source_digest, str) or not re_full_sha256(source_digest):
        errors.append("manifest source_sha256 is missing or invalid")
    _validate_attempt_journal(
        attempt_journal_path,
        rows,
        errors,
    )
    rows_by_id = {
        str(row.get("instance_id") or ""): row
        for row in rows
    }
    for instance_id in sorted(unique_ids):
        trajectory_path = Path(trajectories_dir) / f"{instance_id}.jsonl"
        if not trajectory_path.is_file():
            errors.append(f"missing trajectory for {instance_id}")
        else:
            _validate_trajectory(trajectory_path, instance_id, errors)
        prediction = rows_by_id[instance_id]
        instance_logs = _find_instance_log_dir(
            Path(logs_dir),
            instance_id,
            run_id=evaluation_run_id,
            model_name=str(prediction.get("model_name_or_path") or ""),
        )
        for filename in ("report.json", "test_output.txt", "patch.diff"):
            if not (instance_logs / filename).is_file():
                errors.append(f"missing official log {instance_id}/{filename}")
        official_patch = instance_logs / "patch.diff"
        if official_patch.is_file():
            try:
                patch_text = official_patch.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"cannot read official patch {instance_id}: {exc}")
            else:
                if patch_text != str(prediction.get("model_patch") or ""):
                    errors.append(f"official patch mismatch for {instance_id}")
    return SubmissionValidation(not errors, tuple(errors), len(rows))


def build_submission_bundle(
    *,
    profile: BenchmarkProfile,
    predictions_path: Path,
    manifest_path: Path,
    trajectories_dir: Path,
    logs_dir: Path,
    output_dir: Path,
    metadata: dict,
    attempt_journal_path: Path | None = None,
) -> Path:
    """Build a public bundle only after every eligibility check passes."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    validation = validate_submission_inputs(
        profile=profile,
        predictions_path=predictions_path,
        manifest=manifest,
        trajectories_dir=trajectories_dir,
        logs_dir=logs_dir,
        attempt_journal_path=(
            Path(attempt_journal_path)
            if attempt_journal_path is not None
            else Path(predictions_path).with_suffix(".attempts.jsonl")
        ),
    )
    if not validation.eligible:
        raise ValueError("submission is ineligible:\n- " + "\n- ".join(validation.errors))
    target = Path(output_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"submission directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(predictions_path, target / "all_preds.jsonl")
    public_manifest = dict(manifest)
    shutil.copytree(trajectories_dir, target / "trajs")
    target_logs = target / "logs"
    target_logs.mkdir()
    predictions = {
        row["instance_id"]: row
        for row in _load_predictions(Path(predictions_path), [])
    }
    evaluation_run_id = str(
        (manifest.get("official_evaluation") or {}).get("run_id") or ""
    )
    resolved = 0
    for instance_id, prediction in predictions.items():
        source_logs = _find_instance_log_dir(
            Path(logs_dir),
            instance_id,
            run_id=evaluation_run_id,
            model_name=str(prediction.get("model_name_or_path") or ""),
        )
        instance_target = target_logs / instance_id
        instance_target.mkdir()
        shutil.copy2(source_logs / "report.json", instance_target / "report.json")
        shutil.copy2(source_logs / "test_output.txt", instance_target / "test_output.txt")
        shutil.copy2(source_logs / "patch.diff", instance_target / "patch.diff")
        if _report_resolved(source_logs / "report.json", instance_id):
            resolved += 1
    metadata_payload = {
        **metadata,
        "benchmark": profile.name,
        "attempts": 1,
        "resolved": resolved,
        "total": len(predictions),
    }
    (target / "metadata.yaml").write_text(
        _simple_yaml(metadata_payload), encoding="utf-8"
    )
    digest = hashlib.sha256((target / "all_preds.jsonl").read_bytes()).hexdigest()
    public_manifest["artifact_sha256"] = {
        "all_preds.jsonl": digest,
        "trajs": _tree_digest(target / "trajs"),
        "logs": _tree_digest(target / "logs"),
    }
    (target / "manifest.json").write_text(
        json.dumps(public_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (target / "README.md").write_text(
        f"# NZ-Coder SWE-bench {profile.name.title()} submission\n\n"
        "Strict pass@1; no hints, official test knowledge, or answer-searching network tools.\n\n"
        f"Official result: **{resolved}/{len(predictions)} resolved "
        f"({(100.0 * resolved / len(predictions)):.2f}%)**.\n\n"
        f"Predictions SHA-256: `{digest}`\n",
        encoding="utf-8",
    )
    return target


def _load_predictions(path: Path, errors: list[str]) -> list[dict]:
    if not path.is_file():
        errors.append(f"predictions file not found: {path}")
        return []
    rows = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid predictions JSONL at line {line_number}: {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"prediction {line_number} must be an object")
            continue
        rows.append(row)
    return rows


def _simple_yaml(value: dict) -> str:
    lines = []
    for key, item in sorted(value.items()):
        encoded = json.dumps(item, ensure_ascii=False)
        lines.append(f"{key}: {encoded}")
    return "\n".join(lines) + "\n"


def _find_instance_log_dir(
    logs_dir: Path,
    instance_id: str,
    *,
    run_id: str,
    model_name: str,
) -> Path:
    """Resolve exactly one official run/model/instance directory."""
    root = Path(logs_dir)
    run = _safe_relative_parts(run_id)
    model = _safe_relative_parts(model_name)
    instance = _safe_relative_parts(instance_id)
    if not run or not model or len(instance) != 1:
        return root / "__invalid_official_provenance__" / str(instance_id)
    run_root = root if root.name == run[-1] and len(run) == 1 else root.joinpath(*run)
    return run_root.joinpath(*model, instance[0])


def _safe_relative_parts(value: str) -> tuple[str, ...]:
    normalized = str(value or "").replace("\\", "/")
    parts = tuple(normalized.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return ()
    return parts


def _validate_evaluation_provenance(
    manifest: dict,
    predictions_path: Path,
    errors: list[str],
) -> str:
    provenance = manifest.get("official_evaluation")
    if not isinstance(provenance, dict):
        errors.append("manifest official_evaluation provenance is missing")
        return ""
    run_id = str(provenance.get("run_id") or "")
    if len(_safe_relative_parts(run_id)) != 1:
        errors.append("manifest official_evaluation run_id is invalid")
        run_id = ""
    expected_digest = str(provenance.get("predictions_sha256") or "")
    actual_digest = (
        hashlib.sha256(Path(predictions_path).read_bytes()).hexdigest()
        if Path(predictions_path).is_file() else ""
    )
    if not re_full_sha256(expected_digest) or expected_digest != actual_digest:
        errors.append("manifest official_evaluation predictions_sha256 mismatch")
    return run_id


def record_official_evaluation_provenance(
    manifest_path: Path,
    predictions_path: Path,
    run_id: str,
) -> Path:
    """Bind a successful official harness run to exact prediction bytes."""
    target = Path(manifest_path)
    manifest = json.loads(target.read_text(encoding="utf-8"))
    if len(_safe_relative_parts(run_id)) != 1:
        raise ValueError("official evaluation run_id must be one safe path component")
    predictions = Path(predictions_path)
    if not predictions.is_file():
        raise FileNotFoundError(f"predictions file not found: {predictions}")
    manifest["official_evaluation"] = {
        "run_id": str(run_id),
        "predictions_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest(),
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def _validate_trajectory(path: Path, instance_id: str, errors: list[str]) -> None:
    """Reject forbidden tools or malformed post-hoc-looking trajectories."""
    rows = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"invalid trajectory JSONL {instance_id}:{line_number}")
            return
        if isinstance(row, dict):
            rows.append(row)
    if not rows:
        errors.append(f"empty trajectory for {instance_id}")
        return
    events = {str(row.get("event") or "") for row in rows}
    if "inference_not_started" in events:
        errors.append(f"inference never started for {instance_id}")
    if "benchmark_instance" not in events or "llm_request" not in events:
        errors.append(f"trajectory lacks inference-time prompt/request evidence for {instance_id}")
    for row in rows:
        if row.get("event") != "tool_call":
            continue
        name = str(row.get("name") or row.get("tool") or "")
        if name and name not in STRICT_ALLOWED_TOOLS:
            errors.append(f"forbidden tool in trajectory {instance_id}: {name}")


def _report_resolved(path: Path, instance_id: str) -> bool:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if isinstance(report, dict) and isinstance(report.get(instance_id), dict):
        report = report[instance_id]
    return bool(report.get("resolved")) if isinstance(report, dict) else False


def _validate_attempt_journal(
    path: Path | None,
    predictions: list[dict],
    errors: list[str],
) -> None:
    if path is None or not Path(path).is_file():
        errors.append("strict attempt journal is missing")
        return
    try:
        rows = AttemptJournal(Path(path)).rows()
    except ValueError as exc:
        errors.append(str(exc))
        return
    by_id: dict[str, list[dict]] = {}
    for row in rows:
        by_id.setdefault(str(row.get("instance_id") or ""), []).append(row)
    expected = {str(row.get("instance_id") or ""): row for row in predictions}
    if set(by_id) != set(expected):
        errors.append("attempt journal instance_ids do not match predictions")
        return
    for instance_id, records in by_id.items():
        claims = [row for row in records if row.get("event") == "claim"]
        results = [row for row in records if row.get("event") == "result"]
        if len(claims) != 1 or len(results) != 1:
            errors.append(
                f"attempt journal requires one claim and one result for {instance_id}"
            )
            continue
        if any(int(row.get("attempt", 0)) != 1 for row in records):
            errors.append(f"attempt journal is not pass@1 for {instance_id}")
        if results[0].get("prediction") != expected[instance_id]:
            errors.append(f"attempt journal prediction mismatch for {instance_id}")


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def _tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        hasher.update(len(relative).to_bytes(4, "big"))
        hasher.update(relative)
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()
