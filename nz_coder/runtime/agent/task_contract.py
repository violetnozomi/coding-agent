"""Planner-owned task contracts and deterministic requirement evidence."""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from nz_coder.runtime.agent.task_policy import (
    is_documentation_file,
    task_forbids_test_changes,
    task_wants_tests,
)


REQUIREMENT_KINDS = frozenset({
    "behavior",
    "artifact",
    "test",
    "docs",
    "compatibility",
    "verification",
})
REQUIREMENT_STATUSES = frozenset({
    "pending",
    "in_progress",
    "candidate",
    "satisfied",
    "blocked",
})
SATISFACTION_MODES = frozenset({"deterministic", "semantic", "mixed"})
REQUIRED_EVIDENCE_TYPES = frozenset({"semantic_review"})
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _persisted_int(value: object, *, default: int = 0) -> int:
    """Normalize one integer field from an untrusted RuntimeState snapshot."""
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass(frozen=True)
class EvidenceRef:
    """One durable, mutation-scoped fact supporting a requirement."""

    type: str
    path: str | None = None
    command: str | None = None
    generation: int = 0
    fingerprint: str | None = None


@dataclass(frozen=True)
class Requirement:
    """One normalized requirement emitted by the existing planning call."""

    id: str
    description: str
    kind: str
    expected_artifacts: tuple[str, ...] = ()
    satisfaction_mode: str = "mixed"
    depends_on: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskContract:
    """Validated description of what one Agent run must deliver."""

    objective: str = ""
    requirements: tuple[Requirement, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_commands: tuple[str, ...] = ()
    contract_version: int = 2

    def to_dict(self) -> dict:
        """Return a JSON-safe representation for RuntimeState persistence."""
        return {
            "objective": self.objective,
            "requirements": [
                {
                    **asdict(item),
                    "expected_artifacts": list(item.expected_artifacts),
                    "depends_on": list(item.depends_on),
                    "required_evidence": list(item.required_evidence),
                }
                for item in self.requirements
            ],
            "constraints": list(self.constraints),
            "acceptance_commands": list(self.acceptance_commands),
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, value: dict, workspace: str | Path | None = None) -> "TaskContract":
        """Validate and normalize a planner-provided contract object."""
        if not isinstance(value, dict):
            raise ValueError("task contract must be an object")
        objective = str(value.get("objective") or "").strip()[:2000]
        raw_requirements = value.get("requirements") or []
        if not isinstance(raw_requirements, list):
            raise ValueError("task contract requirements must be a list")
        if len(raw_requirements) > 24:
            raise ValueError("task contract has too many requirements")

        requirements: list[Requirement] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_requirements, start=1):
            if not isinstance(raw, dict):
                raise ValueError("each task requirement must be an object")
            requirement_id = str(raw.get("id") or f"R{index}").strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,31}", requirement_id):
                raise ValueError(f"invalid requirement id: {requirement_id!r}")
            if requirement_id in seen_ids:
                raise ValueError(f"duplicate requirement id: {requirement_id}")
            seen_ids.add(requirement_id)
            description = str(raw.get("description") or "").strip()
            if not description:
                raise ValueError(f"requirement {requirement_id} has no description")
            kind = str(raw.get("kind") or "behavior").strip().lower()
            if kind not in REQUIREMENT_KINDS:
                raise ValueError(f"unknown requirement kind: {kind}")
            satisfaction_mode = str(
                raw.get("satisfaction_mode") or _default_mode(kind)
            ).strip().lower()
            if satisfaction_mode not in SATISFACTION_MODES:
                raise ValueError(
                    f"unknown satisfaction mode for {requirement_id}: {satisfaction_mode}"
                )
            artifacts = _normalize_paths(
                raw.get("expected_artifacts") or [],
                workspace=workspace,
            )
            depends_on = tuple(dict.fromkeys(
                str(item).strip()
                for item in (raw.get("depends_on") or [])
                if str(item).strip()
            ))
            default_evidence = ["semantic_review"] if kind == "compatibility" else []
            raw_evidence = raw.get("required_evidence", default_evidence)
            if not isinstance(raw_evidence, list):
                raise ValueError(
                    f"requirement {requirement_id} required_evidence must be a list"
                )
            required_evidence = tuple(dict.fromkeys(
                str(item).strip().lower()
                for item in raw_evidence
                if str(item).strip()
            ))
            if kind == "compatibility" and "semantic_review" not in required_evidence:
                required_evidence = (*required_evidence, "semantic_review")
            unknown_evidence = set(required_evidence) - REQUIRED_EVIDENCE_TYPES
            if unknown_evidence:
                raise ValueError(
                    f"requirement {requirement_id} has unknown required evidence: "
                    + ", ".join(sorted(unknown_evidence))
                )
            requirements.append(Requirement(
                id=requirement_id,
                description=description[:2000],
                kind=kind,
                expected_artifacts=artifacts,
                satisfaction_mode=satisfaction_mode,
                depends_on=depends_on,
                required_evidence=required_evidence,
            ))
        known_ids = {item.id for item in requirements}
        for item in requirements:
            unknown = set(item.depends_on) - known_ids
            if unknown:
                raise ValueError(
                    f"requirement {item.id} depends on unknown ids: "
                    + ", ".join(sorted(unknown))
                )
        return cls(
            objective=objective,
            requirements=tuple(requirements),
            constraints=_normalize_text_items(value.get("constraints") or [], limit=20),
            acceptance_commands=_normalize_text_items(
                value.get("acceptance_commands") or [], limit=10,
            ),
            contract_version=max(
                1,
                _persisted_int(value.get("contract_version"), default=1),
            ),
        )


