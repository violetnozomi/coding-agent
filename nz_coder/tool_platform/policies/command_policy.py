"""Shared shell-command safety classification."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path, PureWindowsPath


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

_SEGMENT_PREFIX = r"(?:^|(?:&&|\|\||[|;&\n])\s*)"
_SHELL_EVALUATION_PATTERN = r"\$\(|`|(?:<|>)\("

_MUTATING_PATTERNS: list[tuple[str, str]] = [
    (_SHELL_EVALUATION_PATTERN, "shell command substitution"),
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


def external_workspace_path(command: str, workspace: str | Path) -> str | None:
    """Return the first shell path that escapes *workspace*.

    Bash already bounds its working directory, but command arguments used to be
    able to name arbitrary absolute or parent-relative paths. This lexical gate
    covers POSIX and Windows-looking paths without requiring targets to exist.
    """
    root = Path(workspace).resolve()
    raw_expansion = re.search(
        r"(?:%[A-Za-z_]\w*%|\$env:[A-Za-z_]\w*)[\\/][^\s\"';&|]*",
        str(command or ""),
        flags=re.IGNORECASE,
    )
    if raw_expansion is not None:
        return raw_expansion.group(0)
    tokens: list[tuple[str, bool]] = []
    for segment in re.split(r"(?:&&|\|\||[;|&\n])", str(command or "")):
        try:
            segment_tokens = shlex.split(segment, posix=os.name != "nt")
        except ValueError:
            segment_tokens = re.findall(r"[^\s]+", segment)
        command_seen = False
        for value in segment_tokens:
            assignment = not command_seen and re.match(r"^[A-Za-z_]\w*=", value)
            is_command = not command_seen and assignment is None
            if is_command:
                command_seen = True
            tokens.append((value, is_command))
    for raw, is_command in tokens:
        token = raw.strip().strip(";,|()[]{}")
        if not token or token in {"/dev/null", "NUL", "nul"} or "://" in token:
            continue
        if "=" in token and not token.startswith(("/", "\\")):
            _name, token = token.split("=", 1)
            token = token.strip("\"'")
        if is_command:
            # System interpreters are often invoked by absolute executable
            # path. Their data arguments are still checked below.
            continue
        if re.match(
            r"^(?:~(?:[^/\\]*)|\$\{?[A-Za-z_]\w*\}?|%[^%]+%|\$env:[A-Za-z_]\w*)"
            r"(?:[/\\]|$)",
            token,
            flags=re.IGNORECASE,
        ):
            return token
        if re.match(r"^[A-Za-z]:[\\/]", token):
            if os.name != "nt":
                return str(PureWindowsPath(token))
            candidate = Path(token).resolve()
        elif token.startswith(("/", "\\\\")):
            candidate = Path(token).resolve()
        elif token == ".." or token.startswith(("../", "..\\")):
            candidate = (root / token).resolve()
        elif not token.startswith("-"):
            # Resolve even apparently local arguments so an in-workspace
            # symlink cannot turn a read-only command into an external read.
            candidate = (root / token).resolve()
        else:
            continue
        try:
            candidate.relative_to(root)
        except ValueError:
            return token
    return None


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
    """Ignore redirections that cannot write a workspace file."""
    cleaned = re.sub(
        r"(?:[12]?>|&>)\s*/dev/null\b",
        "",
        command or "",
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?<!\S)\d*[<>]\s*&\s*(?:\d+|-)(?=\s|$)",
        "",
        cleaned,
    )


def is_known_read_only_command(command: str) -> bool:
    """Whether a command is in the small allowlist used by read-only contexts."""
    if re.search(_SHELL_EVALUATION_PATTERN, command or ""):
        return False
    segments = [
        segment.strip()
        for segment in re.split(r"(?:&&|\|\||[;|&\n])", command or "")
        if segment.strip()
    ]
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
