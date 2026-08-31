"""Fail-closed inference policy for leaderboard-grade SWE-bench runs."""
from __future__ import annotations

import re
import shlex
from fnmatch import fnmatchcase

from nz_coder.runtime.agent.task_policy import native_runner_positional_selectors


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
_REPOSITORY_TEST_RUNNERS = frozenset({"test/runtests.py", "tests/runtests.py"})
_DOTTED_TEST_LABEL_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_PYTEST_OUTPUT_FILTER_RE = re.compile(
    r"^\s*(?P<direct>.+?)\s+2>\s*&1\s*\|\s*"
    r"(?:head|tail)\s+(?:(?:-n\s+)?[+-]?\d+)\s*$",
    re.IGNORECASE,
)
_STRICT_PRIVATE_PATH_COMPONENTS = frozenset({
    ".nz-coder",
    ".nz-coder-runs",
})
_STRICT_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "replace_lines": ("path",),
    "list_directory": ("path",),
    "read_symbol": ("path",),
    "find_symbol_callers": ("path", "include"),
    "repo_map": ("path",),
    "code_references": ("path",),
    "grep_search": ("path", "include"),
    "glob_search": ("path", "pattern"),
}
_STRICT_GLOB_FIELDS = frozenset({
    ("find_symbol_callers", "include"),
    ("grep_search", "include"),
    ("glob_search", "pattern"),
})
_SEARCH_PATTERN_OPTIONS = frozenset({"-e", "--regexp"})
_SEARCH_PATH_OPTIONS = frozenset({
    "-f",
    "--file",
    "-g",
    "--glob",
    "--iglob",
    "--include",
    "--exclude",
    "--exclude-dir",
})


def validate_strict_tool_names(names: list[str]) -> list[str]:
    """Return tool names that violate the strict local-only allowlist."""
    return [str(name) for name in names if str(name) not in STRICT_ALLOWED_TOOLS]


def strict_private_tool_input_violation(name: str, tool_input: dict) -> str:
    """Reject benchmark-private state only when an input names it as a path."""
    tool_name = str(name or "")
    values: list[tuple[object, bool]] = []
    if not isinstance(tool_input, dict):
        return ""
    for field in _STRICT_PATH_FIELDS.get(tool_name, ()):
        if field in tool_input:
            values.append((
                tool_input.get(field),
                (tool_name, field) in _STRICT_GLOB_FIELDS,
            ))
    if tool_name == "write_files_batch":
        files = tool_input.get("files")
        for item in files if isinstance(files, list) else ():
            if isinstance(item, dict):
                values.append((item.get("path"), False))
    elif tool_name == "apply_patch":
        values.append((tool_input.get("path"), False))
        changes = tool_input.get("changes")
        for change in changes if isinstance(changes, list) else ():
            if isinstance(change, dict):
                values.append((change.get("path"), False))
    elif tool_name == "bash":
        component = _strict_private_bash_path_component(
            str(tool_input.get("command") or "")
        )
        if component:
            return _strict_private_path_message(component)
    for value, is_glob in values:
        component = _strict_private_path_component(value, is_glob=is_glob)
        if component:
            return _strict_private_path_message(component)
    return ""


def _strict_private_path_message(component: str) -> str:
    return (
        f"private NZ-Coder path {component!r} is forbidden in "
        "SWE-bench strict mode"
    )


def _strict_private_path_component(value: object, *, is_glob: bool) -> str:
    if not isinstance(value, str) or not value:
        return ""
    for raw_component in re.split(r"[\\/]", value):
        component = raw_component.strip().casefold()
        if component in _STRICT_PRIVATE_PATH_COMPONENTS:
            return component
        if not is_glob:
            continue
        if any(
            fnmatchcase(private, component)
            for private in _STRICT_PRIVATE_PATH_COMPONENTS
        ):
            return next(
                private
                for private in _STRICT_PRIVATE_PATH_COMPONENTS
                if fnmatchcase(private, component)
            )
        literal_names = re.findall(r"\.[a-z0-9_-]+", component)
        for private in _STRICT_PRIVATE_PATH_COMPONENTS:
            if private in literal_names:
                return private
    return ""


def _strict_private_bash_path_component(command: str) -> str:
    for segment in re.split(r"&&|\|\||[;|]", str(command or "")):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = re.findall(r"[^\s]+", segment)
        while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        executable = tokens[0].casefold()
        if _strict_hidden_traversal(executable, tokens[1:]):
            return ".nz-coder"
        if executable in {"grep", "rg"}:
            values = _strict_search_path_values(tokens[1:])
        elif executable == "git" and len(tokens) > 1 and tokens[1].casefold() == "grep":
            values = _strict_search_path_values(tokens[2:])
        elif executable in {"pwd", "tr"}:
            values = []
        else:
            values = [(value, True) for value in tokens[1:]]
        for value, is_glob in values:
            component = _strict_private_path_component(value, is_glob=is_glob)
            if component:
                return component
    return ""