@dataclass
class RequirementProgress:
    """Mutable state for one otherwise immutable Requirement."""

    requirement: Requirement
    status: str = "pending"
    evidence: list[EvidenceRef] = field(default_factory=list)
    mutation_generation: int = 0

    def to_dict(self) -> dict:
        return {
            "requirement": TaskContract(requirements=(self.requirement,)).to_dict()[
                "requirements"
            ][0],
            "status": self.status,
            "evidence": [asdict(item) for item in self.evidence],
            "mutation_generation": self.mutation_generation,
        }


@dataclass
class RequirementLedger:
    """Provider-neutral progress state derived from objective runtime evidence."""

    items: dict[str, RequirementProgress] = field(default_factory=dict)
    latest_generation: int = 0
    latest_verification_generation: int = -1

    @classmethod
    def from_contract(cls, contract: TaskContract) -> "RequirementLedger":
        return cls(items={
            item.id: RequirementProgress(requirement=item)
            for item in contract.requirements
        })

    @classmethod
    def from_dict(cls, value: dict) -> "RequirementLedger":
        if not isinstance(value, dict):
            return cls()
        result = cls(
            latest_generation=max(
                0,
                _persisted_int(value.get("latest_generation")),
            ),
            latest_verification_generation=_persisted_int(
                value.get("latest_verification_generation"),
                default=-1,
            ),
        )
        for raw in value.get("items") or []:
            if not isinstance(raw, dict):
                continue
            requirement_raw = raw.get("requirement") or {}
            try:
                contract = TaskContract.from_dict({
                    "objective": "",
                    "requirements": [requirement_raw],
                })
            except (TypeError, ValueError):
                continue
            requirement = contract.requirements[0]
            status = str(raw.get("status") or "pending")
            if status not in REQUIREMENT_STATUSES:
                status = "pending"
            evidence: list[EvidenceRef] = []
            for evidence_raw in raw.get("evidence") or []:
                if not isinstance(evidence_raw, dict):
                    continue
                evidence.append(EvidenceRef(
                    type=str(evidence_raw.get("type") or "unknown"),
                    path=(
                        str(evidence_raw["path"])
                        if evidence_raw.get("path") is not None else None
                    ),
                    command=(
                        str(evidence_raw["command"])
                        if evidence_raw.get("command") is not None else None
                    ),
                    generation=max(
                        0,
                        _persisted_int(evidence_raw.get("generation")),
                    ),
                    fingerprint=(
                        str(evidence_raw["fingerprint"])
                        if evidence_raw.get("fingerprint") is not None else None
                    ),
                ))
            result.items[requirement.id] = RequirementProgress(
                requirement=requirement,
                status=status,
                evidence=evidence,
                mutation_generation=max(
                    0,
                    _persisted_int(raw.get("mutation_generation")),
                ),
            )
        return result

    def to_dict(self) -> dict:
        return {
            "items": [item.to_dict() for item in self.items.values()],
            "latest_generation": self.latest_generation,
            "latest_verification_generation": self.latest_verification_generation,
        }

    def status(self, requirement_id: str) -> str:
        item = self.items.get(requirement_id)
        return item.status if item is not None else "pending"

    def observe_mutation(self, generation: int, paths: list[str] | tuple[str, ...]) -> None:
        """Record successful writes and invalidate stale semantic verification."""
        normalized_paths = tuple(dict.fromkeys(
            path for path in (_normalize_path(item) for item in paths) if path
        ))
        generation = max(0, int(generation))
        is_new_generation = generation > self.latest_generation
        docs_only = bool(normalized_paths) and all(
            is_documentation_file(path) for path in normalized_paths
        )
        if is_new_generation and not docs_only:
            for progress in self.items.values():
                if (
                    progress.status == "satisfied"
                    and progress.requirement.kind != "docs"
                ):
                    progress.status = "candidate" if progress.evidence else "pending"
            self.latest_generation = generation
        for progress in self.items.values():
            matched = [
                path for path in normalized_paths
                if path in progress.requirement.expected_artifacts
            ]
            if not matched:
                continue
            progress.mutation_generation = generation
            for path in matched:
                self._append_evidence(progress, EvidenceRef(
                    type=(
                        "test_added"
                        if progress.requirement.kind == "test"
                        else "file_changed"
                    ),
                    path=path,
                    generation=generation,
                ))
            if progress.status == "satisfied" and not is_new_generation:
                continue
            if (
                progress.requirement.kind in {"docs", "artifact"}
                and progress.requirement.satisfaction_mode == "deterministic"
            ):
                progress.status = "satisfied"
            else:
                progress.status = "candidate"

    def observe_verification(
        self,
        generation: int,
        *,
        command: str,
        passed: bool,
        acceptance: bool,
    ) -> None:
        """Promote artifact-backed candidates using current-generation evidence."""
        generation = max(0, int(generation))
        if not passed:
            return
        self.latest_verification_generation = max(
            self.latest_verification_generation,
            generation,
        )
        evidence_type = "verification_passed" if acceptance else "command_passed"
        for progress in self.items.values():
            if (
                acceptance
                and progress.requirement.kind in {"docs", "artifact", "test"}
                and not self._has_artifact_evidence(progress)
            ):
                continue
            if progress.status not in {"candidate", "in_progress"} and not (
                acceptance and not progress.requirement.expected_artifacts
            ):
                continue
            if progress.requirement.expected_artifacts and not self._has_artifact_evidence(
                progress
            ):
                continue
            if not acceptance and not self._targeted_verification_relates(
                progress,
                command,
            ):
                continue
            self._append_evidence(progress, EvidenceRef(
                type=evidence_type,
                command=str(command),
                generation=generation,
            ))
            progress.status = (
                "satisfied"
                if self._required_evidence_satisfied(progress, generation)
                else "candidate"
            )
            progress.mutation_generation = generation

    def observe_semantic_review(
        self,
        generation: int,
        *,
        accepted: bool,
        fingerprint: str = "",
    ) -> None:
        """Record a real independent semantic verdict for this mutation."""
        if not accepted:
            return
        generation = max(0, int(generation))
        for progress in self.items.values():
            if "semantic_review" not in progress.requirement.required_evidence:
                continue
            if not self._has_current_evidence(
                progress,
                "verification_passed",
                generation,
            ):
                continue
            self._append_evidence(progress, EvidenceRef(
                type="semantic_review_passed",
                generation=generation,
                fingerprint=str(fingerprint or "") or None,
            ))
            if self._required_evidence_satisfied(progress, generation):
                progress.status = "satisfied"
                progress.mutation_generation = generation

    def semantic_review_pending_only(self) -> bool:
        """Return whether semantic review is the sole missing completion fact."""
        unresolved = self.unresolved()
        if not unresolved:
            return False
        generation = self.latest_generation
        for progress in unresolved:
            required = set(progress.requirement.required_evidence)
            if required != {"semantic_review"}:
                return False
            if not self._has_current_evidence(
                progress,
                "verification_passed",
                generation,
            ):
                return False
        return True

    def unresolved(self) -> tuple[RequirementProgress, ...]:
        """Return hard requirements not yet objectively satisfied."""
        return tuple(
            item for item in self.items.values()
            if item.status != "satisfied"
        )

    @staticmethod
    def _append_evidence(progress: RequirementProgress, evidence: EvidenceRef) -> None:
        if evidence not in progress.evidence:
            progress.evidence.append(evidence)
        if len(progress.evidence) > 20:
            progress.evidence[:] = progress.evidence[-20:]

    @staticmethod
    def _has_artifact_evidence(progress: RequirementProgress) -> bool:
        return any(
            item.type in {"file_changed", "test_added", "symbol_present"}
            for item in progress.evidence
        )

    @classmethod
    def _required_evidence_satisfied(
        cls,
        progress: RequirementProgress,
        generation: int,
    ) -> bool:
        for evidence_type in progress.requirement.required_evidence:
            recorded_type = {
                "semantic_review": "semantic_review_passed",
            }[evidence_type]
            if not cls._has_current_evidence(
                progress,
                recorded_type,
                generation,
            ):
                return False
        return True

    @staticmethod
    def _has_current_evidence(
        progress: RequirementProgress,
        evidence_type: str,
        generation: int,
    ) -> bool:
        return any(
            item.type == evidence_type and item.generation == generation
            for item in progress.evidence
        )

    @staticmethod
    def _targeted_verification_relates(
        progress: RequirementProgress,
        command: str,
    ) -> bool:
        from nz_coder.intelligence.verification_planner import classify_verification_command

        if classify_verification_command(command) != "targeted":
            return False
        lowered = str(command or "").casefold().replace("\\", "/")
        for artifact in progress.requirement.expected_artifacts:
            stem = PurePosixPath(artifact).stem.casefold()
            for prefix in ("test_", "test-"):
                if stem.startswith(prefix):
                    stem = stem[len(prefix):]
            if len(stem) >= 2 and stem in lowered:
                return True
        return False


