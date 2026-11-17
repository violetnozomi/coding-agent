"""Task and project policy helpers for general coding-agent behavior.

这些函数把原先散落在 SWE-bench 策略里的判断集中起来：文件类型、
测试文件识别、任务模式识别和 broad test runner 判断。保持无外部依赖，
供 tools/runtime/subagent 共享。
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


from nz_coder.foundation.file_kinds import (
    is_documentation_file,
    is_test_file,
)
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
    r"(?:新增|添加|改动|更改|修改|触碰|更新|编写)\s*(?:现有)?(?:测试|验收文件|用例))"
    r"|(?P<test_subject>测试|测试文件|验收文件|用例)"
    r"(?P<test_link>.{0,6})(?P<test_predicate>保持不变|不要改|不得改|不修改)",
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


def native_runner_positional_selectors(
    args: list[str], *, runner: str = "runtests.py",
) -> tuple[str, ...]:
    """Return positional test selectors after skipping known runner options."""
    if runner == "bin/test":
        # bin/test 的 -v 是开关，-k 接收多个关键字，不能沿用 Django 的参数语义。
        selectors: list[str] = []
        index = 0
        flags = {"-v", "--verbose", "--no-colors", "--force-colors", "--random",
                 "-C", "--no-cache", "--no-subprocess", "-E", "--enhance-asserts"}
        while index < len(args):
            token = args[index]
            if token in flags:
                index += 1
                continue
            option, separator, value = token.partition("=")
            if option in {"--tb", "--seed", "--timeout", "-t", "--types"}:
                if not separator:
                    index += 1
                    if index == len(args):
                        return ()
                    value = args[index]
                valid = (
                    value in {"short", "no"} if option == "--tb" else
                    value in {"gmpy", "gmpy1", "python"} if option in {"-t", "--types"} else
                    value.isascii() and value.isdigit()
                )
                if not valid:
                    return ()
            elif token == "-k":
                index += 1
                start = index
                while index < len(args) and not args[index].startswith("-"):
                    if not re.fullmatch(r"[A-Za-z0-9_ ]+", args[index]):
                        return ()
                    index += 1
                if index == start or not selectors:
                    return ()
                continue
            elif (
                not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_]+(?:\.py)?", token)
            ):
                return ()
            else:
                selectors.append(token)
            index += 1
        return tuple(selectors)
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


def task_wants_tests(text: str) -> bool:
    """True 表示用户明确要求测试相关工作。"""
    value = str(text or "")
    if task_forbids_test_changes(value):
        return False
    return _TEST_CHANGE_REQUEST_RE.search(value) is not None


def task_forbids_test_changes(text: str) -> bool:
    """Return whether the user explicitly keeps test artifacts immutable."""
    value = str(text or "")
    offset = 0
    while match := _TEST_CHANGE_NEGATION_RE.search(value, offset):
        # 保留既有候选集合；只核对原来跨分句串接的“测试→禁改”关系。
        if match.group("test_subject") is None or not re.search(
            r"[，,。；;、！!？?]", match.group("test_link")
        ):
            return True
        material = next(
            (name for name in ("测试文件", "测试用例", "验收文件", "用例")
             if value.startswith(name, match.start())),
            "",
        )
        if material and re.match(
            r"[^\S\n]*[，,。；;、！!？?]",
            value[match.start() + len(material):match.start("test_predicate")],
        ):
            return True
        if value[max(0, match.start() - 2):match.start()] in {"现有", "原有", "已有"}:
            return True
        predicate = match.group("test_predicate")
        following = re.split(
            r"[，,。；;、！!？?\n]", value[match.end():], maxsplit=1,
        )[0].strip()
        # 原候选“不要改/不得改”可能停在完整动词前；其后才是宾语。
        if predicate in {"不要改", "不得改"} and following.startswith(("动", "写", "变")):
            following = following[1:].strip()
        if predicate == "保持不变" or not following or following.startswith(("任何", "任意")):
            return True
        # 候选可能覆盖紧随其后的“不要修改测试”；从起点后继续查，保留重叠约束。
        offset = match.start() + 1
    return False


_PYTHON_FLAGS = {"-u", "-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-P", "-q", "-b", "-bb"}


def _python_entry(tokens: list[str] | tuple[str, ...]) -> tuple[str, list[str], bool] | None:
    """只沿真实解释器入口跳过已知开关，不在脚本参数中搜索 -m。"""
    if not tokens or not re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", Path(tokens[0]).name):
        return None
    args = list(tokens[1:])
    while args:
        if args[0] in _PYTHON_FLAGS:
            args.pop(0)
        elif args[0] in {"-W", "-X", "--check-hash-based-pycs"}:
            if len(args) < 2:
                return None
            args = args[2:]
        elif args[0].startswith(("-W", "-X")) and len(args[0]) > 2:
            args.pop(0)
        else:
            break
    if args[:1] == ["-m"] and len(args) >= 2:
        return args[1], args[2:], True
    if args[:1] == ["-c"] and len(args) >= 2:
        return "-c", args[1:], False
    if args and not args[0].startswith("-"):
        return args[0], args[1:], False
    return None


def unittest_selectors(tokens: list[str] | tuple[str, ...]) -> tuple[str, ...] | None:
    """保留完整原生选择器；未知选项和 discover 不证明唯一文件范围。"""
    entry = _python_entry(tokens)
    if entry is None or entry[0] != "unittest" or not entry[2]:
        return None
    return _unittest_selection(entry[1]).targets


def _unittest_selection(args: list[str]) -> _TestSelection:
    if args and args[0] == "discover":
        # 发现参数定义递归集合，不能承诺唯一物理文件；未知参数保持未知。
        values = {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory", "-k", "--durations"}
        flags = {"-v", "--verbose", "-q", "--quiet", "-f", "--failfast", "-c", "--catch", "-b", "--buffer", "--locals"}
        index = 1
        while index < len(args):
            token = args[index]
            index += 1
            option, separator, value = token.partition("=")
            if option in values:
                if not separator:
                    if index >= len(args):
                        return _TestSelection("unknown")
                    value = args[index]
                    index += 1
                if not value:
                    return _TestSelection("unknown")
            elif token.startswith("-") and token not in flags:
                return _TestSelection("unknown")
        return _TestSelection("all")
    selectors: list[str] = []
    index = 0
    positional = False
    flags = {"-v", "--verbose", "-q", "--quiet", "-f", "--failfast",
             "-c", "--catch", "-b", "--buffer", "--locals"}
    while index < len(args):
        token = args[index]
        index += 1
        if not positional and token == "--":
            positional = True
            continue
        if not positional and token in flags:
            continue
        if not positional and token.split("=", 1)[0] == "--durations":
            if "=" in token:
                value = token.split("=", 1)[1]
            else:
                if index >= len(args):
                    return _TestSelection("unknown")
                value = args[index]
                index += 1
            if not re.fullmatch(r"[+-]?\d+", value):
                return _TestSelection("unknown")
            continue
        if not positional and token == "-k":
            if index >= len(args) or args[index].startswith("-"):
                return _TestSelection("unknown")
            index += 1
            continue
        if not positional and token.startswith("-k") and len(token) > 2:
            continue
        if token.startswith("-"):
            return _TestSelection("unknown")
        normalized = _normalize_test_target(token)
        if not normalized:
            return _TestSelection("unknown")
        if not normalized.endswith(".py") and not all(part.isidentifier() for part in normalized.split(".")):
            return _TestSelection("unknown")
        if normalized not in selectors:
            selectors.append(normalized)
    return _TestSelection("bounded" if selectors else "all", tuple(selectors), bool(selectors))


def unittest_selector_matches_path(selector: str, path: str) -> bool:
    """从已知测试文件生成模块名，避免猜测点号选择器中类名的分界。"""
    relative = str(path).replace("\\", "/").removeprefix("./")
    if not relative.endswith(".py") or relative.startswith("/") or ".." in relative.split("/"):
        return False
    module = relative[:-3].replace("/", ".")
    return selector == relative or selector == module or selector.startswith(module + ".")


def declared_unittest_scopes(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        target for command in _declared_test_commands(text)
        for words in _command_segments(command)
        for target in unittest_selectors(words) or ()
        if target in test_command_targets(command)
    ))


def canonical_unittest_discovery(command: str) -> str | None:
    """保留完整字面量 discover 集合身份；不把其参数冒充测试选择器。"""
    quote, escaped = "", False
    for char in command:
        if char in "\n\r":
            return None
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char in "$`":
                return None
        elif char in "'\"":
            quote = char
        elif char in ";&|><$`*?[]{}~":
            return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    entry = _python_entry(tokens)
    if (entry is None or not entry[2] or entry[0] != "unittest"
            or entry[1][:1] != ["discover"]):
        return None
    argument_start = len(tokens) - len(entry[1]) + 1
    result = list(tokens[:argument_start])
    values = {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory",
              "-k", "--durations"}
    flags = {"-f", "--failfast", "-c", "--catch", "-b", "--buffer", "--locals"}
    index, positionals, positional_only = argument_start, 0, False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        option, separator, value = token.partition("=")
        if not positional_only and token == "--":
            positional_only = True
            result.append(token)
        elif not positional_only and token in {"-v", "--verbose", "-q", "--quiet"}:
            continue
        elif not positional_only and option in values:
            if separator:
                if not value:
                    return None
                result.append(token)
            else:
                if index >= len(tokens) or tokens[index].startswith("-"):
                    return None
                result.extend((token, tokens[index]))
                index += 1
        elif not positional_only and token in flags:
            result.append(token)
        elif not positional_only and token.startswith("-"):
            return None
        else:
            positionals += 1
            if positionals > 3:
                return None
            result.append(token)
    return shlex.join(result)


@dataclass(frozen=True)
class _TestSelection:
    # all 是已知全量；unknown 是识别到入口但不能解释选择，二者都不授予目标证明。
    kind: str
    targets: tuple[str, ...] = ()
    focused: bool = False


_PYTEST_VALUE_OPTIONS = {
    "-k", "-m", "-c", "-o", "--override-ini", "--basetemp", "--confcutdir",
    "--ignore", "--ignore-glob", "--deselect", "--maxfail", "--rootdir", "--tb",
    "--timeout", "--junitxml", "--junit-xml", "--junit-prefix", "--capture",
    "--color", "--code-highlight", "--assert", "--import-mode", "--log-level",
    "--log-format", "--log-date-format", "--log-cli-level", "--log-cli-format",
    "--log-cli-date-format", "--log-file", "--log-file-level", "--log-file-format",
    "--log-file-date-format", "--durations", "--durations-min", "-W", "--pythonwarnings",
    "-p", "--html",
}
_PYTEST_FLAGS = {
    "-q", "--quiet", "-v", "--verbose", "-s", "-x", "--exitfirst", "--disable-warnings",
    "--disable-pytest-warnings", "--showlocals", "-l", "--full-trace", "--strict-config",
    "--strict-markers", "--continue-on-collection-errors", "--lf", "--last-failed",
    "--ff", "--failed-first", "--nf", "--new-first", "--cache-clear", "--stepwise",
    "--sw", "--stepwise-skip", "--sw-skip", "--show-capture=no", "--self-contained-html",
}
_NON_TEST_FLAGS = {"--help", "-h", "--version", "-V", "--collect-only", "--co", "--trace-config", "--fixtures", "--fixtures-per-test", "--markers", "--setup-plan"}


def _pytest_selection(args: list[str]) -> _TestSelection | None:
    targets = []
    positional = False
    index = 0
    filtered = False
    while index < len(args):
        token = args[index]
        index += 1
        option, separator, value = token.partition("=")
        if not positional and option in _NON_TEST_FLAGS:
            return None
        if not positional and token == "--":
            positional = True
            continue
        if not positional and option in _PYTEST_VALUE_OPTIONS:
            if not separator:
                if index >= len(args):
                    return _TestSelection("unknown")
                value = args[index]
                index += 1
            if not value:
                return _TestSelection("unknown")
            filtered |= option in {"-k", "-m"}
            continue
        if not positional and (token in _PYTEST_FLAGS or re.fullmatch(r"-[qv]+|-r[a-zA-Z]+", token)):
            filtered |= token in {"--lf", "--last-failed", "--ff", "--failed-first"}
            continue
        if not positional and token.startswith("-"):
            return _TestSelection("unknown")
        target = _normalize_test_target(token)
        if not target:
            return _TestSelection("unknown")
        targets.append(target)
    focused = filtered or bool(targets) and all("::" in target or target.split("::", 1)[0].endswith(".py") for target in targets)
    return _TestSelection("bounded" if targets or filtered else "all", tuple(dict.fromkeys(targets)), focused)


def _package_script_name(tokens: list[str]) -> str | None:
    if not tokens or Path(tokens[0]).name.lower() not in {"npm", "pnpm", "yarn"}:
        return None
    args = tokens[1:]
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option, separator, _value = args[index].partition("=")
        index += 1
        if not separator and option in {"--prefix", "--workspace", "-w", "--dir", "-C", "--cwd"}:
            index += 1
    if args[index:index + 1] == ["run"]:
        index += 1
    return args[index] if index < len(args) else None


def _segment_test_selection(tokens: list[str]) -> _TestSelection | None:
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    args = list(tokens[1:])
    if executable in {"pytest", "py.test", "pytest3"}:
        return _pytest_selection(args)
    entry = _python_entry(tokens)
    if entry is not None:
        runner, runner_args, module = entry
        if module and runner in {"pytest", "py.test"}:
            return _pytest_selection(runner_args)
        if module and runner == "unittest":
            if any(arg.split("=", 1)[0] in _NON_TEST_FLAGS for arg in runner_args):
                return None
            return _unittest_selection(runner_args)
        if runner.removeprefix("./") == "bin/test":
            selectors = native_runner_positional_selectors(runner_args, runner="bin/test")
            return _TestSelection("bounded" if selectors else "unknown", selectors, bool(selectors))
        if Path(runner).name == "runtests.py" or (Path(runner).name == "manage.py" and runner_args[:1] == ["test"]):
            return _TestSelection("unknown")
        return None
    if executable == "node" and args[:1] == ["--test"]:
        if not args[1:]:
            return _TestSelection("all")
        targets = tuple(_normalize_test_target(arg) for arg in args[1:])
        if any(arg.startswith("-") or any(c in arg for c in "*?[]$`;&|<>") or not target
               for arg, target in zip(args[1:], targets)):
            return _TestSelection("unknown")
        return _TestSelection("bounded", tuple(dict.fromkeys(targets)), True)
    if executable == "go" and args[:1] == ["test"]:
        for index, arg in enumerate(args[1:], 1):
            if arg == "-run" and index + 1 < len(args):
                return _TestSelection("bounded", focused=args[index + 1] != "^$")
            if arg.startswith("-run="):
                return _TestSelection("bounded", focused=arg[5:] != "^$")
        return _TestSelection("all")
    if _package_script_name(tokens) == "test":
        return _TestSelection("unknown")
    if (executable in {"jest", "vitest", "tox", "nox"}
            or executable == "cargo" and args[:1] == ["test"]
            or executable in {"mvn", "mvnw", "gradle", "gradlew", "make"} and "test" in args):
        return _TestSelection("unknown")
    # 未建模的 CLI 可能转交测试器；只读展示中的名称不属于执行。
    if executable not in {"echo", "printf", "rg", "grep", "egrep", "fgrep", "cat", "sed", "awk", "ls", "head", "tail"}:
        if any(Path(arg).name in {"pytest", "py.test", "jest", "vitest", "unittest"} for arg in args):
            return _TestSelection("unknown")
    return None


def _command_segments(command: str) -> list[list[str]]:
    from nz_coder.intelligence.verification_planner import verification_command_segments
    from nz_coder.runtime.verification.domain.identity import command_identity, observed_check_subject

    identity = command_identity(command or "", ".")
    subject = observed_check_subject(identity) if identity.opaque else None
    if subject is not None:
        command = shlex.join(subject.argv)
    return verification_command_segments(command or "")


def _test_selections(command: str) -> tuple[_TestSelection, ...]:
    selections = tuple(selection for words in _command_segments(command)
                       if (selection := _segment_test_selection(words)) is not None)
    if selections:
        return selections
    # 命令替换和 shell 消费管道不能从展示文本证明范围。
    if re.search(r"(?:\$\([^)]*\b(?:pytest|jest)\b|\b(?:echo|printf)\s+[^|]*\b(?:pytest|jest)\b[^|]*\|\s*(?:sh|xargs)\b)", command or ""):
        return (_TestSelection("unknown"),)
    return ()


def is_exact_test_command(command: str) -> bool:
    """只作调度观察：每个测试片段都具有已解释的聚焦选择。"""
    selections = _test_selections(command)
    return bool(selections) and all(item.kind == "bounded" and item.focused for item in selections)


def is_broad_test_command(command: str) -> bool:
    """只作调度观察；未知选择不具有执行拒绝或权限例外的含义。"""
    return any(item.kind != "bounded" or not item.focused for item in _test_selections(command))


def test_command_targets(command: str) -> tuple[str, ...]:
    """所有测试片段均有完整目标时才返回；保留 node id 和完整 unittest 选择器。"""
    selections = _test_selections(command)
    if not selections or any(item.kind != "bounded" or not item.targets for item in selections):
        return ()
    return tuple(dict.fromkeys(target for item in selections for target in item.targets))


_DECLARATION_EXCLUSION_RE = re.compile(
    r"\b(?:do\s+not|don't|must\s+not|never|previously|historical|formerly|example|quoted)\b"
    r"|\balready\s+(?:ran|executed)\b|\bwithout\s+(?:running|executing)\b"
    r"|\bexplain\s+(?:the\s+)?command\b|不要|不得|无需|不需要|历史|以前|之前|示例|例如|仅供参考|引用",
    re.IGNORECASE,
)
_TEST_COMMAND_START_RE = re.compile(
    r"(?<![\w/.-])(?:(?:/[^\s`]+/)?(?:python|pypy)(?:\d+(?:\.\d+)*)?\s+|pytest\b|py\.test\b|node\s+--test\b)"
)


_PLAIN_TEST_INSTRUCTION_RE = re.compile(
    r"(?P<english>(?:^[ \t]*(?:(?:[-*]|\d+[.)])[ \t]+)?|\.[ \t]+|[!?。；！？][ \t]*|[ \t]+and[ \t]+)(?:please\s+)?(?:run|execute)\s+"
    r"(?:(?:the\s+)?(?:exact\s+)?(?:acceptance\s+)?(?:command|check)\s+)?)"
    r"|(?P<chinese>(?:运行|执行)\s*|(?:验收|验证)命令\s*[:：]?\s*)",
    re.IGNORECASE,
)


def _positive_command_context(text: str, start: int, end: int) -> bool:
    prefix = re.split(r"[\n。；;!?！？]|\.(?=\s|$)", text[:start])[-1]
    suffix = re.split(r"[\n。；;!?！？]|\.(?=\s|$)", text[end:])[0]
    if _DECLARATION_EXCLUSION_RE.search(prefix + " " + suffix):
        return False
    # 标题只约束紧邻引用；新的明确指令或空行结束该语境。
    line_start = text.rfind("\n", 0, start) + 1
    local_prefix = text[line_start:start].strip()
    if not local_prefix or local_prefix.startswith("```"):
        for line in reversed(text[:max(0, line_start - 1)].split("\n")):
            previous = line.strip()
            if not previous:
                break
            if previous.endswith((":", "：")):
                return _DECLARATION_EXCLUSION_RE.search(previous) is None
            if not previous.startswith("`") and not _TEST_COMMAND_START_RE.match(previous):
                break
    return True


def _plain_declared_command(raw: str) -> str:
    """自然语言分隔符只在引号和转义之外生效，不能改写真实参数。"""
    quote = ""
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        # 独立的目录参数 . 仍属于命令；文件名后的句号才结束裸声明。
        sentence_dot = (char == "." and (index + 1 == len(raw) or raw[index + 1].isspace())
                        and (index == 0 or not raw[index - 1].isspace()))
        if char in "\n`。；，,!?！？" or sentence_dot:
            return raw[:index].strip()
        if (char.isspace() and re.match(r"\s+(?:and|when|then)\b", raw[index:], re.IGNORECASE)
                or raw.startswith(("并且", "并"), index)):
            return raw[:index].strip()
    return raw.strip()


def _declared_command_spans(text: str) -> tuple[tuple[int, str], ...]:
    """保留原位置，代码块只处理一次，避免其内部裸命令逃逸否定标题。"""
    value = str(text or "")
    fences = list(re.finditer(r"^[ \t]*```[^\n]*\n(?P<body>.*?)^[ \t]*```[^\n]*", value, re.MULTILINE | re.DOTALL))
    candidates: list[tuple[int, str]] = []
    masked = list(value)
    for fence in fences:
        if _positive_command_context(value, fence.start(), fence.end()):
            for position, command in _declared_command_spans(fence["body"]):
                candidates.append((fence.start("body") + position, command))
        for index in range(fence.start(), fence.end()):
            if masked[index] != "\n":
                masked[index] = " "
    scan = "".join(masked)
    quoted = list(re.finditer(r"`([^`\n]+)`", scan))
    shell_literals = list(re.finditer(r"(?<!\w)'[^'\n]*'|\"(?:\\.|[^\"\\\n])*\"", scan))
    for match in quoted:
        if (not any(span.start() <= match.start() < span.end() for span in shell_literals)
                and _positive_command_context(value, match.start(), match.end())):
            candidates.append((match.start(), match[1].strip()))
    for clause in re.finditer(r"[^\n]+", scan):
        instructions = list(_PLAIN_TEST_INSTRUCTION_RE.finditer(clause[0]))
        starts = [clause.start()] if _TEST_COMMAND_START_RE.match(clause[0].lstrip()) else []
        for match in instructions:
            absolute = clause.start() + match.start()
            if any(span.start() <= absolute < span.end() for span in (*quoted, *shell_literals)):
                continue
            prefix = clause[0][:match.start()].strip()
            coordinated = match[0].lstrip().lower().startswith("and ")
            if coordinated and starts:
                previous_command = value[starts[-1]:absolute].lstrip()
                if _TEST_COMMAND_START_RE.match(previous_command) is None:
                    continue
            local_prefix = re.split(r"[。；!?！？]|\.(?=\s)", prefix)[-1]
            if match.lastgroup == "chinese" and local_prefix and re.search(r"[A-Za-z_=;&|<>$]", local_prefix):
                continue
            starts.append(clause.start() + match.end())
        for start in starts:
            raw = value[start:clause.end()]
            start += len(raw) - len(raw.lstrip())
            raw = raw.lstrip()
            if any(span.start() <= start < span.end() for span in quoted):
                continue
            if _TEST_COMMAND_START_RE.match(raw) is None:
                continue
            # 分号仍属于完整 shell 命令，合同层不能把复合命令裁成单个测试。
            command = _plain_declared_command(raw)
            if _positive_command_context(value, start, start + len(command)):
                candidates.append((start, command))
    return tuple(sorted(candidates, key=lambda item: item[0]))


def _declared_test_commands(text: str) -> tuple[str, ...]:
    """共享声明边界：代码片段保留全命令；不截取 env 或复合命令中的子串。"""
    return tuple(dict.fromkeys(command for _position, command in _declared_command_spans(text)))


def _declared_discovery_scopes(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(command for candidate in _declared_test_commands(text)
                               if (command := canonical_unittest_discovery(candidate)) is not None))


def declared_test_scopes(text: str) -> tuple[str, ...]:
    """从正向完整声明提取已知测试目标；discover 仅保留集合命令身份。"""
    scopes = []
    for command in _declared_test_commands(text):
        for target in test_command_targets(command):
            if target not in scopes:
                scopes.append(target)
    return tuple(dict.fromkeys((*scopes, *_declared_discovery_scopes(text))))


def _normalize_test_target(value: str) -> str:
    target = str(value or "").replace("\\", "/")
    path, separator, nodes = target.partition("::")
    if (not path or path in {".", "./"} or path.startswith("/")
            or re.match(r"^[A-Za-z]:", path) or any(char in target for char in "\n\r\x00")
            or any(char in path for char in "$`;&|<>") or ".." in path.split("/")):
        return ""
    normalized = posixpath.normpath(path).removeprefix("./")
    return normalized + (separator + nodes if separator else "")


_ROUND_MUTATION_INTENT_RE = re.compile(
    r"(?:\b(?:add|change|complete|create|delete|document|edit|fix|implement|"
    r"migrate|modify|refactor|remove|rename|replace|support|update|write)\b|"
    r"新增|添加|创建|删除|文档化|编辑|修复|实现|完善|支持|补充|修改|改动|移除|重命名|迁移|替换|更新|编写|完成|写)",
    re.IGNORECASE,
)
_ROUND_NEGATED_MUTATION_RE = re.compile(
    r"(?:\b(?:do\s+not|don't|must\s+not|never|without)\s+"
    r"(?:change|changing|create|creating|delete|deleting|edit|editing|modify|"
    r"modifying|remove|removing|rename|renaming|replace|replacing|update|"
    r"updating|write|writing)\b|"
    r"(?:(?:不要|不得|无需|不需要|不)\s*"
    r"(?:新增|添加|创建|删除|编辑|修改|改动|移除|重命名|替换|更新|编写|写)))",
    re.IGNORECASE,
)
_TRACEBACK_EVIDENCE_RE = re.compile(
    r"\btraceback\b|\bstack\s+trace\b|(?:^|\s)调用栈(?:\s|$)",
    re.IGNORECASE,
)

_TASK_REFERENCE_SCOPE_RE = re.compile(
    r"\b(?:follow|according\s+to|based\s+on|described\s+in|specified\s+in|"
    r"requirements?\s+in|required\s+by)\b|按照|根据|依据|遵循", re.IGNORECASE,
)
_REFERENCE_SCOPE_RE = re.compile(
    r"\b(?:read|inspect|look\s+at|explain|summarize|consult|use|using|"
    r"refer\s+to|to\s+match|against)\b|参考|参照|读取|阅读|查看|解释|总结",
    re.IGNORECASE,
)
_VERIFY_SCOPE_RE = re.compile(
    r"\b(?:run|execute|validate|verify|check)\b|运行|执行|验证|校验"
    r"|(?:使|让|确保)(?=[^。！？;；\n]{0,160}通过)"
    r"|\b(?:make|ensure)\b(?=[^.!?;\n]{0,160}\bpass(?:es|ing)?\b)",
    re.IGNORECASE,
)
_NEGATED_REFERENCE_RE = re.compile(
    r"\b(?:do\s+not|don't|must\s+not|never|without)\s+"
    r"(?:follow|read|use|consult|inspect)\b|(?:不要|不得|无需|不需要|不)\s*(?:遵循|按照|根据|读取|阅读)",
    re.IGNORECASE,
)
_SCOPE_MARKERS = re.compile(
    "|".join(f"(?P<{name}>{pattern.pattern})" for name, pattern in (
        ("negated_reference", _NEGATED_REFERENCE_RE),
        ("negated", _ROUND_NEGATED_MUTATION_RE),
        ("task_reference", _TASK_REFERENCE_SCOPE_RE),
        ("context", _REFERENCE_SCOPE_RE),
        ("verification", _VERIFY_SCOPE_RE),
        ("mutation", _ROUND_MUTATION_INTENT_RE),
    )), re.IGNORECASE,
)
_INSTRUCTION_PATH_RE = re.compile(
    r"(?<![\w/.:~-])([\w./-]+\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|rb|php|c|cc|cpp|h|hpp|md|rst|txt|json|ya?ml))(?![\w-]|\.[A-Za-z0-9_])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InstructionPathRole:
    """One explicit path occurrence and the instruction governing it."""

    path: str
    role: str
    authority: str = ""
    operation: str = ""


def normalize_instruction_path(value: str) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (not normalized or len(normalized) > 1024 or normalized.startswith("/")
            or ":" in normalized or ".." in normalized.split("/")
            or any(ord(c) < 32 for c in normalized)):
        return ""
    return normalized


def explicit_instruction_paths(text: str, limit: int | None = 20) -> list[str]:
    if not isinstance(text, str) or (limit is not None and limit <= 0):
        return []
    paths = []
    for match in _INSTRUCTION_PATH_RE.finditer(text.replace("\\", "/")):
        path = normalize_instruction_path(match[1])
        if path and path not in paths:
            paths.append(path)
            if limit is not None and len(paths) >= limit:
                break
    return paths


def _instruction_scopes(text: str):
    """Single scope authority for mutation, named specs, verification and context."""
    for clause in re.split(
        r"(?:[!?。！？;；]|\.(?=\s|$)|\bbut\b|\bhowever\b)\s*|\n+",
        str(text or ""), flags=re.IGNORECASE,
    ):
        masked = re.sub(r"[\w./-]+\.[A-Za-z][A-Za-z0-9]*",
                        lambda m: " " * len(m.group()), clause)
        evidence = _TRACEBACK_EVIDENCE_RE.search(masked)
        if evidence:
            masked = masked[:evidence.start()]
        matches = list(_SCOPE_MARKERS.finditer(masked))
        prefix_end = matches[0].start() if matches else len(masked)
        yield "context", clause[:prefix_end]
        index = 0
        while index < len(matches):
            match = matches[index]
            end = matches[index + 1].start() if index + 1 < len(matches) else len(masked)
            role = match.lastgroup
            if role in {"negated", "negated_reference"}:
                role = "context"
            scope = clause[match.start():end]
            # Suffix authority stays inside this same verb-governed scope.
            if (role == "context" and re.match(r"use\b", scope, re.IGNORECASE)
                    and re.search(r"\bas\s+(?:the\s+)?(?:requirements?|task\s+specification)\b", scope, re.IGNORECASE)):
                role = "task_reference"
            # “阅读文档后完成其中要求”授予只读规范身份；不扩展普通阅读或否定句。
            if role == "context" and re.match(r"(?:读取|阅读)", scope):
                documents = list(_INSTRUCTION_PATH_RE.finditer(scope))
                if len(documents) == 1 and is_documentation_file(documents[0].group(1)):
                    suffix = clause[match.start() + documents[0].end():]
                    if re.match(r"^[`\"'\s,，]*(?:后|并且|然后|并|再)?\s*(?:从零)?(?:完成|实现|开发)\s*其中(?:的)?", suffix):
                        role = "task_reference"
            reference_scopes = []
            next_index = index + 1
            if role == "mutation":
                while next_index < len(matches):
                    reference = matches[next_index]
                    if (reference.lastgroup != "task_reference"
                            or not re.fullmatch(r"required\s+by", reference.group(), re.IGNORECASE)):
                        break
                    reference_end = matches[next_index + 1].start() if next_index + 1 < len(matches) else len(masked)
                    reference_scope = clause[reference.start():reference_end]
                    colon = re.search(r":(?!\s*\d)", reference_scope)
                    separator = colon.start() if colon else -1
                    if separator < 0:
                        comma = reference_scope.find(",")
                        list_continues = re.search(
                            r"\band\s*,\s*(?:as\s+)?$",
                            clause[match.end():reference.start()], re.IGNORECASE,
                        )
                        rename_continues = (
                            re.match(r"rename\b|重命名", scope, re.IGNORECASE)
                            and re.match(r"\s*to\b", reference_scope[comma + 1:], re.IGNORECASE)
                        )
                        if comma >= 0 and (list_continues or rename_continues):
                            separator = comma
                    if separator < 0 or not explicit_instruction_paths(reference_scope[:separator], limit=None):
                        break
                    # 行号冒号和多来源逗号不结束引用；明确输出列表继续属于原修改动词。
                    # 拼回同一作用域，保留跨限定语的 rename/create/delete 语义。
                    reference_scopes.append(reference_scope[:separator])
                    scope += " " + reference_scope[separator + 1:]
                    next_index += 1
            yield role, scope
            for reference_scope in reference_scopes:
                yield "task_reference", reference_scope
            index = next_index


def mutation_instruction_scopes(text: str) -> tuple[str, ...]:
    return tuple(scope for role, scope in _instruction_scopes(text) if role == "mutation")


def explicit_read_only_paths(text: str) -> tuple[str, ...]:
    """保留明确禁改的文件身份，避免后续合同再次从普通提及推断写入。"""
    return tuple(dict.fromkeys(
        path for _role, scope in _instruction_scopes(text)
        if _ROUND_NEGATED_MUTATION_RE.match(scope)
        for path in explicit_instruction_paths(scope, limit=None)
    ))


def classify_instruction_paths(text: str) -> tuple[InstructionPathRole, ...]:
    """Preserve distinct roles when the user both changes and verifies a file."""
    result = []
    for role, scope in _instruction_scopes(text):
        paths = explicit_instruction_paths(scope, limit=None)
        operation = "change" if role == "mutation" else ""
        if role == "mutation":
            if re.match(r"create\b|新建|创建|新增", scope, re.IGNORECASE):
                operation = "create"
            elif re.match(r"(?:delete|remove)\b|删除|移除", scope, re.IGNORECASE):
                operation = "delete"
        rename = role == "mutation" and re.match(r"rename\b|重命名", scope, re.IGNORECASE) and len(paths) == 2
        for index, path in enumerate(paths):
            item = InstructionPathRole(
                path, role, "task_spec" if role == "task_reference" else "",
                ("delete", "create")[index] if rename else operation,
            )
            if item not in result:
                result.append(item)
    readonly = set(explicit_read_only_paths(text))
    immutable_tests = task_forbids_test_changes(text)
    normalized = []
    for item in result:
        if item.role == "mutation" and (item.path in readonly or (immutable_tests and is_test_file(item.path))):
            item = InstructionPathRole(item.path, "context")
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def extract_task_reference_paths(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(r.path for r in classify_instruction_paths(text)
                               if r.role == "task_reference"))



def successful_mutation_operations(name: str, payload: dict, output: str) -> dict[str, str]:
    """Refine settled file-tool writes using their operation/output contract.

    Called only after actual successful dispatch, not on model-supplied evidence.
    A non-overwriting create and a delete are enforced by the filesystem tool.
    An overwriting patch alone cannot prove the target was newly created.
    """
    operations = {}
    if name == "apply_patch":
        for change in payload.get("changes") or []:
            if not isinstance(change, dict):
                continue
            path = normalize_path(str(change.get("path") or "")).removeprefix("./")
            op = change.get("op", "replace")
            if op == "delete":
                operations[path] = "delete"
            elif op == "create" and not change.get("overwrite"):
                operations[path] = "create"
            elif path not in operations:
                operations[path] = "change"
    elif name == "write_file":
        path = normalize_path(str(payload.get("path") or "")).removeprefix("./")
        if tool_output_reports_created_path(output, path):
            operations[path] = "create"
    elif name == "write_files_batch":
        created = re.search(r"^Created: \d+\n(.*?)^Updated:", output, re.MULTILINE | re.DOTALL)
        if created:
            operations.update({normalize_path(line[2:]).removeprefix("./"): "create"
                               for line in created[1].splitlines() if line.startswith("- ")})
    return operations


def extract_explicit_mutation_operations(text: str, limit: int = 20) -> dict[str, str]:
    operations: dict[str, str] = {}
    for item in classify_instruction_paths(text):
        if item.role == "mutation" and (item.path in operations or len(operations) < limit):
            operations[item.path] = item.operation
    return operations
