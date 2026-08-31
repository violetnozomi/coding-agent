"""Budget-aware staged verification selection for one Agent run."""
from __future__ import annotations

from dataclasses import dataclass


_AUTOMATION_SAFE_TARGET_PROVENANCE = frozenset({
    "failure_evidence",
    "model_execution",
    "user_declared",
})


@dataclass(frozen=True)
class VerificationAction:
    """One deterministic scheduler decision at a convergence boundary."""

    kind: str = "none"
    stage: str = ""
    command: str = ""
    reason: str = ""
    mutation_generation: int = 0


class VerificationScheduler:
    """Select cheap/targeted evidence without repeatedly running acceptance."""

    def action(
        self,
        zone: str,
        *,
        verification_status: dict,
        unresolved_requirements: tuple[str, ...] | list[str],
        has_exact_contract: bool,
        exact_attempts: int = 0,
        mutation_generation: int = 0,
        source_mutation_generation: int | None = None,
        scheduled_generations: dict[str, int] | None = None,
    ) -> VerificationAction:
        normalized_zone = str(zone or "").lower()
        generation = max(0, int(mutation_generation))
        source_generation_supplied = source_mutation_generation is not None
        source_generation = max(
            0,
            int(
                generation
                if source_mutation_generation is None
                else source_mutation_generation
            ),
        )
        scheduled = scheduled_generations or {}

        def stage_action(
            stage: str,
            command: str,
            reason: str,
        ) -> VerificationAction:
            attempted = int(scheduled.get(stage, -1))
            if (
                (source_generation_supplied and source_generation <= 0)
                or attempted >= source_generation
                or (
                    max(0, int(exact_attempts)) > 0
                    and generation > source_generation
                )
            ):
                return VerificationAction()
            return VerificationAction(
                kind="stage",
                stage=stage,
                command=command,
                reason=reason,
                mutation_generation=generation,
            )

        if normalized_zone == "completion":
            return (
                VerificationAction(
                    kind="acceptance",
                    stage="acceptance",
                    reason="natural completion requires the exact user contract",
                )
                if has_exact_contract else VerificationAction()
            )

        if normalized_zone == "yellow":
            command = self._pending_command(verification_status, "static")
            if command:
                return stage_action(
                    "static", command,
                    "yellow budget uses the cheapest required evidence",
                )
            return VerificationAction()

        if normalized_zone in {"orange", "red"}:
            command = self._pending_command(verification_status, "targeted")
            if command:
                return stage_action(
                    "targeted", command,
                    f"{normalized_zone} budget verifies affected behavior",
                )
            static = self._pending_command(verification_status, "static")
            if static:
                return stage_action(
                    "static", static,
                    f"{normalized_zone} budget settles missing static evidence",
                )
            if (
                normalized_zone == "red"
                and has_exact_contract
                and not tuple(unresolved_requirements)
            ):
                return VerificationAction(
                    kind="acceptance",
                    stage="acceptance",
                    reason="red convergence has no unresolved hard requirement",
                )
        return VerificationAction()

    @staticmethod
    def _pending_command(status: dict, stage_name: str) -> str:
        pipeline = (
            status.get("verification_pipeline")
            if isinstance(status, dict) else None
        ) or {}
        for stage in pipeline.get("stages") or []:
            if not isinstance(stage, dict) or stage.get("name") != stage_name:
                continue
            required_pending = [
                item for item in stage.get("commands") or []
                if isinstance(item, dict)
                and item.get("required")
                and item.get("status") != "passed"
                and str(item.get("command") or "").strip()
                and (
                    stage_name != "targeted"
                    or item.get("automation_provenance")
                    in _AUTOMATION_SAFE_TARGET_PROVENANCE
                )
            ]
            if required_pending:
                return str(required_pending[0]["command"]).strip()
            if stage_name == "targeted":
                related = [
                    item for item in stage.get("commands") or []
                    if isinstance(item, dict)
                    and not item.get("required")
                    and item.get("status") in {"not_run", "failed", "pending"}
                    and str(item.get("command") or "").strip()
                    and item.get("automation_provenance")
                    in _AUTOMATION_SAFE_TARGET_PROVENANCE
                ]
                if related:
                    return str(related[0]["command"]).strip()
        return ""


__all__ = ["VerificationAction", "VerificationScheduler"]
