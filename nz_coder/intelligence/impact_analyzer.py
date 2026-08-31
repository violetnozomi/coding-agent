"""Lite patch impact analysis for repository-level coding tasks.

The analyzer uses file paths and diff text heuristics rather than a full call
graph. It is intended to support final review and validation planning.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.intelligence.project_profile import build_project_profile
from nz_coder.runtime.agent.task_policy import is_source_file, is_test_file, language_for_path
from nz_coder.tools import register
from nz_coder.intelligence.verification_planner import plan_verification_commands

_SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(config|settings|migration|migrations|schema|auth|security|database|db|models)(/|\.|$)",
    re.IGNORECASE,
)

_EXCLUDED_PARTS = {
    ".git", ".nz-coder", ".nz-coder-runs", "node_modules",
    "build", "dist", "__pycache__",
}

_PUBLIC_SYMBOL_PATTERNS = (
    ("class", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
    ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
    ("export", re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b")),
    ("rust", re.compile(r"^\s*pub(?:\([^)]*\))?\s+(?:async\s+)?(?:fn|struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
)

_PERSISTENT_DELETE_CALL_RE = re.compile(r"\.\s*delete\s*\(")
_PERSISTENT_QUERY_EVIDENCE_RE = re.compile(
    r"\.(?:objects|query)\b|\bqueryset[A-Za-z0-9_]*\b",
    re.IGNORECASE,
)
_DELETE_RECEIVER_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*delete\s*\(",
    re.IGNORECASE,
)


def _include_changed_file(path: str) -> bool:
    parts = Path(path).parts
    return bool(path) and not any(part in _EXCLUDED_PARTS for part in parts)


def _add_unique_path(items: list[str], path: str) -> None:
    path = path.strip()
    if _include_changed_file(path) and path not in items:
        items.append(path)


def _git_diff_text() -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--", ".", ":!.nz-coder", ":!.nz-coder-runs"],
            cwd=current_workdir(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout if result.returncode in (0, 1) else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _git_changed_files() -> list[str]:
    files: list[str] = []
    try:
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "--", ".", ":!.nz-coder", ":!.nz-coder-runs"],
            cwd=current_workdir(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if tracked.returncode in (0, 1):
            for line in tracked.stdout.splitlines():
                _add_unique_path(files, line)

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=current_workdir(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if untracked.returncode in (0, 1):
            for line in untracked.stdout.splitlines():
                _add_unique_path(files, line)
    except (OSError, subprocess.TimeoutExpired):
        return files
    return files


def _public_symbol_changes(diff_text: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return net public deletions and changed public declarations by file."""
    removed: dict[str, dict[str, set[str]]] = {}
    added: dict[str, dict[str, set[str]]] = {}
    old_path = ""
    current_file = ""

    for line in (diff_text or "").splitlines():
        if line.startswith("--- "):
            raw = line[4:].strip()
            old_path = raw[2:] if raw.startswith("a/") else raw
            continue
        if line.startswith("+++ "):
            raw = line[4:].strip()
            new_path = raw[2:] if raw.startswith("b/") else raw
            current_file = old_path if new_path == "/dev/null" else new_path
            if current_file and current_file != "/dev/null":
                removed.setdefault(current_file, {})
                added.setdefault(current_file, {})
            continue
        if not current_file or line[:1] not in {"+", "-"}:
            continue
        if line.startswith(("+++", "---")):
            continue
        declaration = line[1:]
        for kind, pattern in _PUBLIC_SYMBOL_PATTERNS:
            match = pattern.match(declaration)
            if not match:
                continue
            name = match.group(1)
            if kind in {"class", "function"} and name.startswith("_"):
                break
            key = f"{kind}:{name}"
            target = removed if line.startswith("-") else added
            target[current_file].setdefault(key, set()).add(declaration.strip())
            break

    deleted: dict[str, list[str]] = {}
    changed: dict[str, list[str]] = {}
    for path in sorted(set(removed) | set(added)):
        removed_items = removed.get(path, {})
        added_items = added.get(path, {})
        deleted_names = [key.split(":", 1)[1] for key in removed_items if key not in added_items]
        changed_names = [
            key.split(":", 1)[1]
            for key in removed_items.keys() & added_items.keys()
            if removed_items[key] != added_items[key]
        ]
        if deleted_names:
            deleted[path] = sorted(dict.fromkeys(deleted_names))
        if changed_names:
            changed[path] = sorted(dict.fromkeys(changed_names))
    return deleted, changed