def _strict_hidden_traversal(executable: str, tokens: list[str]) -> bool:
    """Detect commands that would defeat InfCodeX-style hidden-path ignores."""
    long_options = {token.partition("=")[0] for token in tokens if token.startswith("--")}
    short_options = [
        token[1:]
        for token in tokens
        if token.startswith("-") and not token.startswith("--")
    ]
    if executable == "rg":
        return (
            "--hidden" in long_options
            or any("." in option or option.count("u") >= 2 for option in short_options)
        )
    if executable == "grep":
        return (
            "--recursive" in long_options
            or any("r" in option.casefold() for option in short_options)
        )
    if executable == "ls":
        return (
            bool(long_options & {"--all", "--almost-all"})
            or any("a" in option.casefold() for option in short_options)
        )
    if executable == "tree":
        return "--all" in long_options or any(
            "a" in option.casefold() for option in short_options
        )
    return False


def _strict_search_path_values(tokens: list[str]) -> list[tuple[str, bool]]:
    """Return grep/rg path scopes while excluding regex and replacement text."""
    positional: list[str] = []
    paths: list[tuple[str, bool]] = []
    pattern_from_option = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            positional.extend(tokens[index + 1:])
            break
        option, separator, inline_value = token.partition("=")
        if option in _SEARCH_PATTERN_OPTIONS:
            pattern_from_option = True
            if not separator:
                index += 1
        elif option in _SEARCH_PATH_OPTIONS:
            if not separator:
                index += 1
                if index < len(tokens):
                    inline_value = tokens[index]
            if inline_value:
                paths.append((inline_value, option not in {"-f", "--file"}))
        elif token.startswith("-e") and token != "-e":
            pattern_from_option = True
        elif token.startswith("-g") and token != "-g":
            paths.append((token[2:], True))
        elif not token.startswith("-"):
            positional.append(token)
        index += 1
    path_start = 0 if pattern_from_option or "--files" in tokens else 1
    paths.extend((value, False) for value in positional[path_start:])
    return paths


def normalize_strict_bash_command(command: str) -> str:
    """Remove only a bounded display filter from one direct pytest command.

    Models commonly append ``2>&1 | tail -N`` to keep test output short.  The
    strict shell correctly rejects that grammar, and executing the pipeline
    would also risk hiding pytest's exit status.  NZ-Coder already bounds Bash
    output, so execute the validated producer directly instead.  Every other
    pipeline, command family, or multi-command input remains byte-for-byte
    unchanged and therefore reaches the normal fail-closed policy.
    """
    value = str(command or "")
    match = _PYTEST_OUTPUT_FILTER_RE.fullmatch(value)
    if match is None:
        return value
    direct = match.group("direct").strip()
    if re.search(r"[`<>&;|\n]|\$\(", direct):
        return value
    try:
        tokens = shlex.split(direct)
    except ValueError:
        return value
    lowered = [token.casefold() for token in tokens]
    is_pytest = bool(
        lowered
        and (
            lowered[0] in {"pytest", "py.test"}
            or (
                len(lowered) >= 3
                and lowered[0] in {"python", "python3"}
                and lowered[1:3] == ["-m", "pytest"]
            )
        )
    )
    if not is_pytest or strict_bash_violation(direct):
        return value
    return direct


def strict_bash_violation(command: str) -> str:
    """Allow only a small local command grammar; reject every unknown shell path."""
    value = str(command or "")
    if not value.strip():
        return "empty shell command is forbidden in SWE-bench strict mode"
    private_path_violation = strict_private_tool_input_violation(
        "bash", {"command": value},
    )
    if private_path_violation:
        return private_path_violation
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
        if tokens[0] == "PYTHONPATH=.":
            tokens = tokens[1:]
            if not _is_narrow_repository_test_runner(tokens):
                return "unsafe environment assignment is forbidden in SWE-bench strict mode"
        elif "=" in tokens[0]:
            return "unsafe environment assignment is forbidden in SWE-bench strict mode"
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
            allowed_module = (
                len(tokens) >= 3
                and tokens[1] == "-m"
                and tokens[2] in _PYTHON_MODULES
            )
            allowed_test_runner = _is_narrow_repository_test_runner(tokens)
            if not allowed_module and not allowed_test_runner:
                return "arbitrary Python execution is forbidden in SWE-bench strict mode"
            continue
        if lowered in {"pytest", "py.test"}:
            continue
        return f"shell executable {executable!r} is not allowed in SWE-bench strict mode"
    return ""


def _is_narrow_repository_test_runner(tokens: list[str]) -> bool:
    """Allow one conventional in-repository runner with a concrete test label."""
    if len(tokens) < 3:
        return False
    runner = tokens[1]
    if runner.startswith("./"):
        runner = runner[2:]
    if runner not in _REPOSITORY_TEST_RUNNERS:
        return False
    selectors = native_runner_positional_selectors(tokens[2:])
    return any(_DOTTED_TEST_LABEL_RE.fullmatch(token) for token in selectors)


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
            "python3 -m compileall <path>, python3 -m pytest <narrow-target>, "
            "or python3 tests/runtests.py <dotted.test.label>."
        )
    if "environment assignment" in violation:
        return (
            "Only PYTHONPATH=. may prefix "
            "python3 tests/runtests.py <dotted.test.label>."
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