@dataclass(frozen=True)
class PlanningEnvelope:
    """One planner response projected to the existing scratchpad and contract."""

    plan_text: str
    contract: TaskContract


def derive_task_contract(
    task_text: str,
    *,
    acceptance_command: str = "",
    workspace: str | Path | None = None,
    artifact_allowlist: tuple[str, ...] | None = None,
    explicit_path_allowlist: tuple[str, ...] | None = None,
) -> TaskContract:
    """Build a conservative zero-call contract from explicit user intent.

    A deterministic contract is only enforceable when the request contains a
    validated exact acceptance command.  Without one, returning an empty
    contract avoids turning model-inferred prose into an unsatisfiable hard
    completion gate.
    """
    text = " ".join(str(task_text or "").split())
    command = " ".join(str(acceptance_command or "").split())
    if not text or not command:
        return TaskContract()

    lowered = text.casefold()
    from nz_coder.intelligence.bootstrap_artifacts import (
        resolve_bootstrap_artifacts,
    )

    resolution = resolve_bootstrap_artifacts(
        text,
        workspace=workspace or Path.cwd(),
        explicit_path_allowlist=explicit_path_allowlist,
    )
    allowed_artifacts = (
        None
        if artifact_allowlist is None
        else {
            normalized
            for normalized in (
                _normalize_path(item) for item in artifact_allowlist
            )
            if normalized
        }
    )
    requirements: list[dict] = []

    def add(
        kind: str,
        description: str,
        mode: str = "semantic",
        artifacts: tuple[str, ...] = (),
    ) -> None:
        if allowed_artifacts is not None:
            artifacts = tuple(
                artifact
                for artifact in artifacts
                if _normalize_path(artifact) in allowed_artifacts
            )
        requirements.append({
            "id": f"R{len(requirements) + 1}",
            "description": description,
            "kind": kind,
            "expected_artifacts": list(artifacts),
            "satisfaction_mode": mode,
            "depends_on": [],
            "required_evidence": (
                ["semantic_review"] if kind == "compatibility" else []
            ),
        })

    add(
        "behavior",
        f"Implement the requested behavior: {text[:900]}",
        artifacts=resolution.required_for("behavior"),
    )
    if task_wants_tests(text):
        test_artifacts = resolution.required_for("test")
        if test_artifacts:
            for artifact in test_artifacts:
                add(
                    "test",
                    f"Add or update the explicitly requested test coverage in {artifact}.",
                    artifacts=(artifact,),
                )
        else:
            add("test", "Add or update the explicitly requested test coverage.")
    if _contains_any(lowered, (
        "readme", "documentation", "docs", "document ", "文档", "说明",
    )):
        add(
            "docs",
            "Update the explicitly requested documentation.",
            "mixed",
            resolution.required_for("docs"),
        )
    if _contains_any(lowered, (
        "compatib", "preserve", "backward", "public api",
        "兼容", "保持现有", "公开 api", "不破坏",
    )):
        add("compatibility", "Preserve the requested compatibility guarantees.")
    add("verification", f"Pass the exact acceptance command: {command}")
    constraints = (
        ["Do not modify test files."]
        if task_forbids_test_changes(text) else []
    )
    return TaskContract.from_dict({
        "objective": text[:2000],
        "requirements": requirements,
        "constraints": constraints,
        "acceptance_commands": [command],
        "contract_version": 2,
    }, workspace=workspace)