def _persistent_delete_additions(
    diff_text: str,
    changed_files: list[str],
) -> dict[str, list[str]]:
    """Return added ``.delete(...)`` calls scoped to sensitive source files."""
    sensitive = {
        str(path).replace("\\", "/").lstrip("./")
        for path in changed_files
        if is_source_file(path)
        and _SENSITIVE_PATH_RE.search(str(path).replace("\\", "/"))
    }
    if not sensitive:
        return {}

    normalized_changed = {
        str(path).replace("\\", "/").lstrip("./")
        for path in changed_files
        if str(path).strip()
    }
    matches: dict[str, list[str]] = {}
    unscoped: list[str] = []
    current_file = ""
    recent_lines: list[str] = []
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            current_file = raw[2:] if raw.startswith("b/") else raw
            current_file = current_file.replace("\\", "/").lstrip("./")
            recent_lines = []
            continue
        if line.startswith("@@"):
            recent_lines = []
            continue
        if line.startswith(("--- ", "diff --git ")):
            continue
        content = line[1:] if line[:1] in {"+", "-", " "} else line
        is_added_delete = (
            line.startswith("+")
            and not line.startswith("+++")
            and _PERSISTENT_DELETE_CALL_RE.search(content) is not None
        )
        if not is_added_delete:
            if not line.startswith("-"):
                recent_lines = [*recent_lines[-11:], content]
            continue
        evidence = "\n".join([*recent_lines[-11:], content])
        receiver_match = _DELETE_RECEIVER_RE.search(content)
        receiver = receiver_match.group(1) if receiver_match is not None else ""
        direct_query_chain = bool(
            re.search(
                r"\.(?:objects|query)\b[^\n]*\.\s*delete\s*\(",
                content,
                flags=re.IGNORECASE,
            )
        )
        receiver_query = receiver.casefold().startswith(("query", "queryset"))
        assigned_query = bool(
            receiver
            and re.search(
                rf"\b{re.escape(receiver)}\s*=[\s\S]{{0,800}}?"
                r"\.(?:objects|query)\b",
                evidence,
                flags=re.IGNORECASE,
            )
        )
        closing_query_chain = bool(
            not receiver
            and _PERSISTENT_QUERY_EVIDENCE_RE.search(evidence)
        )
        if not (
            direct_query_chain
            or receiver_query
            or assigned_query
            or closing_query_chain
        ):
            recent_lines = [*recent_lines[-11:], content]
            continue
        addition = content
        if current_file in sensitive:
            matches.setdefault(current_file, []).append(addition.strip())
        elif not current_file:
            unscoped.append(addition.strip())
        recent_lines = [*recent_lines[-11:], content]

    if unscoped and len(normalized_changed) == 1 and len(sensitive) == 1:
        only_path = next(iter(sensitive))
        matches.setdefault(only_path, []).extend(unscoped)
    return matches


def _signature_changed(diff_text: str) -> bool:
    _deleted, changed = _public_symbol_changes(diff_text)
    return bool(changed)


def _public_api_touched(changed_files: list[str], diff_text: str) -> bool:
    if any(Path(f).name in {"__init__.py", "index.ts", "index.js", "mod.rs", "lib.rs"} for f in changed_files):
        return True
    return _signature_changed(diff_text)


def _format_symbol_map(items: dict[str, list[str]]) -> str:
    return "; ".join(
        f"{path}: {', '.join(names[:8])}"
        for path, names in sorted(items.items())
    )


