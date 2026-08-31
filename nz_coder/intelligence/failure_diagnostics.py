"""Compose ranked failure signals into one actionable runtime diagnostic."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiagnosticSignal:
    """One independently detected failure signal, ranked by specificity."""

    classification: str
    specificity: int
    evidence: str
    action: str
    repair_target: str = ""


def render_failure_diagnostic(signals: list[DiagnosticSignal]) -> str:
    """Render the most specific signal as primary without losing supporting facts."""
    ordered = sorted(signals, key=lambda item: item.specificity, reverse=True)
    if not ordered:
        return ""
    primary, *supporting = ordered
    parts = [
        "<test-failure-diagnostic>",
        f"classification: {primary.classification}",
        f"primary_classification: {primary.classification}",
    ]
    parts.extend(
        f"supporting_classification: {item.classification}"
        for item in supporting
        if item.classification != primary.classification
    )
    if primary.repair_target:
        parts.append(f"repair_target: {primary.repair_target}")
    if primary.evidence:
        parts.append(primary.evidence)
    if primary.action:
        parts.append(primary.action)
    parts.append("</test-failure-diagnostic>")
    return "\n\n".join(parts)


__all__ = ["DiagnosticSignal", "render_failure_diagnostic"]