def derive_round_artifact_contract(
    task_text: str,
    *,
    artifact_paths: tuple[str, ...],
    workspace: str | Path | None = None,
) -> TaskContract:
    """Build only deterministic artifact obligations from explicit write targets."""
    text = " ".join(str(task_text or "").split())
    requirements: list[dict] = []
    for raw_path in artifact_paths[:20]:
        path = _normalize_path(raw_path)
        if not path:
            continue
        is_docs = is_documentation_file(path)
        kind = "docs" if is_docs else "artifact"
        requirements.append({
            "id": f"R{len(requirements) + 1}",
            "description": f"Update the explicitly requested artifact: {path}",
            "kind": kind,
            "expected_artifacts": [path],
            "satisfaction_mode": "deterministic",
            "depends_on": [],
            "required_evidence": [],
        })
    if not requirements:
        return TaskContract()
    return TaskContract.from_dict({
        "objective": text[:2000],
        "requirements": requirements,
        "constraints": [],
        "acceptance_commands": [],
        "contract_version": 2,
    }, workspace=workspace)


def merge_round_task_contract(
    current: TaskContract,
    ledger: RequirementLedger,
    round_contract: TaskContract,
    *,
    allow_test_changes: bool | None = None,
) -> tuple[TaskContract, RequirementLedger]:
    """Merge explicit follow-up requirements while preserving prior evidence."""
    if not round_contract.requirements:
        return current, ledger

    existing_non_verification = [
        item for item in current.requirements if item.kind != "verification"
    ]
    existing_signatures = {
        _requirement_signature(item) for item in existing_non_verification
    }
    used_ids = {item.id for item in current.requirements}
    merged_requirements = list(existing_non_verification)
    for requirement in round_contract.requirements:
        if requirement.kind == "verification":
            continue
        signature = _requirement_signature(requirement)
        if signature in existing_signatures:
            continue
        merged_requirements.append(
            _requirement_with_id(requirement, _next_requirement_id(used_ids))
        )
        existing_signatures.add(signature)

    previous_verification = next(
        (
            item for item in current.requirements
            if item.kind == "verification"
        ),
        None,
    )
    round_verification = next(
        (
            item for item in round_contract.requirements
            if item.kind == "verification"
        ),
        None,
    )
    same_command = (
        current.acceptance_commands == round_contract.acceptance_commands
    )
    if round_verification is None and previous_verification is not None:
        merged_requirements.append(previous_verification)
    elif same_command and previous_verification is not None:
        merged_requirements.append(previous_verification)
    elif round_verification is not None:
        verification_id = (
            previous_verification.id
            if previous_verification is not None
            else _next_requirement_id(used_ids)
        )
        merged_requirements.append(
            _requirement_with_id(round_verification, verification_id)
        )

    constraints = list(current.constraints)
    immutable_tests = "Do not modify test files."
    if allow_test_changes is True:
        constraints = [item for item in constraints if item != immutable_tests]
    elif allow_test_changes is False and immutable_tests not in constraints:
        constraints.append(immutable_tests)
    for constraint in round_contract.constraints:
        if constraint not in constraints:
            constraints.append(constraint)

    merged_contract = TaskContract(
        objective=current.objective or round_contract.objective,
        requirements=tuple(merged_requirements),
        constraints=tuple(constraints),
        acceptance_commands=(
            round_contract.acceptance_commands or current.acceptance_commands
        ),
        contract_version=max(
            current.contract_version,
            round_contract.contract_version,
        ),
    )
    merged_items: dict[str, RequirementProgress] = {}
    for requirement in merged_contract.requirements:
        prior = ledger.items.get(requirement.id)
        if prior is not None and prior.requirement == requirement:
            merged_items[requirement.id] = prior
        else:
            merged_items[requirement.id] = RequirementProgress(
                requirement=requirement
            )
    return merged_contract, RequirementLedger(
        items=merged_items,
        latest_generation=ledger.latest_generation,
        latest_verification_generation=ledger.latest_verification_generation,
    )


