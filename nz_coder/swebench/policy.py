"""Fail-closed inference policy for leaderboard-grade SWE-bench runs."""
from __future__ import annotations

import re
import shlex


STRICT_ALLOWED_TOOLS = frozenset({
    "todo",
    "bash",
    "read_file",
    "write_file",
    "write_files_batch",
    "edit_file",
    "apply_patch",
    "replace_lines",
    "list_directory",
    "grep_search",
    "glob_search",
    "diff_status",
    "verify_changed_files",
    "read_symbol",
    "find_symbol_callers",
    "repo_map",
    "code_references",
    "analyze_impact",
    "update_scratchpad",
    "read_scratchpad",
    "compact",
})

_LOCAL_COMMANDS = frozenset({
    "cat", "cmp", "cut", "diff", "file", "grep", "head", "ls", "pwd",
    "rg", "sort", "stat", "tail", "tr", "tree", "uniq", "wc",
})
_LOCAL_GIT_SUBCOMMANDS = frozenset({
    "diff", "grep", "ls-files", "rev-parse", "status",
})
_PYTHON_MODULES = frozenset({"compileall", "py_compile", "pytest"})


def validate_strict_tool_names(names: list[str]) -> list[str]:
    """Return tool names that violate the strict local-only allowlist."""
    return [str(name) for name in names if str(name) not in STRICT_ALLOWED_TOOLS]


def strict_bash_violation(command: str) -> str:
    """Allow only a small local command grammar; reject every unknown shell path."""
    value = str(command or "")
    if not value.strip():
        return "empty shell command is forbidden in SWE-bench strict mode"
    if re.search(r"[`<>\n]|\$\(|/dev/|\bhttps?://", value, flags=re.IGNORECASE):
        return "network-capable or indirect shell syntax is forbidden in SWE-bench strict mode"
    segments = [
        segment.strip()
        for segment in re.split(r"&&|\|\||[;|]", value)
        if segment.strip()
    ]
    if not segments:
        return "invalid shell command in SWE-bench strict mode"
    for segment in segments:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return "invalid shell quoting in SWE-bench strict mode"
        if not tokens:
            return "invalid shell command in SWE-bench strict mode"
        executable = tokens[0]
        if "/" in executable or "\\" in executable:
            return "executable paths are forbidden in SWE-bench strict mode"
        lowered = executable.lower()
        if lowered in _LOCAL_COMMANDS:
            if lowered == "rg" and any(token.startswith("--pre") for token in tokens[1:]):
                return "rg preprocessors are forbidden in SWE-bench strict mode"
            continue
        if lowered == "git":
            if len(tokens) < 2 or tokens[1].lower() not in _LOCAL_GIT_SUBCOMMANDS:
                return "remote or history Git commands are forbidden in SWE-bench strict mode"
            continue
        if lowered in {"python", "python3"}:
            if len(tokens) < 3 or tokens[1] != "-m" or tokens[2] not in _PYTHON_MODULES:
                return "arbitrary Python execution is forbidden in SWE-bench strict mode"
            continue
        if lowered in {"pytest", "py.test"}:
            continue
        return f"shell executable {executable!r} is not allowed in SWE-bench strict mode"
    return ""


def strict_bash_guidance(command: str, violation: str) -> str:
    """Return one model-actionable rewrite for a rejected strict command."""
    value = str(command or "")
    if re.search(r"(?:^|&&|\|\||[;|])\s*cd(?:\s|$)", value):
        return "Remove cd and set the bash.workdir argument to that workspace subdirectory."
    if "Git commands" in violation:
        return (
            "Allowed Git forms: git diff, git grep, git ls-files, "
            "git rev-parse, git status."
        )
    if "Python execution" in violation:
        return (
            "Allowed Python forms: python3 -m py_compile <file>, "
            "python3 -m compileall <path>, or python3 -m pytest <narrow-target>."
        )
    if "indirect shell syntax" in violation:
        return (
            "Remove command substitution, redirection, multiline input, device paths, "
            "and URLs; use a direct local command instead."
        )
    if "quoting" in violation:
        return "Use one directly quoted local command without nested shell syntax."
    return (
        "Use a local read command (rg, grep, ls, cat, head, tail), an allowed Git "
        "form, or an allowed python3 -m verification command."
    )
