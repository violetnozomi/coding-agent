"""Lite patch impact analysis for repository-level coding tasks.

The analyzer uses file paths and diff text heuristics rather than a full call
graph. It is intended to support final review and validation planning.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nz_coder import config
from nz_coder.project_profile import build_project_profile
from nz_coder.task_policy import is_source_file, is_test_file, language_for_path
from nz_coder.tools import register
from nz_coder.verification_planner import plan_verification_commands

_SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(config|settings|migration|migrations|schema|auth|security|database|db|models)(/|\.|$)",
    re.IGNORECASE,
)

_EXCLUDED_PARTS = {
    ".git", ".nz-coder", ".nz-coder-runs", "node_modules",
    "build", "dist", "__pycache__",
}


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
            cwd=config.WORKDIR,
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
            cwd=config.WORKDIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if tracked.returncode in (0, 1):
            for line in tracked.stdout.splitlines():
                _add_unique_path(files, line)

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=config.WORKDIR,
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


def _signature_changed(diff_text: str) -> bool:
    removed = set(re.findall(r"^-\s*(?:async\s+def|def)\s+([A-Za-z][A-Za-z0-9_]*)\s*\([^)]*\)", diff_text, re.MULTILINE))
    added = set(re.findall(r"^\+\s*(?:async\s+def|def)\s+([A-Za-z][A-Za-z0-9_]*)\s*\([^)]*\)", diff_text, re.MULTILINE))
    if removed & added:
        return True
    if re.search(r"^[+-]\s*export\s+(?:async\s+)?function\s+[A-Za-z]", diff_text, re.MULTILINE):
        return True
    if re.search(r"^[+-]\s*pub\s+fn\s+[A-Za-z]", diff_text, re.MULTILINE):
        return True
    return False


def _public_api_touched(changed_files: list[str], diff_text: str) -> bool:
    if any(Path(f).name in {"__init__.py", "index.ts", "index.js", "mod.rs", "lib.rs"} for f in changed_files):
        return True
    return _signature_changed(diff_text)


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
                if c not in tests and (config.WORKDIR / c).exists():
                    tests.append(c)
    return tests


def analyze_patch_impact(
    changed_files: list[str] | None = None,
    diff_text: str | None = None,
    project_profile: dict | None = None,
    tests_modified: bool | None = None,
    diff_chars: int | None = None,
) -> dict:
    """Return a lightweight impact/risk analysis for the current patch."""
    changed = [str(f) for f in (changed_files or []) if f] or _git_changed_files()
    diff = diff_text if diff_text is not None else _git_diff_text()
    profile = project_profile or build_project_profile(save=False)
    diff_size = int(diff_chars if diff_chars is not None else len(diff))
    tests_changed = bool(tests_modified) if tests_modified is not None else any(is_test_file(f) for f in changed)
    source_files = [f for f in changed if is_source_file(f) and not is_test_file(f)]
    languages = {language_for_path(f) for f in changed if language_for_path(f) != "other"}

    reasons: list[str] = []
    review_notes: list[str] = []
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
    if diff_size >= 6000:
        risk_score += 1
        reasons.append(f"large diff ({diff_size} chars)")
    if diff_size >= 12000:
        risk_score += 1
    if any(_SENSITIVE_PATH_RE.search(f.replace("\\", "/")) for f in changed):
        risk_score += 2
        reasons.append("sensitive path touched (config/auth/security/database/schema/migration)")
    if _public_api_touched(changed, diff):
        risk_score += 1
        reasons.append("public-looking API or function signature changed")
    if len(languages) > 1:
        risk_score += 1
        reasons.append("changes span multiple languages")
    if tests_changed and not source_files:
        risk_score += 2
        reasons.append("tests changed without source changes")
    elif source_files and not tests_changed and len(source_files) >= 2:
        risk_score += 1
        review_notes.append("No tests modified; ensure existing tests cover the behavior.")

    plan = plan_verification_commands(changed_files=changed, project_profile=profile, include_broad=False)
    suggested = [item["command"] for item in plan.get("recommended", [])[:6]]
    if not suggested and changed:
        risk_score += 1
        reasons.append("no low-noise verification inferred")

    if risk_score >= 3:
        risk = "high"
    elif risk_score >= 1:
        risk = "medium"
    else:
        risk = "low"

    likely_tests = _related_tests(changed, profile)
    if risk == "high":
        review_notes.append("Review the diff manually before finalizing; impact may cross module boundaries.")
    if tests_changed:
        review_notes.append("Confirm test changes match the user request and are not masking failures.")

    return {
        "risk": risk,
        "reasons": reasons or ["small patch with no high-risk path signals"],
        "affected_files": changed,
        "likely_tests": likely_tests,
        "suggested_verification": suggested,
        "review_notes": review_notes,
    }


def format_impact_report(report: dict) -> str:
    """Format impact analysis as concise tool output."""
    lines = [f"Patch risk: {report.get('risk', 'unknown')}", "Reasons:"]
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
    return "\n".join(lines)


def analyze_impact() -> str:
    """Tool handler: analyze current patch impact and risk."""
    try:
        return format_impact_report(analyze_patch_impact())
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="analyze_impact",
    description=(
        "Analyze current git diff impact/risk using path and diff heuristics. "
        "Returns risk, reasons, affected files, likely tests, and suggested verification."
    ),
    parameters={"type": "object", "properties": {}},
    handler=analyze_impact,
)