def fallback_plan_text(contract: TaskContract) -> str:
    """Render a bounded deterministic plan when planner output is unusable."""
    if not contract.requirements:
        return ""
    labels = {
        "behavior": "Implement requested behavior",
        "artifact": "Create requested artifacts",
        "test": "Add requested tests",
        "docs": "Update requested documentation",
        "compatibility": "Preserve compatibility",
        "verification": "Run exact acceptance",
    }
    lines = ["## Plan"]
    for index, requirement in enumerate(contract.requirements[:5], start=1):
        target = ", ".join(requirement.expected_artifacts) or "repository evidence"
        lines.append(
            f"{index}. {labels.get(requirement.kind, 'Complete requirement')}"
            f" - {target} - {requirement.description[:240]}"
        )
    return "\n".join(lines)


def parse_planner_output(raw_text: str, workspace: str | Path) -> PlanningEnvelope:
    """Parse JSON plan+contract output, preserving legacy Markdown fallback."""
    raw = str(raw_text or "").strip()
    payload = _extract_json(raw)
    if payload is None:
        return PlanningEnvelope(plan_text=raw, contract=TaskContract())
    contract = TaskContract.from_dict(payload, workspace=workspace)
    plan_text = _format_plan(payload.get("plan") or [])
    if not plan_text:
        plan_text = "## Plan\n1. Implement and verify the task contract."
    return PlanningEnvelope(plan_text=plan_text, contract=contract)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _requirement_signature(requirement: Requirement) -> tuple:
    return (
        requirement.kind,
        " ".join(requirement.description.casefold().split()),
        requirement.expected_artifacts,
        requirement.satisfaction_mode,
        requirement.required_evidence,
    )


