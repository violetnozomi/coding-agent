"""保留参数顺序和绑定的检查身份；展示归一化不参与验收。"""
from __future__ import annotations

from pathlib import Path
import re
import shlex

from .models import CommandIdentity


def _opaque_shell(command: str) -> bool:
    quote = ""
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char in "$`":
                return True
        elif char in "'\"":
            quote = char
        elif char in ";&|><\n$`":
            return True
    return bool(quote or escaped)


def command_identity(command: str, cwd: str) -> CommandIdentity:
    original = str(command).strip()
    directory = str(Path(cwd).resolve())
    if _opaque_shell(original):
        return CommandIdentity((original,), directory, opaque=True, original=original)
    try:
        words = shlex.split(original)
    except ValueError:
        return CommandIdentity((original,), directory, opaque=True, original=original)
    environment: dict[str, str] = {}
    if words[:1] == ["env"]:
        words = words[1:]
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        key, value = words.pop(0).split("=", 1)
        environment[key] = value
    return CommandIdentity(tuple(words), directory, tuple(sorted(environment.items())), original=original)


def _framework_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    unittest_prefix: tuple[str, ...] = ()
    if (len(argv) >= 3 and argv[1:3] == ("-m", "unittest")
            and re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", Path(argv[0]).name)):
        if argv[0] not in {"python", "python3"}:
            # 新识别的绝对入口和 PyPy 没有解释器环境等价证明，保留原入口身份。
            unittest_prefix = argv[:2]
        argv = argv[2:]
    elif len(argv) >= 3 and argv[0] in {"python", "python3"}:
        if argv[1] == "-m" and argv[2] in {"pytest", "unittest"}:
            argv = argv[2:]
        elif argv[1:3] == ("-m", "py_compile"):
            return ("python", *argv[1:])
    if argv[:1] == ("py.test",):
        argv = ("pytest", *argv[1:])
    if argv[:1] == ("unittest",):
        # 展示开关不改变选择集；选项值、发现目录与终止选项后的参数完整保留。
        values = {"-k", "--durations", "-s", "--start-directory", "-p", "--pattern",
                  "-t", "--top-level-directory"}
        flags = {"-f", "--failfast", "-c", "--catch", "-b", "--buffer", "--locals"}
        result, value_expected, positional = [], False, False
        for word in argv:
            if value_expected:
                result.append(word)
                value_expected = False
            elif positional:
                result.append(word)
            elif word == "--":
                positional = True
                result.append(word)
            elif word in values:
                value_expected = True
                result.append(word)
            elif word in {"-v", "--verbose", "-q", "--quiet"}:
                continue
            elif word.startswith("-") and word not in flags and "=" not in word and not word.startswith("-k"):
                return (*unittest_prefix, *argv)
            else:
                result.append(word)
        return (*unittest_prefix, *result)
    if argv[:1] == ("pytest",):
        # 仅删除不取参数的展示开关，选择器和选项值的顺序仍完整保留。
        values = {"-k", "-m", "--ignore", "--ignore-glob", "--confcutdir", "--rootdir", "-c",
                  "--override-ini", "-o", "--deselect", "--maxfail", "--import-mode"}
        result, value_expected, positional = [], False, False
        for word in argv:
            if value_expected:
                result.append(word)
                value_expected = False
            elif positional:
                result.append(word)
            elif word == "--":
                positional = True
                result.append(word)
            elif word in values:
                value_expected = True
                result.append(word)
            elif word in {"-q", "--quiet", "-v", "--verbose", "-s"}:
                continue
            elif word.startswith("-") and "=" not in word:
                return argv
            else:
                result.append(word)
        return tuple(result)
    return argv


def _django_selection(argv: tuple[str, ...]) -> tuple[bool, tuple[str, ...]]:
    argv = _framework_argv(argv)
    native = len(argv) >= 2 and bool(re.fullmatch(r"python(?:3(?:\.\d+)?)?", argv[0])) and argv[1].removeprefix("./") == "tests/runtests.py"
    if native:
        args = argv[2:]
    elif argv[:1] == ("pytest",):
        args = argv[1:]
    else:
        return False, ()
    selectors = []
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg in {"-q", "--quiet", "-v", "--verbose", "-s"} and not native:
            continue
        if native and re.fullmatch(r"-v\d+", arg):
            continue
        if native and arg in {"--parallel", "--verbosity", "-v"}:
            if index >= len(args) or not args[index].isdigit():
                return native, ()
            index += 1
            continue
        if native and re.fullmatch(r"--(?:parallel|verbosity)=\d+", arg):
            continue
        if arg.startswith("-"):
            return native, ()
        if not native:
            path, *nodes = arg.removeprefix("./").split("::")
            if not path.startswith("tests/") or not path.endswith(".py"):
                return native, ()
            arg = ".".join((path[6:-3].replace("/", "."), *nodes))
        selectors.append(arg)
    return native, tuple(selectors)


def _unquoted_operator(command: str, operator: str) -> int:
    quote, escaped = "", False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif command.startswith(operator, index):
            return index
    return -1


def _same_cwd_body(command: str, cwd: str) -> str | None:
    split = _unquoted_operator(command, "&&")
    if split < 0 or any(char in command[:split] for char in "\n\r"):
        return None
    try:
        words = shlex.split(command[:split])
    except ValueError:
        return None
    if words[:2] == ["cd", "--"]:
        words = ["cd", *words[2:]]
    if len(words) != 2 or words[0] != "cd":
        return None
    target = words[1]
    # 仅接受字面量同目录，不能把 shell 展开后的另一个目录误认成本目录。
    if any(char in target for char in "$`*?[]{}~\n\r;&|<>()"):
        return None
    if target != "." and (not Path(target).is_absolute() or target.rstrip("/") != cwd.rstrip("/")):
        return None
    return command[split + 2:].strip()


def display_check_body(identity: CommandIdentity) -> str | None:
    """只去掉纯 && 连接的字面展示尾段；保留检查自身的原文。"""
    from nz_coder.intelligence.verification_planner import _shell_parts

    command = _same_cwd_body(identity.original, identity.cwd) or identity.original
    split = _unquoted_operator(command, "&&")
    if split < 0:
        return None
    segments, operators = _shell_parts(command[split + 2:])
    if not segments or len(segments) != len(operators) + 1 or any(op != "&&" for op in operators):
        return None
    for _words, source in segments:
        tail = command_identity(source, identity.cwd)
        # 不消去求值、文件重定向或任意程序；printf 的失败仍属于整条命令。
        if tail.opaque or tail.environment or not tail.argv or tail.argv[0] not in {"echo", "printf"}:
            return None
    return command[:split].strip() or None


def _output_redirections(command: str) -> tuple[str, bool] | None:
    """解释单一命令末尾的字面输出重定向；不消费输入、求值或后续命令。"""
    start = _unquoted_operator(command, ">")
    if start < 0:
        return None
    # 紧邻操作符的独立1/2才是描述符，不能删除普通位置参数的数字。
    descriptor = re.search(r"(?<!\S)[12]$", command[:start])
    if descriptor is not None:
        start = descriptor.start()
    prefix = command[:start].strip()
    if not prefix:
        return None
    try:
        lexer = shlex.shlex(command[start:], posix=True, punctuation_chars=">&|;<()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return None
    index, to_file = 0, False
    while index < len(words):
        if words[index] in {"1", "2"}:
            index += 1
        if index >= len(words) or words[index] not in {">", ">>", ">&"}:
            return None
        operation = words[index]
        index += 1
        if index >= len(words):
            return None
        target = words[index]
        index += 1
        if operation == ">&":
            if target not in {"1", "2"}:
                return None
        else:
            if not target or any(char in target for char in "$`*?[]{};&|<>\r\n"):
                return None
            to_file = True
    return prefix, to_file


def check_output_redirected(identity: CommandIdentity) -> bool:
    """文件中的正文没有被宿主捕获，不能把检查的正面输出声明为完整。"""
    command = display_check_body(identity) or _same_cwd_body(identity.original, identity.cwd) or identity.original
    split = _unquoted_operator(command, "|")
    prefix = command[:split] if split >= 0 else command
    redirected = _output_redirections(prefix)
    return bool(redirected and redirected[1])


def observed_check_subject(identity: CommandIdentity) -> CommandIdentity | None:
    """有限包装只放宽复测身份，不改写原命令或执行可信度。"""
    if not identity.opaque or command_identity(identity.original, identity.cwd) != identity:
        return None
    body = display_check_body(identity) or _same_cwd_body(identity.original, identity.cwd)
    command = body if body is not None else identity.original
    split = _unquoted_operator(command, "|")
    if split < 0:
        redirected = _output_redirections(command)
        prefix = redirected[0] if redirected is not None else command.strip()
        if body is None and prefix == command:
            return None
    else:
        prefix = command[:split].strip()
        redirected = _output_redirections(prefix)
        prefix = redirected[0] if redirected is not None else prefix
        suffix = command[split:]
        if not (re.fullmatch(r"\|\|\s*true\s*", suffix)
                or re.fullmatch(r"\|\s*(?:(?:tail|head)(?:\s+(?:-\d+|-[nc]\s+[+]?[0-9]+))*|cat)(?:\s*\|\|\s*true)?\s*", suffix)):
            return None
    subject = command_identity(prefix, identity.cwd)
    return subject if subject.argv and not subject.opaque else None


def verified_check_subject(identity: CommandIdentity) -> CommandIdentity | None:
    """仅识别不改变检查范围的显示包装；成功可信度另由宿主回执证明。"""
    if not identity.opaque or command_identity(identity.original, identity.cwd) != identity:
        return None
    if check_output_redirected(identity):
        return None
    command = display_check_body(identity) or _same_cwd_body(identity.original, identity.cwd) or identity.original
    split = _unquoted_operator(command, "|")
    if split >= 0:
        if not re.fullmatch(r"\|\s*(?:(?:tail|head)(?:\s+(?:-\d+|-[nc]\s+[+]?[0-9]+))*|cat)\s*", command[split:]):
            return None
        prefix = command[:split]
    else:
        prefix = command
    prefix = re.sub(r"\s+2>&1\s*$", "", prefix).strip()
    subject = command_identity(prefix, identity.cwd)
    if subject.opaque or not subject.argv:
        return None
    if split < 0:
        # 单独合并 stderr 不改变任意字面检查入口；自动执行仍由调度器检查真实入口。
        return subject
    executable = Path(subject.argv[0]).name
    python = bool(re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable))
    native = executable in {"pytest", "pytest3", "py.test"} or (
        python and subject.argv[1:3] in {("-m", "pytest"), ("-m", "unittest"), ("-m", "py_compile")})
    return subject if native else None


def commands_equivalent(
    first: CommandIdentity, second: CommandIdentity, *, native_runner_kind: str = "",
) -> bool:
    if native_runner_kind == "observed_output":
        left = observed_check_subject(first) if first.opaque else first
        right = observed_check_subject(second) if second.opaque else second
        return bool(left and right and commands_equivalent(left, right))
    if first.cwd != second.cwd or first.environment != second.environment:
        return False
    if first.opaque or second.opaque:
        return first == second
    if _framework_argv(first.argv) == _framework_argv(second.argv):
        return True
    if native_runner_kind == "django":
        first_native, first_selection = _django_selection(first.argv)
        second_native, second_selection = _django_selection(second.argv)
        return bool(first_native or second_native) and bool(first_selection) and first_selection == second_selection
    return False
