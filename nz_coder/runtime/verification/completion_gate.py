"""Deterministic completion decisions based on requirement evidence."""
from __future__ import annotations

from dataclasses import dataclass

from nz_coder.protocol.message_schema import stamp_user_message
from nz_coder.runtime.agent.task_contract import RequirementLedger


COMPLETION_GATE_REANIMATE_BUDGET = 2


@dataclass(frozen=True)
class CompletionDecision:
    """One bounded decision returned at a natural Agent stop boundary."""

    ready: bool
    missing_ids: tuple[str, ...] = ()
    message: str = ""


class CompletionGate:
    """Prevent a final answer while objective contract work remains unresolved."""

    def __init__(self, *, max_missing: int = 6) -> None:
        self.max_missing = max(1, int(max_missing))

    def evaluate(
        self,
        ledger: RequirementLedger,
        *,
        mutation_generation: int,
    ) -> CompletionDecision:
        del mutation_generation
        unresolved = ledger.unresolved()
        if not unresolved:
            return CompletionDecision(ready=True)
        selected = unresolved[: self.max_missing]
        runtime_owned = [
            item for item in selected
            if _runtime_semantic_review_pending(item, ledger.latest_generation)
        ]
        actionable = [item for item in selected if item not in runtime_owned]
        lines = [
            "TaskContract is not complete. Continue with only these unresolved requirements:"
        ]
        for item in actionable:
            artifacts = ", ".join(item.requirement.expected_artifacts)
            details = f"status={item.status}"
            if artifacts:
                details += f", expected artifacts: {artifacts}"
            lines.append(
                f"- {item.requirement.id}: {item.requirement.description} "
                f"({details})"
            )
        if runtime_owned:
            lines.append(
                "Runtime-owned evidence pending. Do not edit code solely to satisfy "
                "these requirements:"
            )
            lines.extend(
                f"- {item.requirement.id}: {item.requirement.description} "
                "(required evidence: semantic_review)"
                for item in runtime_owned
            )
        if len(unresolved) > len(selected):
            lines.append(f"- ... {len(unresolved) - len(selected)} more")
        if actionable:
            lines.append(
                "Use existing repository evidence, make a narrow repair, and run focused "
                "verification. Do not restart broad exploration."
            )
        else:
            lines.append(
                "Do not make speculative edits. Return a truthful summary without extra "
                "tool calls so Runtime can retry its independent semantic review."
            )
        return CompletionDecision(
            ready=False,
            missing_ids=tuple(item.requirement.id for item in selected),
            message="\n".join(lines),
        )


def append_completion_guidance(
    messages: list,
    state,
) -> tuple[CompletionDecision, bool]:
    """Append one generation-scoped repair prompt for an unresolved ledger.

    Both the native terminal boundary and legacy-backed policy adapter use this
    helper so a mixed deterministic/semantic ledger cannot fall between their
    two completion paths.  Repeated natural stops without new workspace
    evidence reuse the existing prompt instead of inflating context.
    """
    ledger_data = getattr(state, "requirement_ledger", None)
    ledger_provider = getattr(state, "requirement_ledger_snapshot", None)
    if not (isinstance(ledger_data, dict) and ledger_data and callable(ledger_provider)):
        return CompletionDecision(ready=True), False
    generation = int(getattr(state, "mutation_generation", 0) or 0)
    decision = CompletionGate().evaluate(
        ledger_provider(),
        mutation_generation=generation,
    )
    if decision.ready:
        return decision, False
    prompt_count = int(getattr(state, "completion_gate_prompts", 0) or 0)
    if prompt_count >= COMPLETION_GATE_REANIMATE_BUDGET:
        return decision, False
    signature = f"{generation}|{'|'.join(decision.missing_ids)}"
    state.completion_gate_signature = signature
    state.completion_gate_prompts = prompt_count + 1
    messages.append(stamp_user_message({
        "role": "user",
        "content": (
            "<requirement-completion-gate>\n"
            + decision.message
            + "\n</requirement-completion-gate>"
        ),
        "_nz_synthetic": True,
        "_nz_completion_gate": True,
    }))
    return decision, True


def _runtime_semantic_review_pending(item, generation: int) -> bool:
    """Return whether only Runtime-owned semantic evidence remains for one item."""
    if set(item.requirement.required_evidence) != {"semantic_review"}:
        return False
    return any(
        evidence.type == "verification_passed"
        and evidence.generation == generation
        for evidence in item.evidence
    )


__all__ = [
    "CompletionDecision",
    "CompletionGate",
    "COMPLETION_GATE_REANIMATE_BUDGET",
    "append_completion_guidance",
]
