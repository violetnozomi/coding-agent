"""Canonical typed result envelope shared by every child-Agent surface."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import math

from nz_coder.foundation.json_safety import json_safe_value


CHILD_RESULT_KEY = "child_result"
MESSAGE_CHILD_RESULT_KEY = "_nz_child_result"
_FINAL_TEXT_LIMIT = 16_000


@dataclass(frozen=True)
class ChildAgentResult:
    """JSON-safe terminal child result with legacy metadata projection."""

    task_id: str
    name: str
    status: str
    final_text: str
    session_id: str = ""
    agent_id: str = ""
    parent_session_id: str = ""
    trace_id: str = ""
    structured: object = None
    structured_present: bool = False
    digest: str = ""
    summary_kind: str = ""
    evidence_refs: tuple[str, ...] = ()
    verification: dict | None = None
    changed_files: tuple[str, ...] = ()
    conflicts: tuple[dict, ...] = ()
    provider: str = ""
    model: str = ""
    route_facts: dict | None = None
    limit_reached: bool = False
    interrupted: bool = False
    usage: dict = field(default_factory=dict)
    cost: float | None = None
    final_text_truncated: bool = False

    def __post_init__(self) -> None:
        for field_name in ("task_id", "name", "status"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"Child result requires non-empty {field_name}")

    def to_dict(self) -> dict:
        """Return the stable durable wire shape, omitting absent optionals."""
        payload = {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status,
            "final_text": self.final_text,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "parent_session_id": self.parent_session_id,
            "trace_id": self.trace_id,
            "evidence_refs": list(self.evidence_refs),
            "changed_files": list(self.changed_files),
            "conflicts": [copy.deepcopy(item) for item in self.conflicts],
            "provider": self.provider,
            "model": self.model,
            "limit_reached": self.limit_reached,
            "interrupted": self.interrupted,
            "usage": copy.deepcopy(self.usage),
            "final_text_truncated": self.final_text_truncated,
        }
        if self.structured_present:
            payload["structured"] = copy.deepcopy(self.structured)
        if self.digest:
            payload["digest"] = self.digest
        if self.summary_kind:
            payload["summary_kind"] = self.summary_kind
        if self.verification is not None:
            payload["verification"] = copy.deepcopy(self.verification)
        if self.route_facts is not None:
            payload["route_facts"] = copy.deepcopy(self.route_facts)
        if self.cost is not None:
            payload["cost"] = self.cost
        return payload

    def to_metadata(self) -> dict:
        """Publish the canonical object plus additive legacy child_* aliases."""
        verification_summary = ""
        if isinstance(self.verification, dict):
            verification_summary = str(self.verification.get("summary") or "")
        metadata = {
            CHILD_RESULT_KEY: self.to_dict(),
            "child_session_id": self.session_id,
            "child_agent_id": self.agent_id,
            "child_parent_session_id": self.parent_session_id,
            "child_trace_id": self.trace_id,
            "child_status": self.status,
            "child_changed_files": list(self.changed_files),
            "child_conflicts": [copy.deepcopy(item) for item in self.conflicts],
            "child_verification": verification_summary,
            "child_tokens": copy.deepcopy(self.usage),
        }
        if self.cost is not None:
            metadata["child_total_cost"] = self.cost
        return metadata

    def with_status(self, status: str, *, final_text: str | None = None):
        return replace(
            self,
            status=str(status),
            final_text=self.final_text if final_text is None else str(final_text),
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "ChildAgentResult":
        """Normalize an untrusted persisted envelope into bounded fields."""
        if not isinstance(payload, dict):
            raise ValueError("Child result payload must be an object")
        raw_text = str(payload.get("final_text") or "")
        truncated = len(raw_text) > _FINAL_TEXT_LIMIT
        verification = payload.get("verification")
        if not isinstance(verification, dict):
            verification = None
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        route_facts = payload.get("route_facts")
        if not isinstance(route_facts, dict):
            route_facts = None
        cost = _finite_nonnegative(payload.get("cost"))
        return cls(
            task_id=str(payload.get("task_id") or payload.get("session_id") or "unknown"),
            name=str(payload.get("name") or payload.get("agent_id") or "child"),
            status=str(payload.get("status") or "unknown"),
            final_text=raw_text[:_FINAL_TEXT_LIMIT],
            session_id=str(payload.get("session_id") or "")[:200],
            agent_id=str(payload.get("agent_id") or "")[:200],
            parent_session_id=str(payload.get("parent_session_id") or "")[:200],
            trace_id=str(payload.get("trace_id") or "")[:200],
            structured=json_safe_value(payload.get("structured")),
            structured_present="structured" in payload,
            digest=str(payload.get("digest") or "")[:4000],
            summary_kind=(
                str(payload.get("summary_kind") or "")[:40]
                if str(payload.get("summary_kind") or "")
                in {"digest", "excerpt", "digest-failed", "pending"}
                else ""
            ),
            evidence_refs=tuple(_strings(payload.get("evidence_refs"), 20)),
            verification=json_safe_value(verification),
            changed_files=tuple(_strings(payload.get("changed_files"), 50)),
            conflicts=tuple(_dicts(payload.get("conflicts"), 20)),
            provider=str(payload.get("provider") or "")[:120],
            model=str(payload.get("model") or "")[:240],
            route_facts=json_safe_value(route_facts),
            limit_reached=payload.get("limit_reached") is True,
            interrupted=payload.get("interrupted") is True,
            usage=_usage_metrics(usage),
            cost=cost,
            final_text_truncated=(
                truncated or payload.get("final_text_truncated") is True
            ),
        )

    @classmethod
    def from_metadata(
        cls,
        metadata: dict,
        *,
        final_text: str = "",
        name: str = "child",
    ) -> "ChildAgentResult | None":
        """Prefer the canonical envelope, then adapt legacy child_* metadata."""
        if not isinstance(metadata, dict):
            return None
        canonical = metadata.get(CHILD_RESULT_KEY)
        if isinstance(canonical, dict):
            return cls.from_dict(canonical)
        session_id = str(metadata.get("child_session_id") or "")
        if not session_id:
            return None
        verification = str(metadata.get("child_verification") or "")
        payload = {
            "task_id": session_id,
            "name": name,
            "status": str(metadata.get("child_status") or "unknown"),
            "final_text": final_text,
            "session_id": session_id,
            "agent_id": str(metadata.get("child_agent_id") or ""),
            "parent_session_id": str(
                metadata.get("child_parent_session_id") or ""
            ),
            "trace_id": str(metadata.get("child_trace_id") or ""),
            "changed_files": metadata.get("child_changed_files") or [],
            "conflicts": metadata.get("child_conflicts") or [],
            "usage": metadata.get("child_tokens") or {},
        }
        if verification:
            payload["verification"] = {
                "status": "unknown",
                "summary": verification,
            }
        return cls.from_dict(payload)


def child_result_from_state(
    state: dict,
    *,
    final_text: str,
    status: str,
    verification: str | dict = "",
) -> ChildAgentResult:
    """Build the canonical result from NZ's persistent child state owner."""
    structured_present = "structured_output" in state
    verification_payload = None
    state_verification = state.get("verification_result")
    if isinstance(state_verification, dict):
        verification_payload = copy.deepcopy(state_verification)
    elif isinstance(verification, dict):
        verification_payload = copy.deepcopy(verification)
    elif verification:
        verification_payload = {
            "status": (
                "failed" if "fail" in status or "error" in status else "passed"
            ),
            "summary": str(verification)[:1200],
        }
    cost = _finite_nonnegative(state.get("cost")) if state.get("cost_known") else None
    raw = {
        "task_id": str(state.get("session_id") or state.get("agent_id") or "child"),
        "name": str(state.get("display_name") or state.get("agent_type") or "child"),
        "status": status,
        "final_text": final_text,
        "session_id": str(state.get("session_id") or ""),
        "agent_id": str(state.get("agent_id") or ""),
        "parent_session_id": str(state.get("parent_session_id") or ""),
        "trace_id": str(state.get("trace_id") or ""),
        "changed_files": state.get("changed_files") or [],
        "conflicts": state.get("conflicts") or [],
        "provider": str(state.get("provider_id") or ""),
        "model": str(state.get("model_id") or ""),
        "limit_reached": status == "max_turns",
        "interrupted": status in {"cancelled", "interrupted"},
        "usage": state.get("tokens") or {},
        "evidence_refs": state.get("evidence_refs") or [],
    }
    route_facts = state.get("route_facts")
    if isinstance(route_facts, dict):
        route_facts = copy.deepcopy(route_facts)
        messages = state.get("messages")
        if isinstance(messages, list):
            route_facts["iterations"] = sum(
                1
                for message in messages
                if isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("_nz_synthetic") is not True
            )
        elif isinstance(state.get("iterations"), int):
            route_facts["iterations"] = max(0, int(state["iterations"]))
        tokens = state.get("tokens")
        if isinstance(tokens, dict):
            normalized_tokens = _usage_metrics(tokens)
            route_facts.update({
                "input_tokens": normalized_tokens.get("input", 0),
                "cache_read_tokens": normalized_tokens.get("cache_read", 0),
                "output_tokens": normalized_tokens.get("output", 0),
            })
        duration_ms = state.get("duration_ms")
        normalized_duration = _finite_nonnegative(duration_ms)
        if normalized_duration is not None:
            route_facts["duration_ms"] = round(normalized_duration, 3)
        raw["route_facts"] = route_facts
    if structured_present:
        raw["structured"] = copy.deepcopy(state.get("structured_output"))
    if verification_payload is not None:
        raw["verification"] = verification_payload
    if cost is not None:
        raw["cost"] = cost
    digest = str(state.get("digest") or "").strip()
    if digest:
        raw["digest"] = digest
        raw["summary_kind"] = str(state.get("summary_kind") or "excerpt")
    return ChildAgentResult.from_dict(raw)


def _strings(value: object, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item)[:1000] for item in value[:limit] if str(item).strip()]


def _dicts(value: object, limit: int) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    return [json_safe_value(item) for item in value[:limit] if isinstance(item, dict)]


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _usage_metrics(value: dict) -> dict[str, int]:
    """Normalize JSON metrics while rejecting booleans and non-finite floats."""
    result: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        number = float(raw)
        if not math.isfinite(number):
            continue
        result[str(key)[:80]] = max(0, int(number))
    return result
