"""Durable proposal, review, and apply control plane for learned memories."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time


_RISK_MARKERS = (
    "api key", "credential", "secret", "password", "token",
    "ignore previous", "bypass", "disable security", "allow every",
    "all projects", "across all projects", "shell command",
)


@dataclass(frozen=True)
class MemoryProposal:
    """Immutable candidate and its current governance decision."""

    source_session: str
    source_message_ids: tuple[str, ...]
    name: str
    description: str
    type: str
    content: str
    confidence: float
    reason: str
    created_at: float
    fingerprint: str
    risk: str
    status: str

    @classmethod
    def from_candidate(
        cls,
        candidate: dict,
        *,
        source_session: str,
        source_message_ids: tuple[str, ...] = (),
    ) -> MemoryProposal:
        normalized = {
            "name": str(candidate.get("name") or "").strip()[:200],
            "description": str(candidate.get("description") or "").strip()[:500],
            "type": str(candidate.get("type") or "project").strip().lower(),
            "content": str(candidate.get("content") or "").strip()[:4000],
        }
        if not all(normalized.values()):
            raise ValueError("Memory proposal requires name, description, type, and content")
        if normalized["type"] not in {"user", "project", "feedback", "reference"}:
            raise ValueError("Memory proposal type is invalid")
        confidence = float(candidate.get("confidence", 0.75) or 0.0)
        confidence = min(1.0, max(0.0, confidence))
        reason = str(candidate.get("reason") or "automatic extraction").strip()[:1000]
        canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        risk = _classify_risk(normalized, confidence)
        return cls(
            source_session=str(source_session or ""),
            source_message_ids=tuple(
                str(item) for item in source_message_ids if str(item).strip()
            ),
            confidence=confidence,
            reason=reason,
            created_at=time.time(),
            fingerprint=fingerprint,
            risk=risk,
            status="proposed",
            **normalized,
        )


class MemoryControlPlane:
    """Serialize proposal decisions before writing the existing memory store."""

    def __init__(self, root: Path, memory_manager) -> None:
        self._root = Path(root) / "memory-control"
        self._proposals = self._root / "proposals"
        self._ledger_path = self._root / "ledger.jsonl"
        self._memory_manager = memory_manager
        self._lock = threading.RLock()

    def submit(
        self,
        candidate: dict,
        *,
        source_session: str,
        source_message_ids: tuple[str, ...] = (),
    ) -> MemoryProposal:
        proposal = MemoryProposal.from_candidate(
            candidate,
            source_session=source_session,
            source_message_ids=source_message_ids,
        )
        with self._lock:
            existing = self._load(proposal.fingerprint)
            if existing is not None:
                duplicate = replace(proposal, status="duplicate")
                self._append("duplicate", duplicate)
                return duplicate
            self._store(proposal)
            self._append("proposed", proposal)
            if _can_auto_apply(proposal):
                return self._apply(proposal, reviewer="policy:auto-safe")
            pending = replace(proposal, status="pending_review")
            self._store(pending)
            self._append("queued_for_review", pending)
            return pending

    def approve(self, fingerprint: str, *, reviewer: str) -> MemoryProposal:
        with self._lock:
            proposal = self._required(fingerprint)
            if proposal.status != "pending_review":
                raise ValueError("Only pending memory proposals can be approved")
            return self._apply(proposal, reviewer=reviewer)

    def reject(self, fingerprint: str, *, reviewer: str, reason: str) -> MemoryProposal:
        with self._lock:
            proposal = self._required(fingerprint)
            if proposal.status != "pending_review":
                raise ValueError("Only pending memory proposals can be rejected")
            rejected = replace(proposal, status="rejected")
            self._store(rejected)
            self._append("rejected", rejected, reviewer=reviewer, reason=reason)
            return rejected

    def pending(self) -> list[MemoryProposal]:
        with self._lock:
            if not self._proposals.exists():
                return []
            items = []
            for path in sorted(self._proposals.glob("*.json")):
                proposal = self._decode(_read_json(path))
                if proposal is not None and proposal.status == "pending_review":
                    items.append(proposal)
            return items

    def get(self, fingerprint: str) -> MemoryProposal | None:
        """Return one proposal for an inspection-only product surface."""
        with self._lock:
            return self._load(str(fingerprint))

    def ledger(self) -> list[dict]:
        with self._lock:
            if not self._ledger_path.exists():
                return []
            events = []
            for line in self._ledger_path.read_text(encoding="utf-8").splitlines():
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    events.append(payload)
            return events

    def _apply(self, proposal: MemoryProposal, *, reviewer: str) -> MemoryProposal:
        result = self._memory_manager.save(
            proposal.name,
            proposal.description,
            proposal.type,
            proposal.content,
        )
        if str(result).startswith("Error:"):
            failed = replace(proposal, status="apply_failed")
            self._store(failed)
            self._append("apply_failed", failed, reviewer=reviewer, result=str(result))
            return failed
        applied = replace(proposal, status="applied")
        self._store(applied)
        self._append("applied", applied, reviewer=reviewer, result=str(result))
        return applied

    def _required(self, fingerprint: str) -> MemoryProposal:
        proposal = self._load(str(fingerprint))
        if proposal is None:
            raise KeyError(f"Unknown memory proposal: {fingerprint}")
        return proposal

    def _load(self, fingerprint: str) -> MemoryProposal | None:
        path = self._proposals / f"{fingerprint}.json"
        return self._decode(_read_json(path)) if path.exists() else None

    @staticmethod
    def _decode(payload: dict) -> MemoryProposal | None:
        if not isinstance(payload, dict) or not payload:
            return None
        try:
            payload = dict(payload)
            payload["source_message_ids"] = tuple(payload.get("source_message_ids", ()))
            return MemoryProposal(**payload)
        except (TypeError, ValueError):
            return None

    def _store(self, proposal: MemoryProposal) -> None:
        self._proposals.mkdir(parents=True, exist_ok=True)
        _atomic_json(
            self._proposals / f"{proposal.fingerprint}.json",
            asdict(proposal),
        )

    def _append(self, action: str, proposal: MemoryProposal, **extra) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": time.time(),
            "action": action,
            "fingerprint": proposal.fingerprint,
            "status": proposal.status,
            "risk": proposal.risk,
            "source_session": proposal.source_session,
            "source_message_ids": list(proposal.source_message_ids),
            **{key: str(value)[:1000] for key, value in extra.items()},
        }
        with self._ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _classify_risk(candidate: dict, confidence: float) -> str:
    text = " ".join(str(value).lower() for value in candidate.values())
    if confidence < 0.6 or any(marker in text for marker in _RISK_MARKERS):
        return "high"
    if candidate["type"] != "project":
        return "medium"
    if not any(token in text for token in ("project", "repository", "repo", "仓库", "项目")):
        return "medium"
    return "low"


def _can_auto_apply(proposal: MemoryProposal) -> bool:
    if proposal.risk == "high" or proposal.confidence < 0.85:
        return False
    if proposal.risk == "low" and proposal.type == "project":
        return True
    # An explicit "remember" request is already a human approval boundary;
    # model-inferred user/reference memories still require the review inbox.
    return proposal.reason == "explicit user memory request"


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
