"""Shared shell-command safety classification."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .shell_path_analysis import ShellPathAnalysis, analyze_shell_paths


@dataclass(frozen=True)
class ShellCommandAssessment:
    paths: ShellPathAnalysis
    classification: dict
    rejection: str
    requires_approval: bool


def assess_shell_command(
    command: str, workspace_root: Path, *, command_cwd: Path,
    read_only: bool = False, allow_package_installs: bool = True,
) -> ShellCommandAssessment:
    """共享硬限制；分析未知只产生审批要求，不能冒充确定越权。"""
    paths = analyze_shell_paths(command, workspace_root, command_cwd=command_cwd)
    classification = classify_bash(command)
    rejection = paths.rejection
    if not rejection and classification["dangerous"]:
        rejection = f"Dangerous command blocked ({classification['reason']})"
    if not rejection and not allow_package_installs and classification["package_install"]:
        rejection = "Package install blocked by current run settings"
    if not rejection and read_only and (
        paths.kind == "unsupported" or classification["mutating"] or not is_known_read_only_command(command)
    ):
        rejection = f"Read-only shell blocked ({classification['reason']})"
    return ShellCommandAssessment(paths, classification, rejection, paths.kind == "unsupported" and not rejection)


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
    """仅投影外部路径；准入调用方必须消费完整分析结论。"""
    from nz_coder.tool_platform.policies.shell_path_analysis import analyze_shell_paths
    root = Path(workspace).resolve()
    return analyze_shell_paths(command, root, command_cwd=root).escaped_path


def classify_bash(command: str, *, _depth: int = 0) -> dict:
    """按真实执行入口分类；文本、注释和脚本正文不是 Shell 命令。"""
    from nz_coder.foundation.shell_syntax import (
        ShellSyntaxError, command_options, executable_words, inline_source, python_effects, shell_commands,
        substitution_commands,
    )
    if _depth > 8:
        return {"dangerous": False, "mutating": True, "package_install": False, "reason": "shell analysis nesting limit"}
    try:
        commands = shell_commands(command or "", partial=True)
    except ShellSyntaxError:
        return {"dangerous": False, "mutating": True, "package_install": False, "reason": "unsupported shell structure"}
    mutation = ""
    package_install = False
    for segment in commands:
        for child in substitution_commands(segment):
            decision = classify_bash(child, _depth=_depth + 1)
            if decision["dangerous"]:
                return decision
            package_install |= decision["package_install"]
        if segment.opaque:
            mutation = "unsupported shell structure"
            continue
        words = executable_words(segment)
        if not words:
            continue
        name = Path(words[0].value).name.lower()
        args = [word.value for word in words[1:]]
        rendered = shlex.join([word.value for word in words])
        if name in {"sudo", "shutdown", "reboot", "poweroff", "dd", "diskpart", "format", "reg", "rm", "remove-item"} or name.startswith("mkfs"):
            for pattern, reason in _DANGEROUS_PATTERNS:
                if re.search(pattern, rendered, flags=re.IGNORECASE):
                    return {"dangerous": True, "mutating": True, "package_install": package_install, "reason": reason}
        for operator, target in segment.redirects:
            if ">" in operator and not operator.endswith("&"):
                if target.value.startswith("/dev/") and target.value != "/dev/null":
                    return {"dangerous": True, "mutating": True, "package_install": package_install, "reason": "write to device"}
                if target.value != "/dev/null":
                    mutation = "shell redirection"
        children = ()
        try:
            options = command_options(segment)
            if options.writes:
                mutation = "command output file"
            if options.incomplete_reason:
                mutation = "unsupported command options"
            program = inline_source(segment)
            children += options.commands
            if program:
                children += (python_effects(program[1]).commands if program[0] == "python"
                             else (program[1],) if program[0] == "shell" else ())
        except ShellSyntaxError:
            mutation = "unsupported interpreter structure"
        for child in children:
            decision = classify_bash(child, _depth=_depth + 1)
            if decision["dangerous"]:
                return decision
            package_install |= decision["package_install"]
        if any(word.dynamic and not (name in {"echo", "printf"} and word in words[1:] and word.parameter_only)
               for word in segment.words):
            mutation = "shell expansion"
        if name in {"rm", "del", "erase", "rmdir", "remove-item"}:
            mutation = "delete"
        elif name in {"mv", "move", "ren", "rename-item"}:
            mutation = "move or rename"
        elif name in {"cp", "copy", "copy-item", "xcopy", "robocopy"}:
            mutation = "copy"
        elif name in {"mkdir", "md", "new-item", "ni", "touch"}:
            mutation = "create file or directory"
        elif name in {"tee", "set-content", "add-content", "out-file"}:
            mutation = "write file"
        elif name in {"pip", "pip3"} and args[:1] == ["install"] or name.startswith("python") and args[:3] == ["-m", "pip", "install"]:
            mutation = "package install"
            package_install = True
        elif name in {"npm", "pnpm", "yarn"} and args and args[0] in {"install", "i", "add", "remove", "uninstall"}:
            mutation = "package manager write"
            package_install = True
        elif name in {"cargo", "go"} and args and args[0] in {"add", "get", "install"}:
            mutation = "package manager write"
            package_install = True
        elif name == "git" and args and (args[0] in {"add", "am", "apply", "checkout", "cherry-pick", "clean", "commit", "merge", "pull", "push", "rebase", "reset", "restore", "stash", "switch"}
                                          or args[0] == "branch" and not _git_readonly(args)):
            mutation = "git write operation"
    if mutation:
        return {"dangerous": False, "mutating": True, "package_install": package_install, "reason": mutation}
    return {"dangerous": False, "mutating": False, "package_install": package_install,
            "reason": "known read-only command" if is_known_read_only_command(command) else "unknown shell command"}


def _git_readonly(args: list[str]) -> bool:
    if not args or args[0] not in _READ_ONLY_GIT_SUBCOMMANDS:
        return False
    if args[0] != "branch":
        return True
    options = args[1:]
    if any(option.startswith('-') and option not in {
            '--list', '--show-current', '-a', '--all', '-r', '--remotes', '-v', '-vv', '--verbose'}
           for option in options):
        return False
    return (not options or "--list" in options or options == ["--show-current"]
            or all(option in {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose"} for option in options))


def is_known_read_only_command(command: str) -> bool:
    """只读身份只来自同一语法结构，引用数据和实际展开分别处理。"""
    from nz_coder.foundation.shell_syntax import ShellSyntaxError, command_options, executable_words, inline_source, shell_commands
    try:
        segments = shell_commands(command or "")
        if not segments:
            return False
        for segment in segments:
            words = executable_words(segment)
            if (not words or words != segment.words
                    or any(word.dynamic and not (words[0].value in {"echo", "printf"} and word in words[1:] and word.parameter_only)
                           for word in words)
                    or inline_source(segment) is not None):
                return False
            options = command_options(segment)
            if options.writes or options.commands or options.incomplete_reason:
                return False
            for operator, target in segment.redirects:
                if "<<" in operator or target.dynamic:
                    return False
                if ">" in operator and not (target.value == "/dev/null" or operator.endswith("&") and target.value in {"1", "2"}):
                    return False
            name = words[0].value.lower()
            if name == "git":
                if not _git_readonly([word.value for word in words[1:]]):
                    return False
            elif name not in _READ_ONLY_COMMANDS | {"printf"}:
                return False
            if name == "rg" and any(word.value == "--pre" or word.value.startswith("--pre=") for word in words[1:]):
                return False
        return True
    except ShellSyntaxError:
        return False
