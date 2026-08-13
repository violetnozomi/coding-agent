"""Immutable diff review packets and deterministic structured quality gates."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_MAX_DIFF_BYTES = 4 * 1024 * 1024
_DEFAULT_CHUNK_BYTES = 64 * 1024


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")[:80]
    return cleaned or "packet"


def _category(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if "test" in path.lower() or suffix in {".spec", ".feature"}:
        return "test"
    if suffix in {".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs", ".cpp", ".cc", ".c", ".h"}:
        return "source"
    if suffix in {".md", ".rst", ".txt"}:
        return "docs"
    if suffix in {".json", ".yaml", ".yml", ".toml", ".ini"}:
        return "config"
    return "other"


def _partition(path: str) -> str:
    parts = Path(path).parts
    area = "/".join(parts[:2]) if parts and parts[0] in {"packages", "clients"} else (parts[0] if len(parts) > 1 else "cross-cutting")
    return f"{area}/{_category(path)}"


def _captured_files(diff: str) -> list[dict]:
    matches = list(_DIFF_HEADER.finditer(diff))
    if not matches:
        return [{
            "path": "(cross-cutting)",
            "partition": "cross-cutting/other",
            "content": diff,
        }]
    files = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(diff)
        path = str(match.group(2)).replace("\\", "/")
        files.append({"path": path, "partition": _partition(path), "content": diff[start:end]})
    return sorted(files, key=lambda item: (item["partition"], item["path"]))


def _atomic_text(path: Path, text: str) -> None:
    encoded = text.encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def write_review_packets(
    *,
    workspace: Path,
    session_id: str,
    label: str,
    diff: str,
    requirements: list[str] | None = None,
    test_evidence: list[str] | None = None,
    routing_risk: str = "low",
    chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
) -> list[dict]:
    """Capture supplied diff bytes once; never reread Git while packetizing."""
    if not isinstance(diff, str) or not diff.strip():
        raise ValueError("review packet diff must be non-empty")
    if len(diff.encode("utf-8")) > _MAX_DIFF_BYTES:
        raise ValueError("review packet diff exceeds 4 MiB")
    if routing_risk not in {"low", "medium", "high"}:
        raise ValueError("review packet routing_risk is invalid")
    budget = max(4096, min(int(chunk_bytes), 256 * 1024))
    range_id = _sha256(diff)
    root = workspace.resolve()
    packet_dir = root / ".nz-coder" / "review-packets" / _safe(session_id) / range_id
    packet_dir.mkdir(parents=True, exist_ok=True)
    for directory in (packet_dir.parent.parent, packet_dir.parent, packet_dir):
        os.chmod(directory, 0o700)
    groups: dict[str, list[dict]] = {}
    for item in _captured_files(diff):
        groups.setdefault(item["partition"], []).append(item)
    output = []
    for index, (partition, files) in enumerate(sorted(groups.items())):
        evidence = "".join(item["content"] for item in files)
        chunks = []
        current: list[str] = []
        current_bytes = 0
        for character in evidence:
            encoded_size = len(character.encode("utf-8"))
            if current and current_bytes + encoded_size > budget:
                chunks.append("".join(current))
                current = []
                current_bytes = 0
            current.append(character)
            current_bytes += encoded_size
        if current:
            chunks.append("".join(current))
        chunk_refs = []
        stem = f"{index + 1:03d}-{_safe(partition)}"
        for chunk_index, content in enumerate(chunks or [""]):
            digest = _sha256(content)
            chunk_path = packet_dir / f"{stem}.chunk-{chunk_index + 1:03d}-{digest[:12]}.diff"
            _atomic_text(chunk_path, content)
            chunk_refs.append({"path": str(chunk_path), "content_hash": digest})
        metadata = {
            "content_hash": _sha256(json.dumps({
                "partition": partition,
                "paths": [item["path"] for item in files],
                "evidence": evidence,
                "requirements": requirements or [],
                "test_evidence": test_evidence or [],
            }, sort_keys=True)),
            "range_id": range_id,
            "partition_key": partition,
            "label": str(label)[:200],
            "scope_paths": [item["path"] for item in files],
            "risk_flags": ["routing-high"] if routing_risk == "high" else [],
            "evidence_chunks": chunk_refs,
            "requirements": [str(item)[:2000] for item in (requirements or [])],
            "test_evidence": [str(item)[:2000] for item in (test_evidence or [])],
            "requirements_present": any(str(item).strip() for item in (requirements or [])),
            "test_evidence_present": any(str(item).strip() for item in (test_evidence or [])),
        }
        packet_path = packet_dir / f"{stem}-{metadata['content_hash'][:12]}.json"
        _atomic_text(packet_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        metadata["packet_path"] = str(packet_path)
        output.append(metadata)
    return output


def review_quality_gate(values: list[Any]) -> dict:
    """Preserve actionable/unresolved findings; never infer approval from silence."""
    findings = []
    unverified = []
    review_outputs = 0

    def visit(value: Any) -> None:
        nonlocal review_outputs
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        structured = value.get("structured")
        if isinstance(structured, dict):
            if "findings" in structured or "unverified_requirements" in structured:
                review_outputs += 1
            visit(structured)
        if "findings" in value or "unverified_requirements" in value:
            review_outputs += 1
        for item in value.get("findings", []) if isinstance(value.get("findings"), list) else []:
            if isinstance(item, dict):
                disposition = str(item.get("disposition") or "unresolved")
                if disposition != "refuted":
                    findings.append(item)
        raw_unverified = value.get("unverified_requirements")
        if isinstance(raw_unverified, list):
            unverified.extend(str(item) for item in raw_unverified if str(item))
        for nested in value.values():
            if isinstance(nested, (dict, list)) and nested is not structured:
                visit(nested)

    visit(values)
    if review_outputs == 0:
        unverified.append("review output unavailable")
    unresolved = [
        item for item in findings
        if str(item.get("disposition") or "unresolved") == "unresolved"
    ]
    return {
        "actionable_findings": findings,
        "unresolved_findings": unresolved,
        "unverified_requirements": sorted(set(unverified)),
        "unqualified_approval_allowed": not findings and not unverified,
    }
