"""Shared shell-command safety classification."""
from __future__ import annotations


import re


_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    # IMPROVED: 每条规则旁附示例，方便维护时快速理解匹配场景。
    (r"\bsudo\b",                                                            "sudo"),             # sudo rm -rf /
    (r"\bshutdown\b|\breboot\b|\bpoweroff\b",                               "system shutdown"),  # shutdown -h now
    (r"\bmkfs(?:\.\w+)?\b",                                                 "format disk"),      # mkfs.ext4 /dev/sda1
    (r"\bdd\s+if=",                                                          "disk dump"),        # dd if=/dev/zero of=/dev/sda
    (r">\s*/dev/",                                                           "write to device"),  # echo 1 > /dev/sda
    (r"\bdiskpart\b",                                                        "disk partitioning"),# diskpart (Windows)
    (r"\bformat\b\s+[a-z]:",                                                "format drive"),     # format C: (Windows)
    (r"\breg\s+delete\b",                                                    "registry delete"),  # reg delete HKLM\...
    (r"\brm\s+[^&|;\n]*-[^\s]*[rf][^\s]*\s+[/\\]?\s*(?:$|[&|;\n])",       "recursive root delete"),  # rm -rf /
    (r"\bRemove-Item\b[^&|;\n]*(?:-Recurse|-Force)[^&|;\n]*(?:[/\\]\s*)?(?:$|[&|;\n])", "recursive forced delete"),  # Remove-Item -Recurse -Force /
]

_SEGMENT_PREFIX = r"(?:^|(?:&&|\|\||[|;\n])\s*)"

_MUTATING_PATTERNS: list[tuple[str, str]] = [
    # IMPROVED: 每条规则旁附示例，方便维护时快速理解匹配场景。
    (r"(?<![<>])>>?(?![>])",                                                "shell redirection"),    # echo foo > out.txt
    (r"\|\s*(?:tee|out-file|set-content|add-content)\b",                   "write pipeline"),       # cmd | tee file.txt
    (rf"{_SEGMENT_PREFIX}(?:rm|del|erase|rmdir|remove-item)\b",            "delete"),               # rm foo.py
    (rf"{_SEGMENT_PREFIX}(?:mv|move|ren|rename-item)\b",                   "move or rename"),       # mv a.py b.py
    (rf"{_SEGMENT_PREFIX}(?:cp|copy|copy-item|xcopy|robocopy)\b",          "copy"),                 # cp src dst
    (rf"{_SEGMENT_PREFIX}(?:mkdir|md|new-item|ni|touch)\b",                "create file or directory"),  # mkdir build
    (rf"{_SEGMENT_PREFIX}(?:set-content|add-content|out-file)\b",          "write file"),           # Set-Content file.txt value
    (rf"{_SEGMENT_PREFIX}(?:pip|pip3)\s+install\b",                        "package install"),      # pip install requests
    (rf"{_SEGMENT_PREFIX}python(?:3(?:\.\d+)?)?\s+-m\s+pip\s+install\b",   "package install"),      # python3 -m pip install foo
    (rf"{_SEGMENT_PREFIX}(?:npm|pnpm|yarn)\s+(?:install|i|add|remove|uninstall)\b", "package manager write"),  # npm install
    (rf"{_SEGMENT_PREFIX}(?:cargo|go)\s+(?:add|get|install)\b",            "package manager write"),# cargo add serde
    (rf"{_SEGMENT_PREFIX}git\s+(?:add|am|apply|checkout|cherry-pick|clean|commit|merge|pull|push|rebase|reset|restore|stash|switch)\b", "git write operation"),  # git commit -m "..."
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
    lowered = command.lower()

    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return {"dangerous": True, "mutating": True, "reason": reason}

    for pattern, reason in _MUTATING_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return {"dangerous": False, "mutating": True, "reason": reason}

    if not is_known_read_only_command(command):
        return {"dangerous": False, "mutating": False, "reason": "unknown shell command"}

    return {"dangerous": False, "mutating": False, "reason": "known read-only command"}


def is_known_read_only_command(command: str) -> bool:
    """Whether a command is in the small allowlist used by read-only contexts."""
    segments = [s.strip() for s in re.split(r"(?:&&|\|\||;|\|)", command or "") if s.strip()]
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
