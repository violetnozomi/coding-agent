"""Task and project policy helpers for general coding-agent behavior.

这些函数把原先散落在 SWE-bench 策略里的判断集中起来：文件类型、
测试文件识别、任务模式识别和 broad test runner 判断。保持无外部依赖，
供 tools/runtime/subagent 共享。
"""
from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import Path


_TEST_DIR_NAMES = {"test", "tests", "testing", "__tests__", "spec", "specs"}
_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.rb", "_spec.rb",
    ".test.js", ".test.jsx", ".test.ts", ".test.tsx",
    ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
    ".test.mjs", ".spec.mjs", ".test.cjs", ".spec.cjs",
)
_SOURCE_LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
}
_NATIVE_RUNNER_OPTIONS_WITH_VALUES = frozenset({
    "--bisect",
    "--durations",
    "--exclude-tag",
    "--external-host",
    "--liveserver",
    "--pair",
    "--parallel",
    "--selenium",
    "--settings",
    "--shuffle",
    "--start-after",
    "--start-at",
    "--tag",
    "--testrunner",
    "--verbosity",
    "-v",
})
_TEST_CHANGE_NEGATION_RE = re.compile(
    r"(?:\b(?:do\s+not|don't|must\s+not|never)\s+"
    r"(?:add|change|edit|modify|touch|update|write)\s+(?:the\s+)?tests?\b)"
    r"|(?:\bwithout\s+(?:adding|changing|editing|modifying|touching|updating)\s+"
    r"(?:the\s+)?tests?\b)"
    r"|(?:(?:不要|不得|不可|不允许|不)\s*"
    r"(?:新增|添加|改动|更改|修改|触碰|更新|编写)\s*(?:现有)?(?:测试|测试文件|用例))"
    r"|(?:(?:测试|测试文件|用例).{0,6}(?:保持不变|不要改|不得改|不修改))",
    re.IGNORECASE,
)
_TEST_CHANGE_REQUEST_RE = re.compile(
    r"(?:\b(?:add|create|expand|improve|modify|update|write)\s+"
    r"(?:new\s+|more\s+|the\s+)?(?:unit\s+|integration\s+|regression\s+)?"
    r"tests?\b)"
    r"|(?:\b(?:add|improve|increase)\s+(?:test\s+)?coverage\b)"
    r"|(?:\b(?:add|create|modify|update|write)\s+[^\s,;]*"
    r"(?:test_[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+_test\.[A-Za-z0-9]+)\b)"
    r"|(?:(?:新增|添加|补充|编写|更新|修改|完善|增加|写).{0,40}"
    r"(?:测试|测试文件|测试用例|用例|覆盖率|test_[A-Za-z0-9_.-]+))",
    re.IGNORECASE,
)


def normalize_path(path: str) -> str:
    """归一化为 forward-slash 路径，便于跨平台匹配。"""
    return (path or "").replace("\\", "/")


def native_runner_positional_selectors(args: list[str]) -> tuple[str, ...]:
    """Return positional test selectors after skipping known runner options."""
    selectors: list[str] = []
    skip_value = False
    positional_only = False
    for token in args:
        if skip_value:
            skip_value = False
            continue
        if positional_only:
            selectors.append(token)
            continue
        if token == "--":
            positional_only = True
            continue
        if token.startswith("-"):
            option, separator, _value = token.partition("=")
            if not separator and option in _NATIVE_RUNNER_OPTIONS_WITH_VALUES:
                skip_value = True
            continue
        selectors.append(token)
    return tuple(selectors)


def language_for_path(path: str) -> str:
    """返回路径对应的主语言，未知则返回 ``other``。"""
    suffix = Path(normalize_path(path)).suffix.lower()
    return _SOURCE_LANG_BY_EXT.get(suffix, "other")


def is_source_file(path: str) -> bool:
    """True 表示路径看起来是可编译/可检查的源码文件。"""
    return language_for_path(path) != "other"


