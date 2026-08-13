"""Shared shell-command safety classification."""
from __future__ import annotations

import re


_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\bsudo\b", "sudo"),
    (r"\bshutdown\b|\breboot\b|\bpoweroff\b", "system shutdown"),
    (r"\bmkfs(?:\.\w+)?\b", "format disk"),
    (r"\bdd\s+if=", "disk dump"),
    (r">\s*/dev/(?!null\b)", "write to device"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\bformat\b\s+[a-z]:", "format drive"),
    (r"\breg\s+delete\b", "registry delete"),
    (
        r"\brm\s+[^&|;\n]*-[^\s]*[rf][^\s]*\s+[/\\]?\s*(?:$|[&|;\n])",
        "recursive root delete",
    ),
    (
        r"\bRemove-Item\b[^&|;\n]*(?:-Recurse|-Force)[^&|;\n]*(?:[/\\]\s*)?(?:$|[&|;\n])",
        "recursive forced delete",
    ),
]

_SEGMENT_PREFIX = r"(?:^|(?:&&|\|\||[|;\n])\s*)"

_MUTATING_PATTERNS: list[tuple[str, str]] = [
    (r"(?<![<>])>>?(?![>])", "shell redirection"),
    (r"\|\s*(?:tee|out-file|set-content|add-content)\b", "write pipeline"),
    (rf"{_SEGMENT_PREFIX}(?:rm|del|erase|rmdir|remove-item)\b", "delete"),
    (rf"{_SEGMENT_PREFIX}(?:mv|move|ren|rename-item)\b", "move or rename"),
    (rf"{_SEGMENT_PREFIX}(?:cp|copy|copy-item|xcopy|robocopy)\b", "copy"),
    (rf"{_SEGMENT_PREFIX}(?:mkdir|md|new-item|ni|touch)\b", "create file or directory"),
    (rf"{_SEGMENT_PREFIX}(?:set-content|add-content|out-file)\b", "write file"),
    (rf"{_SEGMENT_PREFIX}(?:pip|pip3)\s+install\b", "package install"),
    (rf"{_SEGMENT_PREFIX}python(?:3(?:\.\d+)?)?\s+-m\s+pip\s+install\b", "package install"),
    (
        rf"{_SEGMENT_PREFIX}(?:npm|pnpm|yarn)\s+(?:install|i|add|remove|uninstall)\b",
        "package manager write",
    ),
    (rf"{_SEGMENT_PREFIX}(?:cargo|go)\s+(?:add|get|install)\b", "package manager write"),
    (
        rf"{_SEGMENT_PREFIX}git\s+(?:add|am|apply|checkout|cherry-pick|clean|commit|merge|pull|push|rebase|reset|restore|stash|switch)\b",
        "git write operation",
    ),
]

_READ_ONLY_COMMANDS = {
    "cat",
    "dir",
    "echo",
    "findstr",
    "gc",
    "gci",
    "get-childitem",
    "get-command",
    "get-content",
    "grep",
    "head",
    "ls",
    "pwd",
    "rg",
    "select-string",
    "tail",
    "tree",
    "type",
    "wc",
    "where",
    "where.exe",
}

_READ_ONLY_GIT_SUBCOMMANDS = {
    "branch",
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}


def classify_bash(command: str) -> dict:
    """Return a conservative safety classification for a shell command."""
    command = command or ""
    policy_command = _strip_dev_null_redirections(command)
    lowered = policy_command.lower()

    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, policy_command, flags=re.IGNORECASE):
            return {"dangerous": True, "mutating": True, "reason": reason}

    for pattern, reason in _MUTATING_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return {"dangerous": False, "mutating": True, "reason": reason}

    if not is_known_read_only_command(policy_command):
        return {"dangerous": False, "mutating": False, "reason": "unknown shell command"}

    return {"dangerous": False, "mutating": False, "reason": "known read-only command"}


def _strip_dev_null_redirections(command: str) -> str:
    """Ignore output redirections to /dev/null for mutation classification."""
    return re.sub(r"(?:[12]?>|&>)\s*/dev/null\b", "", command or "", flags=re.IGNORECASE)


def is_known_read_only_command(command: str) -> bool:
    """Whether a command is in the small allowlist used by read-only contexts."""
    segments = [segment.strip() for segment in re.split(r"(?:&&|\|\||;|\|)", command or "") if segment.strip()]
    if not segments:
        return False
    return all(_segment_is_read_only(segment) for segment in segments)


def _segment_is_read_only(segment: str) -> bool:
    tokens = re.findall(r"[^\s\"']+|\"[^\"]*\"|'[^']*'", segment)
    if not tokens:
        return False
    first = _clean_token(tokens[0])
    if first in _READ_ONLY_COMMANDS:
        return True
    if first == "git" and len(tokens) > 1:
        subcommand = _clean_token(tokens[1])
        return subcommand in _READ_ONLY_GIT_SUBCOMMANDS
    return False


def _clean_token(token: str) -> str:
    return token.strip("\"'").lower()
