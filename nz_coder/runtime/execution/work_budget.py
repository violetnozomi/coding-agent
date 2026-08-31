"""Run-level convergence pressure derived from the admitted turn budget."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class WorkBudgetNotice:
    """One newly crossed work-budget pressure boundary."""

    zone: str
    completed_turns: int
    max_turns: int
    message: str


@dataclass(frozen=True)
class EmergencyEligibility:
    """Legacy diagnostic facts retained for persisted-state compatibility."""

    eligible: bool
    has_diff: bool
    failure_evidence_exists: bool
    repair_target_known: bool
    needs_broad_exploration: bool
    reason: str


def evaluate_emergency_extension(
    *,
    has_diff: bool,
    failure_evidence_exists: bool,
    repair_target_known: bool,
    needs_broad_exploration: bool,
) -> EmergencyEligibility:
    """Describe legacy repair facts without authorizing or denying runtime work."""
    facts = {
        "has_diff": bool(has_diff),
        "failure_evidence_exists": bool(failure_evidence_exists),
        "repair_target_known": bool(repair_target_known),
        "needs_broad_exploration": bool(needs_broad_exploration),
    }
    eligible = bool(
        facts["has_diff"]
        and facts["failure_evidence_exists"]
        and facts["repair_target_known"]
        and not facts["needs_broad_exploration"]
    )
    if eligible:
        reason = "known_target_repair_after_failed_verification"
    elif not facts["has_diff"]:
        reason = "no_diff"
    elif not facts["failure_evidence_exists"]:
        reason = "no_failure_evidence"
    elif not facts["repair_target_known"]:
        reason = "repair_target_unknown"
    else:
        reason = "broad_exploration_required"
    return EmergencyEligibility(eligible=eligible, reason=reason, **facts)


class WorkBudgetController:
    """Emit each InfCodeX-style convergence zone at most once per run."""

    _MESSAGES = {
        "yellow": (
            "Work budget: begin converging. Reduce broad exploration, use the "
            "evidence already gathered, and organize the shortest complete path."
        ),
        "orange": (
            "Work budget: stop broad exploration. Complete the current "
            "implementation and its required verification before doing optional work."
        ),
        "red": (
            "Work budget: final convergence. Do only unresolved acceptance-critical "
            "work, run the minimum necessary verification, then give the final result."
        ),
    }

    def __init__(
        self,
        max_turns: int,
        emitted: tuple[str, ...] = (),
        *,
        nominal_turns: int = 15,
        closure_reserve: int = 2,
    ) -> None:
        self.max_turns = max(1, int(max_turns))
        self.nominal_turns = min(self.max_turns, max(1, int(nominal_turns)))
        self.closure_reserve = min(
            max(0, int(closure_reserve)),
            self.nominal_turns // 2,
        )
        self.normal_turns = self.nominal_turns - self.closure_reserve
        self._emitted = [zone for zone in emitted if zone in self._MESSAGES]

    @property
    def emitted(self) -> tuple[str, ...]:
        """Return pressure zones already emitted in stable order."""
        return tuple(self._emitted)

    def next_notice(self, completed_turns: int) -> WorkBudgetNotice | None:
        """Return one notice when completed work crosses a new pressure zone."""
        completed = max(0, int(completed_turns))
        zone = self.zone(completed)
        if zone == "green" or zone in self._emitted:
            return None
        self._emitted.append(zone)
        return WorkBudgetNotice(
            zone=zone,
            completed_turns=completed,
            max_turns=self.max_turns,
            message=self._MESSAGES[zone],
        )

    def zone(self, completed_turns: int) -> str:
        """Return the current pressure zone without consuming its notice."""
        completed = max(0, int(completed_turns))
        yellow_at, orange_at, red_at = self._thresholds()
        if completed >= red_at:
            return "red"
        if completed >= orange_at:
            return "orange"
        if completed >= yellow_at:
            return "yellow"
        return "green"

    def _thresholds(self) -> tuple[int, int, int]:
        """Return InfCodeX-compatible 70/85/95 percent soft boundaries."""
        yellow_at = max(1, int(math.ceil(self.nominal_turns * 0.70)))
        orange_at = max(yellow_at + 1, int(math.ceil(self.nominal_turns * 0.85)))
        red_at = max(orange_at + 1, int(math.ceil(self.nominal_turns * 0.95)))
        return yellow_at, orange_at, red_at

    def phase(self, completed_turns: int) -> str:
        """Return advisory work phase; only the configured hard cap terminates."""
        completed = max(0, int(completed_turns))
        if completed >= self.max_turns:
            return "hard_cap"
        if completed < self.normal_turns:
            return "normal"
        closure_index = completed - self.normal_turns
        if closure_index < self.closure_reserve:
            return "closure_repair" if closure_index == 0 else "closure_finalize"
        return "soft_extension"


__all__ = [
    "EmergencyEligibility",
    "WorkBudgetController",
    "WorkBudgetNotice",
    "evaluate_emergency_extension",
]
