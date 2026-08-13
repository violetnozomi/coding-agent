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

from nz_coder.runtime.workdir import current_workdir
from nz_coder.project_profile import build_project_profile, load_project_profile
from nz_coder.task_policy import is_test_file, language_for_path
from nz_coder.tools import register


VERIFICATION_STAGE_ORDER = ("static", "targeted", "regression")
_PYTHON_PTH_STARTUP_WARNING_RE = re.compile(
    r"^Error processing line \d+ of .*?\.pth:\s*$.*?"
    r"^Remainder of file ignored\s*$",
    re.MULTILINE | re.DOTALL,
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
) -> None:
    """Add one command to the legacy list and its explicit pipeline stage."""
    _add_command(destination, command, reason)
    existing = next((item for item in stages[stage] if item["command"] == command), None)
    if existing is not None:
        existing["required"] = bool(existing.get("required")) or required
        return
    stages[stage].append({
        "command": command,
        "reason": reason,
        "required": required,
    })


def normalize_verification_command(command: str) -> str:
    """Return a stable whitespace-normalized command for evidence keys."""
    return " ".join(str(command or "").strip().split())


def _shell_parts(command: str) -> tuple[list[list[str]], list[str]]:
    """Tokenize shell segments and preserve the operators between them."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except (TypeError, ValueError):
        return [], []

    segments: list[list[str]] = []
    operators: list[str] = []
    current: list[str] = []
    for index, token in enumerate(tokens):
        if token and all(char in ";&|\n" for char in token):
            # ``shlex`` separates the ampersand in ``2>&1`` / ``&>file``.
            # Those are redirections, not control-flow operators.
            previous = current[-1] if current else ""
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if token == "&" and (
                (previous.endswith(">") and following.lstrip("-").isdigit())
                or following.startswith(">")
            ):
                current.append(token)
                continue
            if current:
                segments.append(current)
                current = []
            operators.append(token)
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments, operators


def _shell_segments(command: str) -> list[list[str]]:
    """Tokenize shell segments without treating quoted command names as calls."""
    return _shell_parts(command)[0]


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


def verification_command_segments(command: str) -> list[list[str]]:
    """Return actual command segments with wrappers removed for coverage checks."""
    result: list[list[str]] = []
    for raw_tokens in _shell_segments(command):
        tokens = _strip_command_wrappers(raw_tokens)
        if not tokens:
            continue
        executable = Path(tokens[0]).name.lower()
        args = tokens[1:]
        if executable in {"bash", "sh", "zsh"} and len(args) >= 2 and args[0] in {"-c", "-lc"}:
            result.extend(verification_command_segments(args[1]))
        else:
            result.append(tokens)
    return result


def verification_success_is_reliable(command: str) -> bool:
    """Whether one zero exit code proves every classified segment succeeded.

    A pure ``&&`` chain is reliable because the shell stops at the first
    failure.  Pipes, ``||``, semicolons, and background execution can hide a
    failed verifier behind a later successful command, so their zero exit code
    is never accepted as positive evidence.
    """
    raw_segments, operators = _shell_parts(command)
    if any(operator != "&&" for operator in operators):
        return False
    for raw_tokens in raw_segments:
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


def verification_output_failed(output: str) -> bool:
    """Return True when zero-exit output still contains clear failure evidence."""
    cleaned = _PYTHON_PTH_STARTUP_WARNING_RE.sub("", str(output or ""))
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
    canonical = [executable]
    for arg in args:
        lowered = arg.lower()
        if lowered in _PRESENTATION_FLAGS or lowered.startswith("--tb="):
            continue
        canonical.append(arg)
    return tuple(canonical)


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
    lowered = [arg.lower() for arg in args]
    if _has_flag(args, _NON_EXECUTION_FLAGS | _PYTEST_NON_EXECUTION_FLAGS):
        return None
    if any(arg in {"-k", "--lf", "--last-failed", "--ff", "--failed-first"} for arg in lowered):
        return "targeted"
    for arg in args:
        value = arg.strip()
        if not value or value.startswith("-"):
            continue
        normalized = value.replace("\\", "/")
        if "::" in normalized or normalized.endswith(".py"):
            return "targeted"
    return "regression"


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
    if re.match(r"^(python|pypy)(\d+(\.\d+)*)?$", executable) and "-m" in args:
        module_index = args.index("-m") + 1
        module = lowered_args[module_index] if module_index < len(lowered_args) else ""
        module_args = args[module_index + 1:]
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

    if executable.startswith(("python", "pypy")):
        if "-m" in args:
            module_index = args.index("-m") + 1
            module = lowered_args[module_index] if module_index < len(lowered_args) else ""
            module_args = args[module_index + 1:]
            if module in {"py_compile", "compileall", "mypy", "pyright"}:
                return "static"
            if module == "ruff" and module_args and module_args[0].lower() == "check":
                return None if any(arg.lower().startswith("--fix") for arg in module_args) else "static"
            if module in {"pytest", "py.test"}:
                return _pytest_stage(module_args)
            if module == "unittest":
                return "targeted" if any(not arg.startswith("-") for arg in module_args) else "regression"
            if module in {"tox", "nox"}:
                return "regression"
        if any(Path(arg).name == "manage.py" for arg in args) and "test" in lowered_args:
            test_index = lowered_args.index("test")
            return "targeted" if any(not arg.startswith("-") for arg in args[test_index + 1:]) else "regression"
        if "-c" in args:
            script_index = args.index("-c") + 1
            script = args[script_index].lower() if script_index < len(args) else ""
            if "assert" in script:
                return "static"

    if executable in {"pytest", "py.test", "pytest3"}:
        return _pytest_stage(args)
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
        joined = " ".join(lowered_args)
        if "typecheck" in lowered_args or "lint" in lowered_args:
            if any(arg.startswith("--fix") for arg in lowered_args):
                return None
            return "static"
        if lowered_args and (lowered_args[0] == "test" or joined.startswith("run test")):
            return "targeted" if "--" in args and args.index("--") < len(args) - 1 else "regression"
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
    for tokens in verification_command_segments(command):
        if _segment_is_non_execution(tokens):
            continue
        stage = _planned_stage_for_segment(tokens, plan) or _segment_verification_stage(tokens)
        if stage is not None:
            classified.append((stage, shlex.join(tokens)))
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
    task_mode: str | None = None,
    include_broad: bool = False,
    deleted_files: list[str] | None = None,
    related_tests: list[str] | None = None,
    use_repo_intelligence: bool = True,
) -> dict:
    """Return focused first-pass commands plus optional broader fallbacks."""
    del task_mode  # compatibility parameter; no special branching needed here.

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

    recommended: list[dict] = []
    fallback: list[dict] = []
    notes: list[str] = []
    stage_commands: dict[str, list[dict]] = {
        stage: [] for stage in VERIFICATION_STAGE_ORDER
    }
    root = current_workdir()
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
        if test.endswith(".py") or ".py::" in test or "::" in test or test.startswith(("tests/", "test/")):
            _add_planned_command(
                recommended, stage_commands, "targeted",
                f"pytest {_q(test)}", "exact failing test provided", required=True,
            )
        elif language_for_path(test) == "rust" and has_cargo_metadata:
            _add_planned_command(
                recommended, stage_commands, "targeted",
                f"cargo test {_q(Path(test).stem)}",
                "exact failing Rust test provided",
                required=True,
            )

    if py_source and ("pytest" in profile.get("test_commands", []) or profile.get("test_roots")):
        for rel in py_source[:4]:
            for candidate in _python_related_tests(rel, profile)[:2]:
                _add_planned_command(
                    recommended, stage_commands, "targeted",
                    f"pytest {_q(candidate)}",
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
                f"pytest {_q(candidate)}",
                f"related test from {structural_source or 'repository graph'}",
                required=False,
            )
        target = recommended if include_broad else fallback
        _add_planned_command(
            target, stage_commands, "regression", "pytest",
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

    stages = [
        {
            "name": stage,
            "required": any(bool(item.get("required")) for item in stage_commands[stage]),
            "commands": stage_commands[stage],
        }
        for stage in VERIFICATION_STAGE_ORDER
    ]
    return {
        "recommended": recommended,
        "fallback": fallback,
        "notes": notes,
        "stages": stages,
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
