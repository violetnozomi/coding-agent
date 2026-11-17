"""Bounded first-turn repository worksets for multi-artifact coding tasks."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from nz_coder.runtime.verification.domain.requirements import TaskContract


def should_build_implementation_bundle(contract: TaskContract) -> bool:
    """Provide a workset when the user contract contains concrete material."""
    return bool(contract.requirements)


def build_implementation_bundle(
    *,
    query: str,
    contract: TaskContract,
    execution_facts: dict,
    workspace: str | Path,
    candidate_files: tuple[str, ...] | list[str] = (),
    token_budget: int = 2400,
    include_requirements: bool = True,
) -> str:
    """Render contract, launch facts, recommended files, and bounded snippets."""
    root = Path(workspace).resolve()
    max_chars = max(1500, min(12000, int(token_budget) * 4))
    lines = ["<implementation-bundle>", "Implementation Workset"]
    if contract.objective:
        lines.append(f"Objective: {contract.objective}")
    if contract.requirements and include_requirements:
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
        " ".join([contract.objective, *(item.description for item in contract.requirements), query])
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
        lines.append("Task-relevant snippets (verify against the full implementation):")
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
    # 先保留显式符号，避免长运行模板把函数名挤出词数预算。
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
    symbols = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", text)
    symbols.extend(token for token in tokens if "_" in token or re.search(r"[a-z][A-Z]", token))
    terms = []
    for token in [*symbols, *tokens]:
        lowered = token.casefold()
        if lowered in {"add", "update", "support", "test", "tests", "with", "from",
                       "the", "and", "for", "this", "that", "file", "files"}:
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
    def relevance(line: str) -> int:
        words = set(re.findall(r"[a-z_][a-z0-9_]*", line.casefold()))
        score = sum(len(terms) - index for index, term in enumerate(terms) if term in words)
        if score and re.search(r"\b(?:def|class|function|func)\s+", line):
            score += len(terms)
        return score

    match_index = max(range(len(lines)), key=lambda index: relevance(lines[index]))
    start = match_index
    # 优先保证命中行及其实现，不让前面的长注释耗尽片段预算。
    while start > max(0, match_index - 2) and sum(len(line) + 1 for line in lines[start - 1:match_index + 1]) < max_chars // 3:
        start -= 1
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