def is_test_file(path: str) -> bool:
    """语言无关的测试文件识别。

    覆盖 Python、JS/TS、Go、Ruby 及常见 tests/spec/__tests__ 目录。
    """
    p = normalize_path(path)
    if not p:
        return False
    parts = [part.lower() for part in p.split("/") if part]
    name = parts[-1] if parts else ""
    if any(part in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    return name.startswith("test_") or name.endswith(_TEST_SUFFIXES)


def is_ephemeral_scratch_file(path: str) -> bool:
    """Return whether a path explicitly names a temporary Python scratch file."""
    normalized = normalize_path(path)
    if not normalized:
        return False
    return Path(normalized).name.casefold().endswith("_scratch.py")


def tool_output_reports_created_path(output: str, path: str) -> bool:
    """Return whether ``write_file`` reported creating, rather than updating, path."""
    expected = posixpath.normpath(normalize_path(path).strip())
    if not expected or expected == ".":
        return False
    operations: list[tuple[str, str]] = []
    for line in str(output or "").splitlines():
        match = re.fullmatch(
            r"(Created|Updated)\s+(.+?)\s+\(\d+\s+bytes\)",
            line.strip(),
        )
        if match is None:
            continue
        observed = posixpath.normpath(normalize_path(match.group(2)).strip())
        operations.append((match.group(1), observed))
    return operations == [("Created", expected)]


def update_ephemeral_scratch_lifecycle(
    tool_name: str,
    tool_input: dict,
    output: str,
    paths: list[str] | tuple[str, ...],
    active_paths: set[str],
) -> tuple[bool, set[str]]:
    """Classify one explicit scratch create/delete and update run-local tracking.

    Existing-file edits and replacement patches deliberately invalidate the
    temporary lifecycle so a scratch-looking product or test module cannot
    bypass normal verification.
    """
    normalized_paths = {
        posixpath.normpath(normalize_path(path).strip())
        for path in paths
        if isinstance(path, str) and normalize_path(path).strip()
    }
    tracked = {
        posixpath.normpath(normalize_path(path).strip())
        for path in active_paths
        if isinstance(path, str) and normalize_path(path).strip()
    }
    if not normalized_paths or not all(
        is_ephemeral_scratch_file(path) for path in normalized_paths
    ):
        return False, tracked

    if tool_name == "write_file" and len(normalized_paths) == 1:
        path = next(iter(normalized_paths))
        if tool_output_reports_created_path(output, path):
            return True, tracked | {path}
        return False, tracked - {path}

    if tool_name == "apply_patch":
        changes = tool_input.get("changes", [])
        if isinstance(changes, list) and changes:
            operations: dict[str, str] = {}
            for change in changes:
                if not isinstance(change, dict):
                    return False, tracked - normalized_paths
                path = posixpath.normpath(
                    normalize_path(str(change.get("path") or "")).strip()
                )
                if path not in normalized_paths or path in operations:
                    return False, tracked - normalized_paths
                operation = str(change.get("op", "replace")).strip().casefold()
                if operation == "create" and bool(change.get("overwrite")):
                    return False, tracked - normalized_paths
                operations[path] = operation
            if set(operations) == normalized_paths:
                if all(operation == "create" for operation in operations.values()):
                    return True, tracked | normalized_paths
                if (
                    all(operation == "delete" for operation in operations.values())
                    and normalized_paths <= tracked
                ):
                    return True, tracked - normalized_paths

    return False, tracked - normalized_paths


def is_documentation_file(path: str) -> bool:
    """Return whether a workspace path belongs to documentation-only scope."""
    normalized = normalize_path(path)
    if not normalized:
        return False
    parts = [part.casefold() for part in normalized.split("/") if part]
    suffix = Path(normalized).suffix.casefold()
    return bool(
        suffix in {".md", ".rst", ".txt"}
        or any(part in {"doc", "docs"} for part in parts[:-1])
    )




def detect_task_mode(text: str) -> str:
    """从用户文本粗略识别任务模式。

    匹配优先级是 project_creation > test > refactor > feature > bugfix > discuss > unknown。
    例如 “创建一个 FastAPI Todo API，带 pytest 测试” 会归为 project_creation，
    因为“创建项目”的意图比“包含测试”更基础也更关键。
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return "unknown"

    project_creation_markers = (
        "from scratch", "build a new project", "scaffold", "project skeleton",
        "create a fastapi project", "create a fastapi app", "create a cli tool",
        "完整 demo", "从零创建", "从 0 创建", "从0创建", "搭一个项目",
        "生成一个 fastapi 项目", "生成一个项目", "创建项目骨架", "做一个 cli 工具",
        "帮我搭一个项目", "帮我创建一个项目", "实现一个完整 demo",
    )
    refactor_markers = ("refactor", "rename", "migrate", "migration", "重构", "迁移", "改名")
    feature_markers = (
        "add ", "implement", "create", "new ", "endpoint", "feature",
        "初始化", "新建", "创建", "实现", "添加", "加一个",
    )
    bug_markers = (
        "bug", "traceback", "error", "failing", "failed", "regression",
        "报错", "失败", "修复", "问题", "异常",
    )
    discuss_markers = (
        "explain", "why", "how should", "design", "proposal", "方案", "讨论",
        "解释", "为什么", "怎么设计", "如何设计",
    )

    if any(marker in lowered for marker in project_creation_markers):
        return "project_creation"
    if (
        any(word in lowered for word in ("create", "build", "generate", "scaffold", "创建", "生成", "搭", "做一个"))
        and any(word in lowered for word in (" project", "项目", " demo", "cli", "package", "fastapi", "service", "脚手架"))
    ):
        return "project_creation"
    if task_wants_tests(text):
        return "test"
    if any(marker in lowered for marker in refactor_markers):
        return "refactor"
    if any(marker in lowered for marker in feature_markers):
        return "feature"
    if (
        re.search(r"\bfix(?:es|ed|ing)?\b", lowered)
        or any(marker in lowered for marker in bug_markers)
    ):
        return "bugfix"
    if re.search(
        r"\bsolve\s+swe-bench(?:\s+(?:lite|verified))?\s+instance\b",
        lowered,
    ):
        # A benchmark envelope always requests repository mutation even when
        # its issue title starts with a verb outside the generic feature list.
        return "feature"
    if any(marker in lowered for marker in discuss_markers) or "?" in lowered or "？" in lowered:
        return "discuss"
    return "unknown"


def task_wants_tests(text: str) -> bool:
    """True 表示用户明确要求测试相关工作。"""
    value = str(text or "")
    if task_forbids_test_changes(value):
        return False
    return _TEST_CHANGE_REQUEST_RE.search(value) is not None


def task_forbids_test_changes(text: str) -> bool:
    """Return whether the user explicitly keeps test artifacts immutable."""
    return _TEST_CHANGE_NEGATION_RE.search(str(text or "")) is not None


def estimate_text_complexity(text: str) -> str:
    """从用户任务文本预估复杂度，用于 planning 触发判断。

    不依赖 RuntimeState（planning 前没有 diff/edit 信息）。返回
    ``simple`` / ``moderate`` / ``complex``。
    """
    if not text or len(text.strip()) < 20:
        return "simple"

    lowered = text.lower()
    score = 0
    file_refs = re.findall(r"[\w/\-]+\.(?:py|js|ts|go|rs|java|rb)\b", text)
    if len(set(file_refs)) >= 3:
        score += 2
    elif len(set(file_refs)) >= 1:
        score += 1

    if re.search(r"(?:^|\n)\s*(?:\d+[.)]\s|[-*]\s)", text, re.MULTILINE):
        score += 2
    if any(w in lowered for w in ("then ", "after that", "finally", "第一步", "然后", "最后")):
        score += 1

    if len(text) > 800:
        score += 1
    if len(text) > 2000:
        score += 1

    if any(w in lowered for w in (
        "across", "multiple files", "refactor", "migrate", "rename all",
        "每个文件", "所有模块", "批量",
    )):
        score += 2

    if score >= 4:
        return "complex"
    if score >= 2:
        return "moderate"
    return "simple"


_TEST_RUNNER_MARKERS = (
    "pytest", "tox", "nox", "runtests.py", "manage.py test", "unittest",
    "npm test", "npm run test", "pnpm test", "pnpm run test",
    "yarn test", "yarn run test", "vitest", "jest",
    "go test", "cargo test", "mvn test", "gradle test", "./gradlew test",
    "make test",
)


def is_exact_test_command(command: str) -> bool:
    """True 表示命令运行的是聚焦/精确测试。"""
    cmd = " ".join((command or "").strip().lower().split())
    if not cmd:
        return False
    if not any(marker in cmd for marker in _TEST_RUNNER_MARKERS):
        return False
    if "::" in cmd:
        return True
    if re.search(r"\s-run\s+\w+", cmd):
        return True
    if " --test " in cmd or " --filter " in cmd:
        return True
    if re.search(r"\s\S+\.(?:py|js|jsx|ts|tsx|go|rs|rb)\b", cmd):
        return True
    return False


def is_broad_test_command(command: str) -> bool:
    """判断命令是否是 broad test runner。"""
    cmd = " ".join((command or "").strip().lower().split())
    if not cmd:
        return False

    if not any(marker in cmd for marker in _TEST_RUNNER_MARKERS):
        return False
    if is_exact_test_command(cmd):
        return False
    if "go test" in cmd and " ./..." not in cmd and " -run " in cmd:
        return False
    return True


_SHELL_BOUNDARIES = {"|", "||", "&&", ";"}
_PYTEST_VALUE_OPTIONS = {
    "-k", "-m", "--basetemp", "--confcutdir", "--ignore", "--ignore-glob",
    "--maxfail", "--rootdir", "--tb", "--timeout",
}


def test_command_targets(command: str) -> tuple[str, ...]:
    """Return normalized explicit test targets from a pytest command.

    The parser is deliberately conservative: a command with no path target is
    repository-wide and therefore returns an empty tuple. Shell output filters
    after a pipe are never interpreted as test scope.
    """
    try:
        tokens = shlex.split(command or "", posix=True)
    except ValueError:
        return ()
    lowered = [token.lower() for token in tokens]
    try:
        runner = lowered.index("pytest")
    except ValueError:
        return ()

    targets: list[str] = []
    skip_value = False
    for token in tokens[runner + 1:]:
        if token in _SHELL_BOUNDARIES or token.startswith((">", "2>")):
            break
        if skip_value:
            skip_value = False
            continue
        option = token.split("=", 1)[0].lower()
        if option in _PYTEST_VALUE_OPTIONS and "=" not in token:
            skip_value = True
            continue
        if token.startswith("-"):
            continue
        normalized = _normalize_test_target(token)
        if normalized and normalized not in targets:
            targets.append(normalized)
    return tuple(targets)


def declared_test_scopes(text: str) -> tuple[str, ...]:
    """Extract explicit pytest path scopes from the natural user request."""
    scopes: list[str] = []
    pattern = re.compile(
        r"(?:python(?:3(?:\.\d+)?)?\s+-m\s+)?pytest\b([^\n`。；;，,]*)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text or ""):
        command = "pytest" + match.group(1)
        for target in test_command_targets(command):
            if _looks_like_test_scope(target) and target not in scopes:
                scopes.append(target)
    return tuple(scopes)


def test_command_within_scopes(command: str, scopes: tuple[str, ...]) -> bool:
    """Return whether every explicit pytest target stays inside user scope."""
    targets = test_command_targets(command)
    normalized_scopes = tuple(
        target for target in (_normalize_test_target(scope) for scope in scopes)
        if target
    )
    if not targets or not normalized_scopes:
        return False
    return all(
        any(target == scope or target.startswith(scope + "/") for scope in normalized_scopes)
        for target in targets
    )


def _normalize_test_target(value: str) -> str:
    target = str(value or "").strip().strip("`'\".,:;()[]{}")
    target = target.split("::", 1)[0].replace("\\", "/")
    if not target or target in {".", "./"} or target.startswith("/"):
        return ""
    normalized = posixpath.normpath(target)
    if normalized == ".." or normalized.startswith("../"):
        return ""
    return normalized.removeprefix("./")


def _looks_like_test_scope(target: str) -> bool:
    parts = [part.lower() for part in target.split("/") if part]
    return bool(
        is_test_file(target)
        or any(part in _TEST_DIR_NAMES for part in parts)
    )
