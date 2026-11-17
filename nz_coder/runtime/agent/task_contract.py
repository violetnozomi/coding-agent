"""用户明确要求形成的合同定义、跟进合并与证据投影。"""
from __future__ import annotations

import math
import re
from pathlib import Path, PurePosixPath

from nz_coder.runtime.verification.domain.requirements import EvidenceRef, Requirement, TaskContract, apply_write_constraints

from nz_coder.runtime.agent.task_policy import (
    explicit_read_only_paths,
    is_documentation_file,
    is_test_file,
    task_forbids_test_changes,
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






def parse_task_contract(value: dict, workspace: str | Path | None = None) -> "TaskContract":
    """校验并归一化宿主提供的正式用户合同定义。"""
    if not isinstance(value, dict):
        raise ValueError("task contract must be an object")
    objective = str(value.get("objective") or "").strip()
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
        operations = []
        for pair in raw.get("artifact_operations") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("artifact_operations must contain path/operation pairs")
            path, operation = pair
            if path not in artifacts or operation not in {"change", "create", "delete"}:
                raise ValueError("invalid artifact operation or unbound path")
            if any(prior[0] == path for prior in operations):
                raise ValueError("duplicate artifact operation path")
            operations.append((path, operation))
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
            artifact_operations=tuple(operations),
        ))
    known_ids = {item.id for item in requirements}
    for item in requirements:
        unknown = set(item.depends_on) - known_ids
        if unknown:
            raise ValueError(
                f"requirement {item.id} depends on unknown ids: "
                + ", ".join(sorted(unknown))
            )
    return TaskContract(
        objective=objective,
        requirements=tuple(requirements),
        constraints=_normalize_text_items(value.get("constraints") or [], limit=20),
        acceptance_commands=_acceptance_commands(value.get("acceptance_commands") or []),
        contract_version=max(
            1,
            _persisted_int(value.get("contract_version"), default=1),
        ),
    )



def derive_task_contract(
    task_text: str,
    *,
    acceptance_commands: tuple[str, ...] = (),
    workspace: str | Path | None = None,
    artifact_allowlist: tuple[str, ...] | None = None,
    explicit_path_allowlist: tuple[str, ...] | None = None,
) -> TaskContract:
    """只把用户明确命令和具体文件操作转为硬条件，泛化质量由评审检查。"""
    text = str(task_text or "").strip()
    commands = _acceptance_commands(list(acceptance_commands))
    from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts
    from nz_coder.runtime.agent.task_policy import extract_explicit_mutation_operations

    resolution = resolve_bootstrap_artifacts(text, workspace=workspace or Path.cwd(),
        explicit_path_allowlist=explicit_path_allowlist)
    operations = extract_explicit_mutation_operations(text)
    paths = tuple(path for path in resolution.required_paths if path in operations
                  and (artifact_allowlist is None or path in artifact_allowlist))
    artifacts = derive_round_artifact_contract(text, artifact_paths=paths, workspace=workspace)
    requirements = list(artifacts.requirements)
    if commands:
        requirements.append(Requirement(
            f"R{len(requirements) + 1}", "Pass every exact acceptance command:\n" + "\n".join(commands),
            "verification", satisfaction_mode="deterministic"))
    return TaskContract(objective=text, requirements=tuple(requirements),
        constraints=tuple(_instruction_constraints(text)), acceptance_commands=commands)