def _matches_requested_path(path: str, requested_paths: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for raw in requested_paths:
        requested = str(raw or "").replace("\\", "/").lstrip("./").rstrip("/")
        if not requested:
            continue
        if normalized == requested or normalized.startswith(requested + "/"):
            return True
    return False


def _related_tests(changed_files: list[str], profile: dict) -> list[str]:
    tests: list[str] = []
    roots = profile.get("test_roots") or ["tests", "test", "__tests__"]
    for rel in changed_files:
        if is_test_file(rel):
            continue
        stem = Path(rel).stem
        for root in roots:
            for candidate in (
                Path(root) / f"test_{stem}.py",
                Path(root) / f"{stem}.test.ts",
                Path(root) / f"{stem}.spec.ts",
                Path(root) / f"{stem}_test.go",
            ):
                c = candidate.as_posix()
                if c not in tests and (current_workdir() / c).exists():
                    tests.append(c)
    return tests


def analyze_patch_impact(
    changed_files: list[str] | None = None,
    diff_text: str | None = None,
    project_profile: dict | None = None,
    tests_modified: bool | None = None,
    diff_chars: int | None = None,
    requested_paths: list[str] | None = None,
    task_mode: str | None = None,
    deleted_files: list[str] | None = None,
    structural_scope: dict | None = None,
) -> dict:
    """Return a lightweight impact/risk analysis for the current patch."""
    changed = (
        [str(f) for f in changed_files if f]
        if changed_files is not None
        else _git_changed_files()
    )
    diff = diff_text if diff_text is not None else _git_diff_text()
    profile = (
        project_profile
        if project_profile is not None
        else build_project_profile(save=False)
    )
    diff_size = int(diff_chars if diff_chars is not None else len(diff))
    tests_changed = bool(tests_modified) if tests_modified is not None else any(is_test_file(f) for f in changed)
    source_files = [f for f in changed if is_source_file(f) and not is_test_file(f)]
    languages = {language_for_path(f) for f in changed if language_for_path(f) != "other"}

    reasons: list[str] = []
    review_notes: list[str] = []
    risk_signals: list[dict] = []
    risk_score = 0

    if not changed:
        reasons.append("no changed files detected")
    if len(source_files) == 1 and diff_size < 1200:
        reasons.append("single small source-file change")
    if len(source_files) >= 2:
        risk_score += 1
        reasons.append(f"{len(source_files)} source files changed")
    if len(source_files) > 4:
        risk_score += 2
        reasons.append("more than 4 source files changed")
        if task_mode != "project_creation":
            risk_signals.append({
                "category": "broad_scope_expansion",
                "severity": "replan",
                "detail": f"{len(source_files)} source files changed",
            })
    if diff_size >= 6000:
        risk_score += 1
        reasons.append(f"large diff ({diff_size} chars)")
    if diff_size >= 12000:
        risk_score += 1
    if any(_SENSITIVE_PATH_RE.search(f.replace("\\", "/")) for f in changed):
        risk_score += 2
        reasons.append("sensitive path touched (config/auth/security/database/schema/migration)")
    persistent_deletions = _persistent_delete_additions(diff, changed)
    if persistent_deletions:
        risk_score += 1
        detail = "; ".join(
            f"{path}: {', '.join(lines[:3])}"
            for path, lines in sorted(persistent_deletions.items())
        )
        reasons.append(f"persistent data deletion added in sensitive path ({detail})")
        risk_signals.append({
            "category": "persistent_data_deletion",
            "severity": "review",
            "detail": detail,
        })
        review_notes.append(
            "Semantically review added persistent-data deletion for authorization, "
            "data preservation, and integrity effects before accepting the patch."
        )
    deleted_symbols, changed_signatures = _public_symbol_changes(diff)
    if deleted_symbols:
        risk_score += 2
        detail = _format_symbol_map(deleted_symbols)
        reasons.append(f"public-looking symbols deleted ({detail})")
        risk_signals.append({
            "category": "deleted_public_symbols",
            "severity": "replan",
            "detail": detail,
        })
    if changed_signatures:
        risk_score += 1
        detail = _format_symbol_map(changed_signatures)
        reasons.append(f"public-looking API signature changed ({detail})")
        if task_mode != "project_creation":
            risk_signals.append({
                "category": "public_signature_change",
                "severity": "replan",
                "detail": detail,
            })
    elif _public_api_touched(changed, diff):
        risk_score += 1
        reasons.append("public-looking API surface file changed")
    if len(languages) > 1:
        risk_score += 1
        reasons.append("changes span multiple languages")
    if tests_changed and not source_files:
        risk_score += 2
        reasons.append("tests changed without source changes")
    elif source_files and not tests_changed and len(source_files) >= 2:
        risk_score += 1
        review_notes.append("No tests modified; ensure existing tests cover the behavior.")

    requested = [str(path) for path in (requested_paths or []) if str(path).strip()]
    outside_scope = [
        path for path in source_files
        if requested and not _matches_requested_path(path, requested)
    ]
    if outside_scope and task_mode != "project_creation":
        risk_score += 1
        detail = ", ".join(outside_scope[:8])
        reasons.append(f"source changes extend beyond user-named paths ({detail})")
        risk_signals.append({
            "category": "requested_scope_expansion",
            "severity": "replan",
            "detail": detail,
        })

    plan = plan_verification_commands(
        changed_files=changed if changed_files is not None else None,
        deleted_files=deleted_files,
        project_profile=profile,
        include_broad=False,
    )
    suggested = [item["command"] for item in plan.get("recommended", [])[:6]]
    if not suggested and changed:
        risk_score += 1
        reasons.append("no low-noise verification inferred")

    likely_tests = _related_tests(changed, profile)
    structural = structural_scope if isinstance(structural_scope, dict) else {}
    impacted_callers = [str(item) for item in structural.get("impacted_callers", [])]
    changed_symbols = [str(item) for item in structural.get("changed_symbols", [])]
    related_tests = [str(item) for item in structural.get("related_tests", [])]
    if not related_tests:
        related_tests = [
            str(item) for item in structural.get("related", []) if is_test_file(str(item))
        ]
    for path in related_tests:
        if path not in likely_tests:
            likely_tests.append(path)
    if impacted_callers:
        risk_score += 1
        reasons.append(f"{len(impacted_callers)} structural callers may be affected")
    if risk_score >= 3:
        risk = "high"
    elif risk_score >= 1:
        risk = "medium"
    else:
        risk = "low"
    if risk == "high":
        review_notes.append("Review the diff manually before finalizing; impact may cross module boundaries.")
    if tests_changed:
        review_notes.append("Confirm test changes match the user request and are not masking failures.")

    fingerprint_source = "\n".join(sorted(changed)) + "\n" + diff
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8", errors="replace")).hexdigest()[:16]
    requires_replan = any(item.get("severity") == "replan" for item in risk_signals)
    if requires_replan:
        review_notes.append(
            "Re-read the affected declarations and revise the plan before continuing; "
            "preserve public APIs and user-named scope unless the task explicitly requires otherwise."
        )

    return {
        "risk": risk,
        "fingerprint": fingerprint,
        "requires_replan": requires_replan,
        "risk_signals": risk_signals,
        "reasons": reasons or ["small patch with no high-risk path signals"],
        "affected_files": changed,
        "likely_tests": likely_tests,
        "suggested_verification": suggested,
        "review_notes": review_notes,
        "structural_impact": {
            "changed_symbols": changed_symbols,
            "impacted_callers": impacted_callers,
            "direct_callers": [str(item) for item in structural.get("direct_callers", [])],
            "transitive_callers": [str(item) for item in structural.get("transitive_callers", [])],
            "dependent_modules": [str(item) for item in structural.get("dependent_modules", [])],
            "related_tests": related_tests,
            "public_api_exposure": [str(item) for item in structural.get("public_api_exposure", [])],
            "truncated": bool(structural.get("truncated")),
            "budget": structural.get("budget", {}),
            "related": [str(item) for item in structural.get("related", [])],
            "source": structural.get("source", "call-graph" if structural else "unavailable"),
        },
    }


def format_impact_report(report: dict) -> str:
    """Format impact analysis as concise tool output."""
    lines = [
        f"Patch risk: {report.get('risk', 'unknown')}",
        f"Risk fingerprint: {report.get('fingerprint', '-')}",
        f"Requires replan: {str(bool(report.get('requires_replan'))).lower()}",
        "Reasons:",
    ]
    lines.extend(f"- {reason}" for reason in report.get("reasons", [])[:6])
    affected = report.get("affected_files", [])
    if affected:
        lines.append("Affected files:")
        lines.extend(f"- {path}" for path in affected[:8])
    likely = report.get("likely_tests", [])
    if likely:
        lines.append("Likely related tests:")
        lines.extend(f"- {test}" for test in likely[:5])
    suggested = report.get("suggested_verification", [])
    if suggested:
        lines.append("Suggested verification:")
        lines.extend(f"- {cmd}" for cmd in suggested[:6])
    notes = report.get("review_notes", [])
    if notes:
        lines.append("Review notes:")
        lines.extend(f"- {note}" for note in notes[:5])
    signals = report.get("risk_signals", [])
    if signals:
        lines.append("Risk signals:")
        lines.extend(
            f"- [{item.get('severity', 'warning')}] {item.get('category', 'unknown')}: {item.get('detail', '')}"
            for item in signals[:6]
        )
    return "\n".join(lines)


def analyze_impact() -> str:
    """Tool handler: analyze current patch impact and risk."""
    try:
        from nz_coder.state.changes import (
            current_changed_files,
            current_deleted_files,
            render_current_change_diff,
        )

        changed = current_changed_files()
        diff = render_current_change_diff() if changed else None
        structural_scope = None
        try:
            from nz_coder.intelligence.service import workspace_repo_intelligence
            service = workspace_repo_intelligence(current_workdir(), max_files=5000)
            if service is not None:
                structural_scope = service.changed_scope(
                    changed_paths=changed or None, limit=100, node_limit=100,
                    max_depth=4, time_budget_ms=100.0, wait_budget_ms=75.0,
                )
        except Exception:
            structural_scope = None
        return format_impact_report(analyze_patch_impact(
            changed_files=changed or None,
            diff_text=diff,
            deleted_files=current_deleted_files() if changed else None,
            structural_scope=structural_scope,
        ))
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="analyze_impact",
    description=(
        "Analyze current agent-tracked or Git diff impact/risk using path and diff heuristics. "
        "Returns risk, reasons, affected files, likely tests, and suggested verification."
    ),
    parameters={"type": "object", "properties": {}},
    handler=analyze_impact,
    execution="read",
)