def _requirement_with_id(requirement: Requirement, requirement_id: str) -> Requirement:
    return Requirement(
        id=requirement_id,
        description=requirement.description,
        kind=requirement.kind,
        expected_artifacts=requirement.expected_artifacts,
        satisfaction_mode=requirement.satisfaction_mode,
        depends_on=requirement.depends_on,
        required_evidence=requirement.required_evidence,
    )


def _next_requirement_id(used_ids: set[str]) -> str:
    index = 1
    while f"R{index}" in used_ids:
        index += 1
    requirement_id = f"R{index}"
    used_ids.add(requirement_id)
    return requirement_id


def _extract_json(raw: str) -> dict | None:
    candidate = raw
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        candidate = fence.group(1)
    elif not raw.lstrip().startswith("{"):
        return None
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"planner output JSON is invalid: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("planner output must be an object")
    return value


def _format_plan(raw_steps) -> str:
    if not isinstance(raw_steps, list):
        raise ValueError("planner plan must be a list")
    lines = ["## Plan"]
    for index, raw in enumerate(raw_steps[:5], start=1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        target = str(raw.get("target") or "need to search").strip()
        verification = str(raw.get("verification") or "focused verification").strip()
        lines.append(f"{index}. {title} - {target} - {verification}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _normalize_paths(values, workspace: str | Path | None) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("expected_artifacts must be a list")
    root = Path(workspace).resolve() if workspace is not None else None
    result: list[str] = []
    for raw in values[:20]:
        normalized = _normalize_path(str(raw))
        if not normalized:
            raise ValueError(f"invalid expected artifact path: {raw!r}")
        if root is not None:
            target = (root / normalized).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"expected artifact escapes workspace: {raw}") from exc
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _normalize_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        return ""
    normalized = _strip_relative_prefix(path.as_posix())
    return "" if normalized in {"", "."} else normalized


def _strip_relative_prefix(value: str) -> str:
    normalized = str(value)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _normalize_text_items(values, *, limit: int) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("task contract text collections must be lists")
    return tuple(dict.fromkeys(
        str(item).strip()[:2000]
        for item in values[:limit]
        if str(item).strip()
    ))


def _default_mode(kind: str) -> str:
    return "deterministic" if kind in {"docs", "artifact"} else "mixed"


__all__ = [
    "EvidenceRef",
    "PlanningEnvelope",
    "Requirement",
    "RequirementLedger",
    "RequirementProgress",
    "TaskContract",
    "derive_round_artifact_contract",
    "merge_round_task_contract",
    "parse_planner_output",
]
