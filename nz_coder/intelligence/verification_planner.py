"""Plan focused verification commands for repository-level code changes.

This module recommends commands; it never executes them. The goal is to keep
first-pass verification narrow and cheap, then list broader fallbacks only when
needed.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.intelligence.project_profile import build_project_profile, load_project_profile
from nz_coder.foundation.file_kinds import language_for_path
from nz_coder.runtime.agent.task_policy import _package_script_name, _python_entry, _segment_test_selection, is_test_file, native_runner_positional_selectors, unittest_selectors
from nz_coder.tools import register


VERIFICATION_STAGE_ORDER = ("static", "targeted", "regression")
# 引号和转义中的控制符属于词内容；执行时使用原文，避免重建命令改变语义。
_SHELL_WORD_RE = re.compile(
    r'''(?:[^\s;&|'"\\]|\\[\s\S]|'[^']*'|"(?:[^"\\]|\\[\s\S])*")+|[;&|]+|\n'''
)


def _q(value: str) -> str:
    return shlex.quote(str(value))


def _add_command(items: list[dict], command: str, reason: str) -> None:
    if not command:
        return
    if any(item["command"] == command for item in items):
        return
    items.append({"command": command, "reason": reason})


def _add_planned_command(
    destination: list[dict],
    stages: dict[str, list[dict]],
    stage: str,
    command: str,
    reason: str,
    *,
    required: bool,
    automation_provenance: str = "",
) -> None:
    """Add one command to the legacy list and its explicit pipeline stage."""
    _add_command(destination, command, reason)
    existing = next((item for item in stages[stage] if item["command"] == command), None)
    if existing is not None:
        existing["required"] = bool(existing.get("required")) or required
        if automation_provenance:
            existing["automation_provenance"] = automation_provenance
        return
    item = {
        "command": command,
        "reason": reason,
        "required": required,
    }
    if automation_provenance:
        item["automation_provenance"] = automation_provenance
    stages[stage].append(item)


def normalize_verification_command(command: str) -> str:
    """Return a stable whitespace-normalized command for evidence keys."""
    return " ".join(str(command or "").strip().split())


def _shell_parts(command: str) -> tuple[list[tuple[list[str], str]], list[str]]:
    """Return parsed words, original segment text, and shell control operators."""
    try:
        words = list(_SHELL_WORD_RE.finditer(command))
    except TypeError:
        return [], []

    segments: list[str] = []
    operators: list[str] = []
    start = 0
    end = 0
    for index, word in enumerate(words):
        if command[end:word.start()].strip():
            return [], []
        end = word.end()
        token = word.group()
        if token and all(char in ";&|\n" for char in token):
            previous = words[index - 1].group() if index else ""
            following = words[index + 1].group() if index + 1 < len(words) else ""
            if token == "&" and (
                (previous.endswith((">", "<")) and following.lstrip("-").isdigit())
                or following.startswith(">")
            ):
                continue
            segment = command[start:word.start()].strip()
            if segment:
                segments.append(segment)
            operators.append(token)
            start = word.end()
    if command[end:].strip():
        return [], []
    if command[start:].strip():
        segments.append(command[start:].strip())
    try:
        return [(shlex.split(segment), segment) for segment in segments], operators
    except ValueError:
        return [], []


def _strip_command_wrappers(tokens: list[str]) -> list[str]:
    """Remove common execution wrappers and environment assignments."""
    result = list(tokens)
    while result and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", result[0]):
        result.pop(0)
    if result and Path(result[0]).name.lower() == "env":
        result.pop(0)
        while result and (result[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", result[0])):
            result.pop(0)
    if result and Path(result[0]).name.lower() in {"command", "time"}:
        result.pop(0)
    if len(result) >= 2 and Path(result[0]).name.lower() in {"uv", "poetry", "pipenv"} and result[1].lower() == "run":
        result = result[2:]
    return result


def _verification_segments(command: str) -> list[tuple[list[str], str]]:
    result: list[tuple[list[str], str]] = []
    for raw_tokens, source in _shell_parts(command)[0]:
        tokens = _strip_command_wrappers(raw_tokens)
        if not tokens:
            continue
        executable = Path(tokens[0]).name.lower()
        args = tokens[1:]
        if executable in {"bash", "sh", "zsh"} and len(args) >= 2 and args[0] in {"-c", "-lc"}:
            result.extend(_verification_segments(args[1]))
        else:
            result.append((tokens, source))
    return result


def verification_command_segments(command: str) -> list[list[str]]:
    """Return actual command segments with wrappers removed for coverage checks."""
    return [tokens for tokens, _source in _verification_segments(command)]


def verification_success_is_reliable(command: str, *, pipefail: bool = False, cwd: str | None = None) -> bool:
    """Whether one zero exit code proves every classified segment succeeded.

    A pure ``&&`` chain is reliable because the shell stops at the first
    failure. A supported display pipeline also needs the executing shell's
    pipefail and the actual cwd for any directory wrapper. Other control flow
    can hide a failed verifier behind a later successful command.
    """
    raw_segments, operators = _shell_parts(command)
    if any(operator != "&&" for operator in operators):
        if not pipefail:
            return False
        from nz_coder.runtime.verification.domain.identity import command_identity, verified_check_subject
        return verified_check_subject(command_identity(command, cwd or ".")) is not None
    for raw_tokens, _source in raw_segments:
        tokens = _strip_command_wrappers(raw_tokens)
        if not tokens:
            continue
        executable = Path(tokens[0]).name.lower()
        args = tokens[1:]
        if executable in {"bash", "sh", "zsh"} and len(args) >= 2 and args[0] in {"-c", "-lc"}:
            if not verification_success_is_reliable(args[1]):
                return False
    return True


def is_python_probe_command(command: str) -> bool:
    """Return True for a real ``python -c`` command, excluding printed text."""
    for tokens in verification_command_segments(command):
        if not tokens:
            continue
        executable = Path(tokens[0]).name.lower()
        if re.match(r"^(python|pypy)(\d+(\.\d+)*)?$", executable) and "-c" in tokens[1:]:
            return True
    return False


def is_unclassified_python_script(command: str) -> bool:
    """识别无框架分类的单条脚本；不用运行中补入的计划反推脚本类型。"""
    segments, operators = _shell_parts(command)
    if operators or len(segments) != 1:
        return False
    tokens = _strip_command_wrappers(segments[0][0])
    if not tokens or not re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", Path(tokens[0]).name.lower()):
        return False
    args = tokens[1:]
    if any(arg in {"-h", "--help", "-V", "--version"} for arg in args):
        return False
    while args and args[0] in {"-u", "-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-P", "-q"}:
        args = args[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    return bool(args and not args[0].startswith("-") and args[0].endswith(".py")
                and classify_verification_command(command) is None)


def verification_output_failed(output: str) -> bool:
    """Return True when zero-exit output still contains clear failure evidence."""
    cleaned = str(output or "")
    lowered = cleaned.lower()
    if (
        "traceback (most recent call last)" in lowered
        or "no module named" in lowered
        or "setup failed" in lowered
        or "test result: failed" in lowered
    ):
        return True
    if re.search(r"\b[1-9]\d*\s+failed\b", lowered):
        return True
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if line.startswith(("FAILED ", "FAIL:", "--- FAIL:", "AssertionError")):
            return True
        if line == "FAIL" or line.startswith("FAIL "):
            return True
        if line.startswith("Test ") and " FAIL" in line:
            return True
    return False


def verification_output_has_no_tests(output: str) -> bool:
    """Return True when a successful test command explicitly ran zero tests."""
    cleaned = str(output or "")
    positive_patterns = (
        r"\b(?:collected|ran|running|found)\s+[1-9]\d*\s+(?:items?|tests?)\b",
        r"\btests run:\s*[1-9]\d*\b",
        r"\b[1-9]\d*\s+passed\b",
    )
    if any(
        re.search(pattern, cleaned, re.IGNORECASE)
        for pattern in positive_patterns
    ):
        return False

    go_empty_markers = ("[no test files]", "[no tests to run]")
    if any(marker in cleaned.casefold() for marker in go_empty_markers):
        package_summaries = [
            line.strip().casefold()
            for line in cleaned.splitlines()
            if line.lstrip().startswith(("?", "ok "))
        ]
        if package_summaries and all(
            any(marker in line for marker in go_empty_markers)
            for line in package_summaries
        ):
            return True

    return any(
        re.search(pattern, cleaned, re.IGNORECASE)
        for pattern in (
            r"\bno tests ran\b",
            r"\bno tests found\b",
            r"\bno tests to run\b",
            r"\bcollected\s+0\s+items?\b",
            r"\bran\s+0\s+tests?\b",
            r"\brunning\s+0\s+tests?\b",
            r"\bfound\s+0\s+tests?(?:\(s\))?",
            r"\btests run:\s*0\b",
            r"\btest result:\s*ok\.\s*0\s+passed\b",
            r"\btests finished:\s*0\s+passed,\s*in\s+[0-9.]+\s+seconds\b",
        )
    )


_PRESENTATION_FLAGS = {
    "-q", "--quiet", "-v", "-vv", "--verbose", "-s", "-x",
    "--disable-warnings", "--color=yes", "--color=no",
}


def _canonical_segment_tokens(tokens: list[str]) -> tuple[str, ...]:
    tokens = _strip_command_wrappers(tokens)
    if not tokens:
        return ()
    executable = Path(tokens[0]).name.lower()
    args = list(tokens[1:])
    if re.match(r"^(python|pypy)(\d+(\.\d+)*)?$", executable) and "-m" in args:
        module_index = args.index("-m") + 1
        if module_index < len(args):
            executable = args[module_index].lower()
            args = args[module_index + 1:]
    native_runner = any(
        arg.replace("\\", "/").endswith("tests/runtests.py")
        for arg in args
    )
    canonical = [executable]
    skip_parallel_value = False
    for index, arg in enumerate(args):
        if skip_parallel_value:
            skip_parallel_value = False
            continue
        lowered = arg.lower()
        if native_runner and lowered == "--parallel":
            if index + 1 < len(args) and re.fullmatch(
                r"(?:\d+|auto)", args[index + 1], re.I,
            ):
                skip_parallel_value = True
            continue
        if native_runner and lowered.startswith("--parallel="):
            continue
        if lowered in _PRESENTATION_FLAGS or lowered.startswith("--tb="):
            continue
        canonical.append(arg)
    return tuple(canonical)


def _pytest_native_runner_scopes(command: str) -> tuple[str, ...]:
    """Map pytest file selectors to the equivalent Django runner selectors."""
    scopes: list[str] = []
    for tokens in verification_command_segments(command):
        if not tokens:
            continue
        executable = Path(tokens[0]).name.lower()
        args = list(tokens[1:])
        if re.match(r"^(python|pypy)(\d+(\.\d+)*)?$", executable):
            if "-m" not in args:
                continue
            module_index = args.index("-m") + 1
            if module_index >= len(args) or args[module_index].lower() not in {
                "pytest",
                "py.test",
            }:
                continue
            args = args[module_index + 1:]
        elif executable not in {"pytest", "py.test", "pytest3"}:
            continue

        positional_only = False
        for argument in args:
            if not argument:
                continue
            if argument == "--":
                positional_only = True
                continue
            lowered = argument.lower()
            if not positional_only and argument.startswith("-"):
                if (
                    lowered in _PRESENTATION_FLAGS
                    or lowered.startswith("--tb=")
                    or lowered.startswith("--color=")
                ):
                    continue
                return ()
            path, *nodes = argument.replace("\\", "/").split("::")
            while path.startswith("./"):
                path = path[2:]
            if not path.startswith("tests/") or not path.endswith(".py"):
                return ()
            selector = path[len("tests/"):-len(".py")].replace("/", ".")
            if nodes:
                selector += "." + ".".join(nodes)
            if selector and selector not in scopes:
                scopes.append(selector)
    return tuple(scopes)


def _is_workspace_native_runner_token(token: str) -> bool:
    """Return whether a token names the exact runner verified by the planner."""
    normalized = str(token or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == "tests/runtests.py"


def _native_runner_scopes(command: str) -> tuple[str, ...]:
    """Return positional selectors from a repository-native Django runner."""
    scopes: list[str] = []
    for tokens in verification_command_segments(command):
        runner_index = next(
            (
                index
                for index, token in enumerate(tokens)
                if _is_workspace_native_runner_token(token)
            ),
            None,
        )
        if runner_index is None:
            continue
        selectors: list[str] = []
        skip_value = False
        positional_only = False
        for token in tokens[runner_index + 1:]:
            if skip_value:
                if token.startswith("-"):
                    return ()
                skip_value = False
                continue
            if positional_only:
                selectors.append(token)
                continue
            if token == "--":
                positional_only = True
                continue
            if token.startswith("-"):
                if re.fullmatch(r"-v\d+", token, re.I):
                    continue
                option, separator, value = token.partition("=")
                if option not in {"-v", "--verbosity", "--parallel"}:
                    return ()
                if separator:
                    if not value:
                        return ()
                else:
                    skip_value = True
                continue
            selectors.append(token)
        if skip_value:
            return ()
        for selector in selectors:
            normalized = selector.replace("\\", "/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            if normalized.startswith("tests/") and normalized.endswith(".py"):
                normalized = normalized[len("tests/"):-len(".py")]
            normalized = normalized.replace("/", ".").strip(".")
            if normalized and normalized not in scopes:
                scopes.append(normalized)
    return tuple(scopes)


def _native_runner_verification_scope_covers(
    planned: str,
    observed: str,
    plan: dict | None = None,
) -> bool:
    """Return whether a native Django run covers an equivalent pytest scope.

    At least one side must invoke ``tests/runtests.py``. This keeps the alias
    local to repositories with that native runner instead of treating pytest
    paths and dotted names as interchangeable in every Python project.
    """
    if str((plan or {}).get("native_runner_kind") or "") != "django":
        return False
    planned_native = _native_runner_scopes(planned)
    observed_native = _native_runner_scopes(observed)
    if not planned_native and not observed_native:
        return False
    planned_scopes = planned_native or _pytest_native_runner_scopes(planned)
    observed_scopes = observed_native or _pytest_native_runner_scopes(observed)
    return bool(planned_scopes) and set(planned_scopes) <= set(observed_scopes)


def _planned_stage_for_segment(tokens: list[str], plan: dict | None) -> str | None:
    candidate = _canonical_segment_tokens(tokens)
    if not candidate:
        return None
    for stage in (plan or {}).get("stages", []):
        if not isinstance(stage, dict):
            continue
        stage_name = str(stage.get("name") or "")
        if stage_name not in VERIFICATION_STAGE_ORDER:
            continue
        for item in stage.get("commands", []):
            if not isinstance(item, dict):
                continue
            planned_segments = verification_command_segments(str(item.get("command") or ""))
            if len(planned_segments) == 1 and _canonical_segment_tokens(planned_segments[0]) == candidate:
                return stage_name
    return None


_NON_EXECUTION_FLAGS = {"-h", "--help", "--version"}
_PYTEST_NON_EXECUTION_FLAGS = {
    "--co", "--collect-only", "--fixtures", "--fixtures-per-test",
    "--markers", "--setup-plan", "--trace-config",
}


def _has_flag(args: list[str], names: set[str]) -> bool:
    lowered = [arg.lower() for arg in args]
    return any(
        arg in names or any(arg.startswith(name + "=") for name in names if name.startswith("--"))
        for arg in lowered
    )


def _boolean_flag_enabled(args: list[str], name: str) -> bool:
    """Return whether a CLI boolean flag is present and not explicitly false."""
    lowered = [arg.lower() for arg in args]
    name = name.lower()
    for index, arg in enumerate(lowered):
        if arg == name:
            if index + 1 < len(lowered) and lowered[index + 1] in {"false", "0", "no", "off"}:
                return False
            return True
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1] not in {"false", "0", "no", "off"}
    return False


def _pytest_stage(args: list[str]) -> str | None:
    selection = _segment_test_selection(["pytest", *args])
    if selection is None:
        return None
    return "targeted" if selection.focused else "regression"


def _segment_is_non_execution(tokens: list[str]) -> bool:
    """Reject commands that only print metadata, collect, or compile tests."""
    tokens = _strip_command_wrappers(tokens)
    if not tokens:
        return False
    executable = Path(tokens[0]).name.lower()
    args = tokens[1:]
    lowered_args = [arg.lower() for arg in args]
    if "-V" in args or _has_flag(args, _NON_EXECUTION_FLAGS):
        return True
    entry = _python_entry(tokens)
    if entry is not None and entry[2]:
        module, module_args, _is_module = entry
        if module in {"pytest", "py.test"}:
            return _has_flag(module_args, _PYTEST_NON_EXECUTION_FLAGS)
        if module == "unittest":
            return _has_flag(module_args, _NON_EXECUTION_FLAGS)
    if executable in {"pytest", "py.test", "pytest3"}:
        return _has_flag(args, _PYTEST_NON_EXECUTION_FLAGS)
    if executable == "cargo" and lowered_args[:1] == ["test"]:
        if "--no-run" in lowered_args[1:]:
            return True
        if "--" in lowered_args:
            harness_args = lowered_args[lowered_args.index("--") + 1:]
            return any(arg == "--list" or arg.startswith("--list=") for arg in harness_args)
        return False
    if executable == "go" and lowered_args[:1] == ["test"]:
        return _has_flag(args[1:], {"-list", "--list"})
    return False


def _segment_verification_stage(tokens: list[str]) -> str | None:
    tokens = _strip_command_wrappers(tokens)
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    args = tokens[1:]
    lowered_args = [arg.lower() for arg in args]

    if executable in {"echo", "printf", "rg", "grep", "egrep", "fgrep", "cat", "sed", "awk"}:
        return None
    if _has_flag(args, _NON_EXECUTION_FLAGS):
        return None
    if executable in {"bash", "sh", "zsh"} and len(args) >= 2 and args[0] in {"-c", "-lc"}:
        return classify_verification_command(args[1])

    entry = _python_entry(tokens)
    if entry is not None:
        runner, runner_args, module = entry
        if not module and runner.replace("\\", "/").endswith("tests/runtests.py"):
            return "targeted" if native_runner_positional_selectors(runner_args) else "regression"
        if not module and runner.removeprefix("./") == "bin/test":
            return "targeted" if native_runner_positional_selectors(runner_args, runner="bin/test") else None
        if module:
            if runner in {"py_compile", "compileall", "mypy", "pyright"}:
                return "static"
            if runner == "ruff" and runner_args[:1] == ["check"]:
                return None if any(arg.startswith("--fix") for arg in runner_args) else "static"
            if runner in {"pytest", "py.test"}:
                return _pytest_stage(runner_args)
            if runner == "unittest":
                return "targeted" if unittest_selectors(tokens) else "regression"
            if runner in {"tox", "nox"}:
                return "regression"
        if not module and Path(runner).name == "manage.py" and runner_args[:1] == ["test"]:
            return "targeted" if any(not arg.startswith("-") for arg in runner_args[1:]) else "regression"
        if runner == "-c" and runner_args and "assert" in runner_args[0].lower():
            return "static"
    if executable.replace("\\", "/").endswith("tests/runtests.py"):
        return "targeted" if native_runner_positional_selectors(args) else "regression"

    if executable in {"pytest", "py.test", "pytest3"}:
        return _pytest_stage(args)
    if executable == "node" and args[:1] == ["--test"]:
        selection = _segment_test_selection(tokens)
        return "targeted" if selection is not None and selection.focused else "regression"
    if executable == "go" and lowered_args:
        if lowered_args[0] == "vet":
            return "static"
        if lowered_args[0] == "test":
            if _has_flag(args[1:], {"-list", "--list"}):
                return None
            if "-run" in lowered_args:
                run_index = lowered_args.index("-run") + 1
                pattern = args[run_index].strip("'\"") if run_index < len(args) else ""
                return "static" if pattern == "^$" else "targeted"
            return "regression"
    if executable == "cargo" and lowered_args:
        if lowered_args[0] in {"check", "clippy"}:
            return None if any(arg.startswith("--fix") for arg in lowered_args[1:]) else "static"
        if lowered_args[0] == "test":
            if "--no-run" in lowered_args[1:]:
                return None
            return "targeted" if any(not arg.startswith("-") for arg in args[1:]) else "regression"
    if executable in {"npm", "pnpm", "yarn"}:
        if _segment_test_selection(tokens) is not None:
            return "regression"
        if _package_script_name(tokens) in {"typecheck", "lint"}:
            if any(arg.startswith("--fix") for arg in lowered_args):
                return None
            return "static"
    if executable == "tsc":
        return "static" if _boolean_flag_enabled(args, "--noemit") else None
    if executable == "eslint":
        return None if any(arg.startswith("--fix") for arg in lowered_args) else "static"
    if executable in {"flake8", "pylint", "mypy", "pyright"}:
        return "static"
    if executable == "ruff" and lowered_args and lowered_args[0] == "check":
        return None if any(arg.startswith("--fix") for arg in lowered_args) else "static"
    if executable == "biome" and lowered_args and lowered_args[0] == "check":
        return None if any(arg.startswith(("--write", "--fix")) for arg in lowered_args) else "static"
    if executable in {"tox", "nox"}:
        return "regression"
    if executable in {"mvn", "mvnw", "gradle", "gradlew"} and "test" in lowered_args:
        if any(arg.startswith("-dtest=") for arg in lowered_args) or "--tests" in lowered_args:
            return "targeted"
        return "regression"
    if executable == "make" and "test" in lowered_args:
        return "regression"
    return None


def classify_verification_command(command: str, plan: dict | None = None) -> str | None:
    """Classify a verification command into a pipeline stage.

    Exact commands from a planner result win over heuristics. The fallback
    classifier is deliberately conservative: a single-file or test-id command
    is targeted, while a repository/package-wide test runner is regression.
    """
    normalized = normalize_verification_command(command)
    if not normalized:
        return None

    ranks = {"static": 1, "targeted": 2, "regression": 3}
    best: str | None = None
    for stage, _segment in classify_verification_segments(command, plan):
        if stage is not None and (best is None or ranks[stage] > ranks[best]):
            best = stage
    return best


def classify_verification_segments(
    command: str,
    plan: dict | None = None,
) -> list[tuple[str, str]]:
    """Return every real verification segment instead of only the highest stage."""
    classified: list[tuple[str, str]] = []
    for tokens, source in _verification_segments(command):
        if _segment_is_non_execution(tokens):
            continue
        stage = _planned_stage_for_segment(tokens, plan) or _segment_verification_stage(tokens)
        if stage is not None:
            # 分类使用去壳参数，复测执行保留原文，避免把重定向变成普通参数。
            classified.append((stage, source))
    return classified


def canonical_verification_segments(
    command: str,
    plan: dict | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    """Return stage plus canonical semantic tokens for matching and evidence keys."""
    canonical: list[tuple[str, tuple[str, ...]]] = []
    for stage, segment in classify_verification_segments(command, plan):
        segments = verification_command_segments(segment)
        if segments:
            canonical.append((stage, _canonical_segment_tokens(segments[0])))
    return canonical


def verification_command_key(command: str, plan: dict | None = None) -> str:
    """Return a stable semantic key across wrappers and presentation flags."""
    canonical = canonical_verification_segments(command, plan)
    if not canonical:
        return normalize_verification_command(command)
    return " || ".join(
        stage + ":" + "\x1f".join(sorted(tokens))
        for stage, tokens in canonical
    )


def _git_changed_files() -> list[str]:
    files: list[str] = []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--", ".", ":!.nz-coder", ":!.nz-coder-runs"],
            cwd=current_workdir(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode in (0, 1):
            files.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=current_workdir(),
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


def _git_deleted_files() -> set[str]:
    try:
        result = subprocess.run(
            [
                "git", "diff", "--diff-filter=D", "--name-only", "--",
                ".", ":!.nz-coder", ":!.nz-coder-runs",
            ],
            cwd=current_workdir(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode in (0, 1):
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return set()


def _extract_failed_tests(traceback: str | None) -> list[str]:
    if not traceback:
        return []
    found: list[str] = []
    for match in re.finditer(r"FAILED\s+([\w/\\.\-]+(?:::[\w\[\].\-]+)+)", traceback):
        found.append(match.group(1))
    return found


def _python_related_tests(path: str, profile: dict) -> list[str]:
    root = current_workdir()
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
    package_test_root = rel.parent / "tests"
    for candidate in (
        package_test_root / f"test_{stem}.py",
        package_test_root / rel.name,
        package_test_root / f"{stem}_test.py",
    ):
        candidate_str = candidate.as_posix()
        if candidate_str not in candidates and (root / candidate_str).is_file():
            candidates.append(candidate_str)

    # Mature repositories do not always use ``test_<source-stem>.py``.  Pytest,
    # for example, maps ``assertion/rewrite.py`` to ``test_assertrewrite.py``
    # and ``logging.py`` to ``logging/test_reporting.py``.  Rank a bounded
    # filename/path scan before accepting a lower-affinity call-graph edge as
    # the sole required behavior check.
    ignored = {
        "app", "core", "lib", "package", "py", "pytest", "python", "src",
        "test", "testing", "tests",
    }
    parts = [
        token
        for token in re.findall(r"[a-z0-9]+", rel.with_suffix("").as_posix().lower())
        if len(token) >= 4 and token not in ignored
    ]
    stem_tokens = set(re.findall(r"[a-z0-9]+", stem.lower()))
    ranked: list[tuple[int, str]] = []
    scanned = 0
    for test_root in test_roots:
        base = root / str(test_root)
        if not base.is_dir():
            continue
        for test_path in base.rglob("*.py"):
            scanned += 1
            if scanned > 2500:
                break
            candidate = test_path.relative_to(root).as_posix()
            if not is_test_file(candidate):
                continue
            compact = re.sub(r"[^a-z0-9]+", "", candidate.lower())
            score = sum(
                8 if signal in stem_tokens else 3
                for signal in parts
                if signal in compact
            )
            if score > 0:
                ranked.append((score, candidate))
        if scanned > 2500:
            break
    for _score, candidate in sorted(ranked, key=lambda item: (-item[0], item[1])):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _repo_intelligence_related_tests(
    changed_files: list[str],
    *,
    limit: int = 4,
) -> tuple[list[str], str]:
    """Read bounded related-test evidence from the existing workspace index.

    Verification planning never creates or owns the index. If the workspace
    runtime is cold, absent, or stale, filename/import heuristics remain the
    complete fallback.
    """
    if not changed_files:
        return [], ""
    try:
        from nz_coder.intelligence.service import workspace_repo_intelligence

        service = workspace_repo_intelligence(current_workdir(), create=False)
        if service is None:
            return [], ""
        scope = service.changed_scope(
            changed_paths=list(changed_files),
            limit=50,
            max_depth=4,
            node_limit=100,
            time_budget_ms=75.0,
            confidence_threshold=0.5,
            wait_budget_ms=50.0,
        )
        if scope.get("freshness") != "indexed":
            return [], ""
        confidence = float(scope.get("confidence") or 0.0)
        if confidence < 0.7:
            return [], ""
        tests = []
        for value in scope.get("related_tests", []):
            path = str(value or "").strip()
            if (
                path
                and is_test_file(path)
                and (current_workdir() / path).is_file()
                and path not in tests
            ):
                tests.append(path)
        return tests[:max(0, int(limit))], str(scope.get("source") or "repo intelligence")
    except Exception:
        return [], ""


def _node_commands(profile: dict) -> tuple[str | None, str | None]:
    typecheck = next(iter(profile.get("typecheck_commands", [])), None)
    test = next(iter(profile.get("test_commands", [])), None)
    return typecheck, test


def _is_native_python_test_runner(command: str) -> bool:
    """Return whether *command* invokes a repository runtests.py script."""
    try:
        tokens = shlex.split(str(command or ""))
    except ValueError:
        return False
    return any(
        token.replace("\\", "/").endswith("tests/runtests.py")
        for token in tokens
    )


def _native_python_runner_kind(command: str, root: Path) -> str:
    """Identify a repository-native runner using bounded workspace evidence."""
    if not _is_native_python_test_runner(command):
        return ""
    try:
        tokens = shlex.split(str(command or ""))
    except ValueError:
        return ""
    if not any(_is_workspace_native_runner_token(token) for token in tokens):
        return ""
    runner = root / "tests" / "runtests.py"
    django_init = root / "django" / "__init__.py"
    if not runner.is_file() or not django_init.is_file():
        return ""
    try:
        runner_text = runner.read_text(encoding="utf-8", errors="replace")[:131072]
    except OSError:
        return ""
    if re.search(r"(?m)^\s*(?:import\s+django\b|from\s+django\b)", runner_text):
        return "django"
    return ""


def _python_test_runner(profile: dict) -> str | None:
    """Prefer explicit native runners, then configured pytest commands."""
    commands = [
        str(command).strip()
        for command in profile.get("test_commands", [])
        if str(command).strip()
    ]
    native = next(
        (command for command in commands if _is_native_python_test_runner(command)),
        None,
    )
    if native:
        return native
    unittest_command = next(
        (command for command in commands
         if len(verification_command_segments(command)) == 1
         and unittest_selectors(verification_command_segments(command)[0]) is not None),
        None,
    )
    if unittest_command:
        return unittest_command
    pytest_command = next(
        (
            command
            for command in commands
            if classify_verification_command(command) == "regression"
            and "pytest" in command.casefold()
        ),
        None,
    )
    if pytest_command:
        return pytest_command
    if profile.get("test_roots") and not commands:
        return "pytest"
    return None


def _native_python_test_selector(target: str) -> str:
    """Convert a test path/node id into a runtests.py dotted label."""
    raw_path, *nodes = str(target or "").replace("\\", "/").split("::")
    path = raw_path.lstrip("./")
    parts = [part for part in path.split("/") if part]
    if parts[:1] == ["tests"]:
        parts = parts[1:]
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1:] == ["__init__"]:
        parts.pop()
    label_parts = [*parts, *(node for node in nodes if node)]
    return ".".join(label_parts) or str(target or "")


def _unittest_target_command(runner: str, target: str, *, explicit_selection: bool = False) -> str | None:
    segments, operators = _shell_parts(runner)
    if operators or len(segments) != 1:
        return None
    raw_tokens, _source = segments[0]
    tokens = _strip_command_wrappers(raw_tokens)
    entry = _python_entry(tokens)
    if entry is None or entry[0] != "unittest" or not entry[2]:
        return None
    # 保留原始前缀，避免重建带空格的环境赋值时改变 shell 的赋值语义。
    prefix_count = len(raw_tokens) - len(entry[1])
    words = list(_SHELL_WORD_RE.finditer(runner))
    prefix = runner[:words[prefix_count - 1].end()]
    args = entry[1]
    discovery = args[:1] == ["discover"]
    remaining = args[1:] if discovery else args
    behavior: list[str] = []
    top_level: str | None = None
    positional_count = 0
    index = 0
    flags = {"-v", "--verbose", "-q", "--quiet", "-f", "--failfast",
             "-c", "--catch", "-b", "--buffer", "--locals"}
    values = {"-k", "--durations"}
    discovery_values = {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory"}
    while index < len(remaining):
        token = remaining[index]
        index += 1
        option, separator, _value = token.partition("=")
        if token in flags:
            behavior.append(token)
        elif option in values | discovery_values:
            value = _value
            if not separator:
                if index == len(remaining):
                    return None
                value = remaining[index]
                index += 1
            if option in values and not (explicit_selection and option == "-k"):
                behavior.extend((option, value))
            elif discovery and option in {"-t", "--top-level-directory"}:
                top_level = value
        elif token.startswith("-k") and len(token) > 2:
            if not explicit_selection:
                behavior.append(token)
        elif token.startswith("-"):
            return None
        elif discovery:
            positional_count += 1
            if positional_count == 3:
                top_level = token
        # 起始目录/模式改为相关文件集合，top-level 决定包内相对导入语义，必须保留。
    if target.endswith(".py"):
        path = Path(target)
        discovery_context = ["-t", top_level] if top_level is not None else []
        return prefix + " " + shlex.join([
            "discover", "-s", path.parent.as_posix(), "-p", path.name, *discovery_context, *behavior,
        ])
    if all(part.isidentifier() for part in target.split(".")):
        return prefix + " " + shlex.join([*behavior, target])
    return None


def _python_target_command(
    runner: str,
    target: str,
    *,
    native_runner_kind: str = "",
    explicit_selection: bool = False,
) -> str:
    unittest_command = _unittest_target_command(runner, str(target), explicit_selection=explicit_selection)
    if any(unittest_selectors(words) is not None for words in verification_command_segments(runner)):
        return unittest_command or ""
    selector = (
        _native_python_test_selector(target)
        if native_runner_kind == "django"
        else str(target)
    )
    return f"{runner} {_q(selector)}"


def _go_package(path: str) -> str:
    parent = Path(path).parent.as_posix()
    return "." if parent in {"", "."} else "./" + parent


def _validated_changed_files(values: list[str]) -> list[str]:
    """Validate planner paths with the same workspace boundary as file tools."""
    from nz_coder.tools.files import _safe_path

    root = current_workdir().resolve()
    validated: list[str] = []
    for value in values:
        safe = _safe_path(str(value))
        relative = safe.relative_to(root).as_posix()
        if relative and relative not in validated:
            validated.append(relative)
    return validated


def plan_verification_commands(
    changed_files: list[str] | None = None,
    failing_tests: list[str] | None = None,
    traceback: str | None = None,
    project_profile: dict | None = None,
    include_broad: bool = False,
    deleted_files: list[str] | None = None,
    related_tests: list[str] | None = None,
    require_targeted: bool = False,
    use_repo_intelligence: bool = True,
) -> dict:
    """Return focused first-pass commands plus optional broader fallbacks."""
    changed_files_provided = changed_files is not None
    changed = _validated_changed_files([str(f) for f in (changed_files or []) if f])
    if not changed and not changed_files_provided:
        changed = _validated_changed_files(_git_changed_files())
    failing = [str(t) for t in (failing_tests or []) if t]
    failing.extend(t for t in _extract_failed_tests(traceback) if t not in failing)
    profile = (
        project_profile
        if project_profile is not None
        else load_project_profile()
    )
    python_test_runner = _python_test_runner(profile)
    root = current_workdir()
    native_runner_kind = _native_python_runner_kind(
        python_test_runner or "",
        root,
    )

    recommended: list[dict] = []
    fallback: list[dict] = []
    notes: list[str] = []
    stage_commands: dict[str, list[dict]] = {
        stage: [] for stage in VERIFICATION_STAGE_ORDER
    }
    has_go_metadata = any((root / name).exists() for name in ("go.mod", "go.work"))
    has_cargo_metadata = (root / "Cargo.toml").exists()
    deleted = (
        set(_validated_changed_files([str(f) for f in deleted_files if f]))
        if deleted_files is not None
        else (set() if changed_files_provided else _git_deleted_files())
    )
    if deleted:
        skipped = [path for path in changed if path in deleted]
        if skipped:
            notes.append(
                "Deleted files do not receive per-file compile commands: "
                + ", ".join(skipped[:4])
            )

    py_files = [f for f in changed if language_for_path(f) == "python"]
    py_source = [f for f in py_files if not is_test_file(f) and f not in deleted]
    for rel in py_source[:8]:
        _add_planned_command(
            recommended, stage_commands, "static",
            f"python -m py_compile {_q(rel)}",
            "changed Python source file sanity check",
            required=True,
        )

    for test in failing[:6]:
        if (test.endswith(".py") or ".py::" in test or "::" in test or test.startswith(("tests/", "test/"))
                or python_test_runner and "." in test and all(part.isidentifier() for part in test.split("."))
                and any(unittest_selectors(words) is not None for words in verification_command_segments(python_test_runner))):
            runner = python_test_runner or "pytest"
            _add_planned_command(
                recommended, stage_commands, "targeted",
                _python_target_command(
                    runner,
                    test,
                    native_runner_kind=native_runner_kind,
                    explicit_selection=True,
                ),
                "exact failing test provided",
                required=True,
                automation_provenance="failure_evidence",
            )
        elif language_for_path(test) == "rust" and has_cargo_metadata:
            _add_planned_command(
                recommended, stage_commands, "targeted",
                f"cargo test {_q(Path(test).stem)}",
                "exact failing Rust test provided",
                required=True,
                automation_provenance="failure_evidence",
            )

    if py_source and python_test_runner:
        for rel in py_source[:4]:
            for candidate in _python_related_tests(rel, profile)[:2]:
                _add_planned_command(
                    recommended, stage_commands, "targeted",
                    _python_target_command(
                        python_test_runner,
                        candidate,
                        native_runner_kind=native_runner_kind,
                    ),
                    f"related test candidate for {rel}",
                    required=False,
                )
        structural_tests = [
            str(path) for path in (related_tests or ())
            if str(path).strip() and is_test_file(str(path))
        ]
        structural_source = "provided structural evidence"
        if related_tests is None and use_repo_intelligence:
            structural_tests, structural_source = _repo_intelligence_related_tests(
                py_source, limit=4,
            )
        for candidate in structural_tests[:4]:
            if not (root / candidate).is_file():
                continue
            _add_planned_command(
                recommended,
                stage_commands,
                "targeted",
                _python_target_command(
                    python_test_runner,
                    candidate,
                    native_runner_kind=native_runner_kind,
                ),
                f"related test from {structural_source or 'repository graph'}",
                required=False,
            )
    if python_test_runner and (py_source or (py_files or failing) and any(
        unittest_selectors(words) is not None for words in verification_command_segments(python_test_runner)
    )):
        target = recommended if include_broad else fallback
        _add_planned_command(
            target, stage_commands, "regression", python_test_runner,
            "broad Python test runner", required=False,
        )

    node_files = [f for f in changed if language_for_path(f) in {"javascript", "typescript"}]
    if node_files:
        typecheck, test_cmd = _node_commands(profile)
        if typecheck:
            _add_planned_command(
                recommended, stage_commands, "static", typecheck,
                "changed JS/TS files; configured typecheck script", required=True,
            )
        else:
            notes.append("No JS/TS typecheck command detected in package.json.")
        if test_cmd:
            target = recommended if include_broad else fallback
            _add_planned_command(
                target, stage_commands, "regression", test_cmd,
                "configured JS/TS test script", required=False,
            )

    go_dirs = sorted({_go_package(f) for f in changed if language_for_path(f) == "go"})
    if go_dirs and not has_go_metadata:
        notes.append("Changed Go files but no root go.mod or go.work was found.")
    else:
        for pkg in go_dirs:
            _add_planned_command(
                recommended, stage_commands, "static", f"go test {_q(pkg)} -run '^$'",
                "changed Go package compile check", required=True,
            )
            target = recommended if include_broad else fallback
            _add_planned_command(
                target, stage_commands, "regression", f"go test {_q(pkg)}",
                "changed Go package tests", required=False,
            )

    rust_files = [f for f in changed if language_for_path(f) == "rust"]
    if rust_files and not has_cargo_metadata:
        notes.append("Changed Rust files but no root Cargo.toml was found.")
    elif rust_files:
        _add_planned_command(
            recommended, stage_commands, "static", "cargo check",
            "changed Rust files; cargo check sanity", required=True,
        )
        for test in failing[:4]:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", test):
                _add_planned_command(
                    recommended, stage_commands, "targeted", f"cargo test {_q(test)}",
                    "exact Rust failing test provided", required=True,
                    automation_provenance="failure_evidence",
                )
        target = recommended if include_broad else fallback
        _add_planned_command(
            target, stage_commands, "regression", "cargo test",
            "broad Rust tests", required=False,
        )

    if not changed:
        notes.append("No changed files detected; provide changed_files or run after applying a patch.")
    if not recommended:
        notes.append("No focused verification command could be inferred from the current profile.")

    stages = []
    for stage in VERIFICATION_STAGE_ORDER:
        command_required = any(
            bool(item.get("required")) for item in stage_commands[stage]
        )
        evidence_required = bool(require_targeted and stage == "targeted")
        stages.append({
            "name": stage,
            "required": command_required or evidence_required,
            "evidence_required": evidence_required,
            "commands": stage_commands[stage],
        })
    return {
        "recommended": recommended,
        "fallback": fallback,
        "notes": notes,
        "stages": stages,
        "native_runner_kind": native_runner_kind,
    }


def format_verification_plan(plan: dict, max_items: int = 6) -> str:
    """Format a verification plan for prompt/tool output."""
    lines = ["Recommended verification:"]
    recs = plan.get("recommended", [])
    if recs:
        for idx, item in enumerate(recs[:max_items], 1):
            lines.append(f"{idx}. {item['command']} — {item['reason']}")
    else:
        lines.append("(none)")
    fallback = plan.get("fallback", [])
    if fallback:
        lines.append("Fallback:")
        for item in fallback[:3]:
            lines.append(f"- {item['command']} — {item['reason']}")
    stages = plan.get("stages", [])
    if stages:
        lines.append("Pipeline stages:")
        for idx, stage in enumerate(stages, 1):
            commands = stage.get("commands", [])
            policy = "required" if stage.get("required") else ("optional" if commands else "unavailable")
            command_text = ", ".join(
                f"{item['command']} ({'required' if item.get('required') else 'optional'})"
                for item in commands[:2]
            ) or "(none)"
            lines.append(f"{idx}. {stage.get('name')}: {policy} — {command_text}")
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
    deleted_files: list[str] | None = None,
) -> str:
    """Tool handler: recommend verification commands for current changes."""
    try:
        profile = build_project_profile(save=False)
        plan = plan_verification_commands(
            changed_files=changed_files,
            deleted_files=deleted_files,
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
        "Recommend a staged static, targeted, and regression verification plan for current changes. "
        "Uses changed files, failing tests, project profile, and high-confidence related tests "
        "from the existing workspace repository index. Does not execute commands."
    ),
    parameters={
        "type": "object",
        "properties": {
            "changed_files": {"type": "array", "items": {"type": "string"}, "description": "Changed files. Default: git diff."},
            "deleted_files": {"type": "array", "items": {"type": "string"}, "description": "Known deleted files when changed_files is supplied."},
            "failing_tests": {"type": "array", "items": {"type": "string"}, "description": "Exact failing test ids, if known."},
            "traceback": {"type": "string", "description": "Traceback or test output excerpt."},
            "include_broad": {"type": "boolean", "description": "Include broad/full tests. Default: false."},
        },
    },
    handler=plan_verification,
    execution="read",
)