def derive_round_artifact_contract(
    task_text: str,
    *,
    artifact_paths: tuple[str, ...],
    workspace: str | Path | None = None,
) -> TaskContract:
    """Build only deterministic artifact obligations from explicit write targets."""
    text = str(task_text or "").strip()
    from nz_coder.runtime.agent.task_policy import extract_explicit_mutation_operations

    operations = extract_explicit_mutation_operations(text)
    requirements: list[dict] = []
    for raw_path in artifact_paths[:20]:
        path = _normalize_path(raw_path)
        if not path or path in explicit_read_only_paths(text) or (task_forbids_test_changes(text) and is_test_file(path)):
            continue
        is_docs = is_documentation_file(path)
        kind = "docs" if is_docs else "artifact"
        requirements.append({
            "id": f"R{len(requirements) + 1}",
            "description": f"Update the explicitly requested artifact: {path}",
            "kind": kind,
            "expected_artifacts": [path],
            "artifact_operations": [[path, operations.get(path, "change")]],
            "satisfaction_mode": "deterministic",
            "depends_on": [],
            "required_evidence": [],
        })
    return parse_task_contract({
        "objective": text,
        "requirements": requirements,
        "constraints": _instruction_constraints(text),
        "acceptance_commands": [],
        "contract_version": 2,
    }, workspace=workspace)


def merge_round_task_contract(
    current: TaskContract,
    round_contract: TaskContract,
    *,
    allow_test_changes: bool | None = None,
    allow_write_paths: tuple[str, ...] = (),
) -> TaskContract:
    """Merge explicit follow-up requirements while preserving prior evidence."""
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
    new_readonly = {p for clause in round_contract.constraints for p in explicit_read_only_paths(clause)}
    allowed = set(allow_write_paths) - new_readonly
    constraints = [clause for clause in constraints
                   if not (set(explicit_read_only_paths(clause)) & allowed)]
    immutable_tests = "Do not modify test files."
    if allow_test_changes is True:
        constraints = [item for item in constraints if item != immutable_tests]
    elif allow_test_changes is False and immutable_tests not in constraints:
        constraints.append(immutable_tests)
    for constraint in round_contract.constraints:
        if constraint not in constraints:
            constraints.append(constraint)

    objective = current.objective or round_contract.objective
    followup = "\n\nUser follow-up:\n" + round_contract.objective
    if current.objective and round_contract.objective and round_contract.objective != current.objective and not current.objective.endswith(followup):
        objective += followup
    merged_contract = TaskContract(
        objective=objective,
        requirements=tuple(merged_requirements),
        constraints=tuple(constraints),
        acceptance_commands=tuple(dict.fromkeys((*current.acceptance_commands, *round_contract.acceptance_commands))),
        contract_version=max(
            current.contract_version,
            round_contract.contract_version,
        ),
    )
    return apply_write_constraints(merged_contract)


def _instruction_constraints(text: str) -> list[str]:
    constraints = ["Do not modify test files."] if task_forbids_test_changes(text) else []
    return constraints + [f"Do not modify {path}." for path in explicit_read_only_paths(text)]


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


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _requirement_signature(requirement: Requirement) -> tuple:
    return (
        requirement.kind,
        " ".join(requirement.description.casefold().split()),
        requirement.expected_artifacts,
        requirement.satisfaction_mode,
        requirement.required_evidence,
        requirement.artifact_operations,
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
        artifact_operations=requirement.artifact_operations,
    )


def _next_requirement_id(used_ids: set[str]) -> str:
    index = 1
    while f"R{index}" in used_ids:
        index += 1
    requirement_id = f"R{index}"
    used_ids.add(requirement_id)
    return requirement_id


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


def _acceptance_commands(values) -> tuple[str, ...]:
    # 命令是执行身份；截断或折叠引号内空白会把用户的验收换成另一条命令。
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError("acceptance commands must be a list of strings")
    return tuple(dict.fromkeys(item.strip() for item in values if item.strip()))


def _normalize_text_items(values, *, limit: int) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("task contract text collections must be lists")
    return tuple(dict.fromkeys(
        str(item).strip()[:2000]
        for item in values[:limit]
        if str(item).strip()
    ))


def _default_mode(kind: str) -> str:
    return "deterministic" if kind in {"docs", "artifact", "verification"} else "mixed"


__all__ = [
    "EvidenceRef",
    "Requirement",
    "TaskContract",
    "derive_round_artifact_contract",
    "merge_round_task_contract",
]
