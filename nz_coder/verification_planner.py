"""Plan minimal verification commands for repository-level code changes.

This module recommends commands; it never executes them. The rules are
heuristic by design and prefer low-noise checks before broad test runners.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nz_coder import config
from nz_coder.project_profile import build_project_profile, load_project_profile
from nz_coder.task_policy import is_test_file, language_for_path
from nz_coder.tools import register


def _add_command(items: list[dict], command: str, reason: str, level: str) -> None:
    if not command:
        return
    if any(item["command"] == command for item in items):
        return
    items.append({"command": command, "reason": reason, "level": level})


def _git_changed_files() -> list[str]:
    files: list[str] = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--", ".", ":!.nz-coder", ":!.nz-coder-runs"],
            cwd=config.WORKDIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode in (0, 1):
            files.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=config.WORKDIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if untracked.returncode in (0, 1):
            for line in untracked.stdout.splitlines():
                line = line.strip()
                if line and line not in files and not line.startswith(".nz-coder/"):
                    files.append(line)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return files


def _extract_failed_tests(traceback: str | None) -> list[str]:
    if not traceback:
        return []
    found: list[str] = []
    for match in re.finditer(r"FAILED\s+([\w/\\.\-]+(?:::[\w\[\].\-]+)+)", traceback):
        found.append(match.group(1))
    return found


def _python_related_tests(path: str, profile: dict) -> list[str]:
    root = config.WORKDIR
    rel = Path(path)
    stem = rel.stem
    parent_names = [part for part in rel.with_suffix("").parts if part not in {"src", "lib", "app"}]
    test_roots = profile.get("test_roots") or ["tests", "test"]
    candidates: list[str] = []
    for test_root in test_roots:
        root_path = Path(test_root)
        names = [
            root_path / f"test_{stem}.py",
            root_path / rel.name,
            root_path / f"{stem}_test.py",
        ]
        if parent_names:
            names.append(root_path.joinpath(*parent_names[:-1], f"test_{stem}.py"))
            names.append(root_path.joinpath(*parent_names[:-1], rel.name))
        for candidate in names:
            candidate_str = candidate.as_posix()
            if candidate_str not in candidates and (root / candidate_str).exists():
                candidates.append(candidate_str)
    return candidates


def _node_commands(profile: dict) -> tuple[str | None, str | None]:
    typecheck = next(iter(profile.get("typecheck_commands", [])), None)
    test = next(iter(profile.get("test_commands", [])), None)
    return typecheck, test


def _go_package(path: str) -> str:
    parent = Path(path).parent.as_posix()
    return "." if parent in {"", "."} else "./" + parent


def plan_verification_commands(
    changed_files: list[str] | None = None,
    failing_tests: list[str] | None = None,
    traceback: str | None = None,
    project_profile: dict | None = None,
    task_mode: str | None = None,
    include_broad: bool = False,
) -> dict:
    """Return recommended and fallback verification commands."""
    changed = [str(f) for f in (changed_files or []) if f]
    if not changed:
        changed = _git_changed_files()
    failing = [str(t) for t in (failing_tests or []) if t]
    failing.extend(t for t in _extract_failed_tests(traceback) if t not in failing)
    profile = project_profile or load_project_profile()

    recommended: list[dict] = []
    fallback: list[dict] = []
    notes: list[str] = []

    py_files = [f for f in changed if language_for_path(f) == "python"]
    py_source = [f for f in py_files if not is_test_file(f)]
    for rel in py_source[:8]:
        _add_command(recommended, f"python -m py_compile {rel}", "changed Python source file sanity check", "L0")

    for test in failing[:6]:
        if test.endswith(".py") or ".py::" in test or "::" in test or test.startswith(("tests/", "test/")):
            _add_command(recommended, f"pytest {test}", "exact failing test provided", "L1")
        elif language_for_path(test) == "rust":
            _add_command(recommended, f"cargo test {Path(test).stem}", "failing Rust test provided", "L1")

    if py_source and ("pytest" in profile.get("test_commands", []) or profile.get("test_roots")):
        for rel in py_source[:4]:
            for candidate in _python_related_tests(rel, profile)[:2]:
                _add_command(recommended, f"pytest {candidate}", f"related test candidate for {rel}", "L2")
        if include_broad:
            _add_command(recommended, "pytest", "broad Python test requested", "L4")
        else:
            _add_command(fallback, "pytest", "broad Python test runner; use only when needed", "L4")

    node_files = [f for f in changed if language_for_path(f) in {"javascript", "typescript"}]
    if node_files:
        typecheck, test_cmd = _node_commands(profile)
        if typecheck:
            _add_command(recommended, typecheck, "changed JS/TS files; configured typecheck script", "L0")
        else:
            notes.append("No JS/TS typecheck command detected in package.json.")
        if test_cmd:
            target = recommended if include_broad or task_mode == "test" else fallback
            _add_command(target, test_cmd, "configured JS/TS test script", "L4" if target is fallback else "L2")

    go_dirs = sorted({_go_package(f) for f in changed if language_for_path(f) == "go"})
    for pkg in go_dirs:
        _add_command(recommended, f"go test {pkg} -run '^$'", "changed Go package compile check", "L0")
        target = recommended if include_broad else fallback
        _add_command(target, f"go test {pkg}", "changed Go package tests", "L2" if include_broad else "L3")

    rust_files = [f for f in changed if language_for_path(f) == "rust"]
    if rust_files:
        _add_command(recommended, "cargo check", "changed Rust files; cargo check sanity", "L0")
        for test in failing[:4]:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", test):
                _add_command(recommended, f"cargo test {test}", "exact Rust failing test provided", "L1")
        target = recommended if include_broad else fallback
        _add_command(target, "cargo test", "broad Rust tests", "L4")

    if not changed:
        notes.append("No changed files detected; provide changed_files or run after applying a patch.")
    if not recommended:
        notes.append("No low-noise verification command could be inferred from the current profile.")

    return {"recommended": recommended, "fallback": fallback, "notes": notes}


def format_verification_plan(plan: dict, max_items: int = 6) -> str:
    """Format a verification plan for prompt/tool output."""
    lines = ["Recommended verification:"]
    recs = plan.get("recommended", [])
    if recs:
        for idx, item in enumerate(recs[:max_items], 1):
            lines.append(f"{idx}. {item['command']} — {item['reason']} ({item['level']})")
    else:
        lines.append("(none)")
    fallback = plan.get("fallback", [])
    if fallback:
        lines.append("Fallback:")
        for item in fallback[:3]:
            lines.append(f"- {item['command']} — {item['reason']} ({item['level']})")
    notes = plan.get("notes", [])
    if notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in notes[:4])
    return "\n".join(lines)


def plan_verification(
    changed_files: list[str] | None = None,
    failing_tests: list[str] | None = None,
    traceback: str = "",
    include_broad: bool = False,
) -> str:
    """Tool handler: recommend verification commands for current changes."""
    try:
        profile = build_project_profile(save=False)
        plan = plan_verification_commands(
            changed_files=changed_files or [],
            failing_tests=failing_tests or [],
            traceback=traceback,
            project_profile=profile,
            include_broad=include_broad,
        )
        return format_verification_plan(plan)
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="plan_verification",
    description=(
        "Recommend the smallest useful verification commands for current changes. "
        "Uses changed files, failing tests, traceback, and project profile. Does not execute commands."
    ),
    parameters={
        "type": "object",
        "properties": {
            "changed_files": {"type": "array", "items": {"type": "string"}, "description": "Changed files. Default: git diff."},
            "failing_tests": {"type": "array", "items": {"type": "string"}, "description": "Exact failing test ids, if known."},
            "traceback": {"type": "string", "description": "Traceback or test output excerpt."},
            "include_broad": {"type": "boolean", "description": "Include broad/full tests. Default: false."},
        },
    },
    handler=plan_verification,
)
