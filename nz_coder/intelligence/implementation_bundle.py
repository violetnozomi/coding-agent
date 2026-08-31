"""Bounded first-turn repository worksets for multi-artifact coding tasks."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from nz_coder.runtime.agent.task_contract import TaskContract


def should_build_implementation_bundle(
    contract: TaskContract,
    *,
    text_complexity: str,
    task_mode: str,
) -> bool:
    """Activate only for non-trivial coding contracts with multiple artifacts."""
    if task_mode not in {"bugfix", "feature", "refactor", "test", "project_creation"}:
        return False
    artifacts = {
        path
        for requirement in contract.requirements
        for path in requirement.expected_artifacts
    }
    return bool(
        len(contract.requirements) >= 3
        or (
            text_complexity in {"moderate", "complex"}
            and len(artifacts) >= 2
        )
    )


def build_implementation_bundle(
    *,
    query: str,
    contract: TaskContract,
    execution_facts: dict,
    workspace: str | Path,
    candidate_files: tuple[str, ...] | list[str] = (),
    token_budget: int = 2400,
) -> str:
    """Render contract, launch facts, recommended files, and bounded snippets."""
    root = Path(workspace).resolve()
    max_chars = max(1500, min(12000, int(token_budget) * 4))
    lines = ["<implementation-bundle>", "Implementation Workset"]
    if contract.objective:
        lines.append(f"Objective: {contract.objective}")
    if contract.requirements:
        lines.append("Requirements:")
        lines.extend(
            f"- {item.id} [{item.kind}] {item.description}"
            + (
                f" | artifacts={','.join(item.expected_artifacts)}"
                if item.expected_artifacts else ""
            )
            for item in contract.requirements
        )
    if contract.constraints:
        lines.append("Constraints: " + "; ".join(contract.constraints[:8]))

    facts = _format_execution_facts(execution_facts)
    if facts:
        lines.extend(["Execution facts:", *facts])

    artifact_paths = [
        path
        for requirement in contract.requirements
        for path in requirement.expected_artifacts
    ]
    recommended = _safe_paths(
        [*artifact_paths, *candidate_files],
        root=root,
        limit=8,
    )
    if recommended:
        lines.append("Recommended files:")
        lines.extend(f"- {path}" for path in recommended)

    terms = _search_terms(
        " ".join([query, *(item.description for item in contract.requirements)])
    )
    snippets: list[str] = []
    for relative in recommended:
        target = root / relative
        if not target.is_file() or target.stat().st_size > 512_000:
            continue
        snippet = _read_relevant_snippet(target, terms=terms, max_chars=1200)
        if snippet:
            snippets.append(f"--- {relative}\n{snippet}")
    if snippets:
        lines.append("High-confidence snippets:")
        lines.extend(snippets)

    body = "\n".join(lines)
    closing = "\n</implementation-bundle>"
    if len(body) + len(closing) > max_chars:
        body = body[: max(0, max_chars - len(closing) - 24)].rstrip()
        body += "\n... [bundle truncated]"
    return body + closing


def _format_execution_facts(facts: dict) -> list[str]:
    lines: list[str] = []
    for key in ("workspace_root", "project_root"):
        value = str(facts.get(key) or "").strip()
        if value:
            lines.append(f"- {key}={value}")
    for package in facts.get("python_packages") or []:
        if not isinstance(package, dict):
            continue
        lines.append(
            "- python_package="
            f"{package.get('module_name', '')}; "
            f"package_path={package.get('package_path', '')}; "
            f"module_cwd={package.get('module_cwd', '')}"
        )
    for key in (
        "source_roots", "test_roots", "test_commands", "typecheck_commands",
        "lint_commands", "build_commands", "entrypoints",
    ):
        values = [str(item) for item in facts.get(key) or [] if str(item).strip()]
        if values:
            lines.append(f"- {key}={','.join(values[:8])}")
    return lines


def _safe_paths(values, *, root: Path, limit: int) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip().replace("\\", "/")
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            continue
        normalized = path.as_posix()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        target = (root / normalized).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _search_terms(text: str) -> tuple[str, ...]:
    terms = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
        lowered = token.casefold()
        if lowered in {"add", "update", "support", "test", "tests", "with", "from"}:
            continue
        if lowered not in terms:
            terms.append(lowered)
    return tuple(terms[:20])


def _read_relevant_snippet(path: Path, *, terms: tuple[str, ...], max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.strip():
        return ""
    if len(text) <= max_chars:
        return text.rstrip()
    lines = text.splitlines()
    match_index = next(
        (
            index for index, line in enumerate(lines)
            if any(term in line.casefold() for term in terms)
        ),
        0,
    )
    start = max(0, match_index - 8)
    selected: list[str] = []
    size = 0
    for line in lines[start:]:
        addition = len(line) + 1
        if selected and size + addition > max_chars:
            break
        selected.append(line)
        size += addition
    prefix = f"[lines {start + 1}-{start + len(selected)}]\n"
    return prefix + "\n".join(selected).rstrip()


__all__ = [
    "build_implementation_bundle",
    "should_build_implementation_bundle",
]
