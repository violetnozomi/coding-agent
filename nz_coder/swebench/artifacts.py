"""Exact-once journals and sanitized inference-time trajectory exports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any


_SECRET_KEYS = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)",
    flags=re.IGNORECASE,
)


class AttemptJournal:
    """Append-only one-record-per-instance pass@1 journal."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        result = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid attempt journal at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict) or not row.get("instance_id"):
                raise ValueError(f"invalid attempt journal record at line {line_number}")
            result.append(row)
        return result

    def completed_ids(self) -> set[str]:
        return {
            str(row["instance_id"])
            for row in self.rows()
            if row.get("event") != "claim"
        }

    def attempted_ids(self) -> set[str]:
        """Return every durably claimed instance, including interrupted ones."""
        return {str(row["instance_id"]) for row in self.rows()}

    def claim(self, instance_id: str) -> None:
        """Durably claim inference, reusing a crash-interrupted open claim."""
        normalized = str(instance_id or "")
        if not normalized:
            raise ValueError("attempt claim requires instance_id")
        if normalized in self.completed_ids():
            raise ValueError(f"instance {normalized} already recorded")
        if normalized in self.attempted_ids():
            return
        self._append({
            "event": "claim",
            "instance_id": normalized,
            "attempt": 1,
        })

    def record(self, row: dict) -> None:
        instance_id = str(row.get("instance_id") or "")
        if not instance_id:
            raise ValueError("attempt record requires instance_id")
        if int(row.get("attempt", 0)) != 1:
            raise ValueError("strict pass@1 journal only accepts attempt=1")
        if instance_id in self.completed_ids():
            raise ValueError(f"instance {instance_id} already recorded")
        self._append({**dict(row), "event": "result"})

    def _append(self, row: dict) -> None:
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        descriptor = os.open(
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            os.write(descriptor, encoded.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write_predictions(self, path: Path) -> Path:
        """Atomically derive official predictions from committed attempts."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        lines = []
        for row in self.rows():
            if row.get("event") == "claim":
                continue
            prediction = row.get("prediction")
            if not isinstance(prediction, dict):
                raise ValueError(
                    f"attempt for {row['instance_id']} has no committed prediction"
                )
            lines.append(json.dumps(prediction, ensure_ascii=False, sort_keys=True))
        temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        os.replace(temporary, target)
        return target


def export_public_trajectory(
    trace_path: Path,
    output_path: Path,
    *,
    workspace: Path,
    preamble_path: Path | None = None,
) -> Path:
    """Export an existing inference trace without secrets or host paths."""
    source = Path(trace_path)
    target = Path(output_path)
    if not source.is_file():
        raise FileNotFoundError(f"trace not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    workspace_text = str(Path(workspace).resolve())
    lines = []
    if preamble_path is not None and Path(preamble_path).is_file():
        preamble = json.loads(Path(preamble_path).read_text(encoding="utf-8"))
        lines.append(json.dumps(
            _sanitize_public(preamble, workspace_text),
            ensure_ascii=False,
            sort_keys=True,
        ))
    for line_number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid trace JSONL at line {line_number}: {exc}") from exc
        sanitized = _sanitize_public(row, workspace_text)
        lines.append(json.dumps(sanitized, ensure_ascii=False, sort_keys=True))
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


def _sanitize_public(value: Any, workspace: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SECRET_KEYS.search(str(key))
                else _sanitize_public(item, workspace)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_public(item, workspace) for item in value]
    if isinstance(value, str):
        text = value.replace(workspace, "<workspace>")
        text = re.sub(
            r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+",
            r"\1[REDACTED]",
            text,
        )
        text = re.sub(
            r"(?i)\b((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            text,
        )
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
        return text
    return value
