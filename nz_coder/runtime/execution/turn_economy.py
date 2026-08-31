"""Deterministic Provider-turn attribution and safe convergence predicates."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from nz_coder.runtime.agent.task_contract import RequirementLedger
from nz_coder.runtime.verification.verification_contract import (
    VerificationContract,
    effective_acceptance_generation,
)
from nz_coder.tools import is_filesystem_mutation_tool


_VERIFICATION_TOOLS = frozenset({
    "bash",
    "diff_status",
    "lsp_diagnostics",
    "verify_changed_files",
})
_INVESTIGATION_TOOLS = frozenset({
    "ast_search",
    "call_hierarchy",
    "find_references",
    "get_definition",
    "glob_search",
    "grep_search",
    "list_directory",
    "read_file",
    "repo_map",
    "smart_search",
    "symbol_context",
})


@dataclass(frozen=True)
class ProviderTurnSnapshot:
    """Run-state facts captured immediately before one main Provider call."""

    turn: int
    reason: str
    mutation_generation: int
    verification_generation: int
    transition: str
    work_phase: str
    budget_zone: str

    def to_dict(self) -> dict:
        """Return a JSON-safe trace representation."""
        return asdict(self)


@dataclass(frozen=True)
class ProviderTurnObservation:
    """Structured response outcome linked to one Provider-turn snapshot."""

    turn: int
    reason: str
    outcome: str
    tool_names: tuple[str, ...]
    finish_reason: str
    mutation_generation_before: int
    mutation_generation_after: int
    mutation_delta: int
    verification_generation_after: int

    def to_dict(self) -> dict:
        """Return a JSON-safe persisted representation."""
        data = asdict(self)
        data["tool_names"] = list(self.tool_names)
        return data


def begin_provider_turn(state, messages: list, turn: int) -> ProviderTurnSnapshot:
    """Classify why the canonical Runner is about to call the main model."""
    reason = _turn_reason(state, messages, turn)
    return ProviderTurnSnapshot(
        turn=max(1, int(turn)),
        reason=reason,
        mutation_generation=max(
            0, int(getattr(state, "mutation_generation", 0) or 0)
        ),
        verification_generation=int(
            getattr(state, "verification_generation", -1) or -1
        ),
        transition=str(getattr(state, "transition", "") or ""),
        work_phase=str(getattr(state, "work_phase", "normal") or "normal"),
        budget_zone=str(
            getattr(state, "budget_pressure_zone", "green") or "green"
        ),
    )


def settle_provider_turn(
    snapshot: ProviderTurnSnapshot,
    state,
    *,
    tool_calls: list | tuple,
    finish_reason: str,
) -> ProviderTurnObservation:
    """Classify one settled structured model response without reading prose."""
    tool_names = tuple(
        name for name in (_tool_name(call) for call in tool_calls) if name
    )
    after = max(0, int(getattr(state, "mutation_generation", 0) or 0))
    outcome = _response_outcome(
        tool_names,
        finish_reason=str(finish_reason or ""),
        mutation_delta=max(0, after - snapshot.mutation_generation),
    )
    return ProviderTurnObservation(
        turn=snapshot.turn,
        reason=snapshot.reason,
        outcome=outcome,
        tool_names=tool_names,
        finish_reason=str(finish_reason or ""),
        mutation_generation_before=snapshot.mutation_generation,
        mutation_generation_after=after,
        mutation_delta=max(0, after - snapshot.mutation_generation),
        verification_generation_after=int(
            getattr(state, "verification_generation", -1) or -1
        ),
    )


def early_tool_completion_ready(state) -> bool:
    """Return whether a tool boundary owns enough current evidence to close.

    This predicate is deliberately stricter than ordinary completion. It is
    used to avoid one otherwise redundant Provider round-trip, so malformed,
    stale, or absent evidence always falls back to the normal loop.
    """
    if not bool(getattr(state, "has_diff", False)):
        return False
    generation = effective_acceptance_generation(state)
    raw_contract = getattr(state, "verification_contract", None)
    if not isinstance(raw_contract, dict) or not raw_contract:
        return False
    try:
        contract = VerificationContract.from_dict(raw_contract)
    except (TypeError, ValueError):
        return False
    if not (
        contract.command
        and contract.targets
        and contract.passed is True
        and contract.attempted_generation == generation
    ):
        return False

    raw_ledger = getattr(state, "requirement_ledger", None)
    if not (
        isinstance(raw_ledger, dict)
        and isinstance(raw_ledger.get("items"), list)
    ):
        return False
    try:
        ledger = RequirementLedger.from_dict(raw_ledger)
    except (TypeError, ValueError):
        return False
    return not ledger.unresolved() or ledger.semantic_review_pending_only()


def _turn_reason(state, messages: list, turn: int) -> str:
    latest = next(
        (
            message for message in reversed(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        {},
    )
    if latest.get("_nz_completion_gate"):
        return "requirement_repair"
    if (
        (
            int(getattr(state, "verification_failures", 0) or 0) > 0
            and str(getattr(state, "last_verification_failure", "") or "").strip()
        )
        or (
            int(getattr(state, "completion_review_rejections", 0) or 0) > 0
            and str(
                getattr(state, "last_completion_review_rejection", "") or ""
            ).strip()
        )
    ):
        return "failure_repair"
    if early_tool_completion_ready(state):
        return "completion"
    phase = str(getattr(state, "work_phase", "normal") or "normal")
    zone = str(getattr(state, "budget_pressure_zone", "green") or "green")
    if phase != "normal" or zone != "green" or latest.get("_nz_work_budget_zone"):
        return "convergence"
    if max(1, int(turn)) == 1:
        return "initial_investigation"
    mutation = max(0, int(getattr(state, "mutation_generation", 0) or 0))
    verification = int(getattr(state, "verification_generation", -1) or -1)
    if bool(getattr(state, "has_diff", False)) and verification < mutation:
        return "verification"
    if mutation > 0 or bool(getattr(state, "has_diff", False)):
        return "implementation"
    return "investigation"


def _response_outcome(
    tool_names: tuple[str, ...],
    *,
    finish_reason: str,
    mutation_delta: int,
) -> str:
    if finish_reason in {"error", "length"}:
        return "provider_error"
    if not tool_names:
        return "final_answer"
    categories: set[str] = set()
    for name in tool_names:
        if is_filesystem_mutation_tool(name):
            categories.add("mutation")
        elif name in _VERIFICATION_TOOLS:
            categories.add("verification")
        elif name in _INVESTIGATION_TOOLS:
            categories.add("investigation")
        else:
            categories.add("other")
    if len(categories) > 1:
        return "mixed_tool_batch"
    category = next(iter(categories))
    if mutation_delta > 0 and category != "mutation":
        return "mixed_tool_batch"
    return {
        "investigation": "investigation_batch",
        "mutation": "mutation_batch",
        "verification": "verification_batch",
        "other": "other_tool_batch",
    }[category]


def _tool_name(call) -> str:
    if isinstance(call, dict):
        function = call.get("function") or {}
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(call.get("name") or "")
    function = getattr(call, "function", None)
    return str(getattr(function, "name", "") or getattr(call, "name", "") or "")


__all__ = [
    "ProviderTurnObservation",
    "ProviderTurnSnapshot",
    "begin_provider_turn",
    "early_tool_completion_ready",
    "settle_provider_turn",
]
