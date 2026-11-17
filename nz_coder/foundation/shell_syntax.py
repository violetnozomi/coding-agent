"""有限 Shell 结构与内联程序用途；只分析，不执行或改写用户命令。"""
from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from itertools import product
from pathlib import PurePosixPath
import re
import shlex
from urllib.parse import unquote, urlsplit


class ShellSyntaxError(ValueError):
    """The limited admission grammar cannot determine this input's roles."""

    def __init__(self, message: str, *, kind: str = "unsupported"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ShellToken:
    value: str
    dynamic: bool = False
    operator: bool = False
    quoted: bool = False
    heredoc: str | None = None
    expansions: frozenset[str] = frozenset()
    substitutions: tuple[str, ...] = ()

    @property
    def parameter_only(self) -> bool:
        return self.dynamic and self.expansions == {"parameter"}


@dataclass(frozen=True)
class ShellCommand:
    words: tuple[ShellToken, ...]
    redirects: tuple[tuple[str, ShellToken], ...] = ()
    connector: str = ";"
    opaque: bool = False


def shell_tokens(command: str, *, _depth: int = 0) -> list[ShellToken]:
    if _depth > 8:
        raise ShellSyntaxError("shell analysis nesting limit")
    result: list[ShellToken] = []
    word, quote = "", ""
    started = dynamic = quoted = False
    expansions: set[str] = set()
    substitutions: list[str] = []
    pending: list[tuple[int, bool]] = []
    i = 0

    def flush():
        nonlocal word, started, dynamic, quoted, expansions, substitutions
        if started:
            token = ShellToken(word, dynamic, quoted=quoted, expansions=frozenset(expansions),
                               substitutions=tuple(substitutions))
            if result and result[-1].operator and re.fullmatch(r'\d*<<-?', result[-1].value):
                if dynamic or not word:
                    raise ShellSyntaxError("dynamic heredoc delimiter")
                pending.append((len(result), result[-1].value.endswith("<<-")))
            result.append(token)
        word, started, dynamic, quoted = "", False, False, False
        expansions = set()
        substitutions = []

    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'":
            if i + 1 >= len(command):
                raise ShellSyntaxError("unfinished escape", kind="invalid")
            nxt = command[i + 1]
            if nxt == "\n":
                i += 2
                continue
            if quote == '"' and nxt not in '$`"\\':
                word += "\\"
            word += nxt
            started = quoted = True
            i += 2
            continue
        if (quote != "'" and (command.startswith('$(', i) or ch == '`')
                or not quote and command.startswith(('<(', '>('), i)):
            # 只保留替换表达式的边界；内部代码由授权链决定，不能当作普通变量。
            end = _substitution_end(command, i)
            expression = command[i:end]
            word += expression
            substitutions.extend(_substitution_sources(expression, _depth))
            started = dynamic = True
            expansions.add("substitution")
            i = end
            continue
        if quote != "'" and ch == '$':
            parameter = re.match(r'\$(?:[A-Za-z_]\w*|[?#!*@0-9-]|\{[A-Za-z_]\w*\})', command[i:])
            value = parameter[0] if parameter else '$'
            word += value
            started = dynamic = True
            expansions.add("parameter" if parameter else "unknown")
            i += len(value)
            continue
        if quote:
            if ch == quote:
                quote = ""
            else:
                word += ch
            i += 1
            continue
        if ch in "\"'":
            quote, started, quoted = ch, True, True
        elif ch in " \t\r":
            flush()
        elif ch == "#" and not started:
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        elif ch in "\n;|&<>()":
            prefix = word if started and word.isdigit() and ch in "<>" else ""
            if prefix:
                word, started = "", False
            flush()
            match = re.match(r'(?:<<-|>>|<<|<>|>&|<&|&&|\|\||;;&|;;|;&|\(\(|[\n;|&<>()])', command[i:])
            assert match
            result.append(ShellToken(prefix + match[0], operator=True))
            i += len(match[0])
            if ch == "\n" and pending:
                for index, strip_tabs in pending:
                    delimiter = result[index]
                    lines = []
                    while i < len(command):
                        end = command.find("\n", i)
                        if end < 0:
                            end = len(command)
                        line = command[i:end]
                        i = min(end + 1, len(command))
                        checked = line.lstrip("\t") if strip_tabs else line
                        if checked == delimiter.value:
                            break
                        lines.append(checked + ("\n" if end < len(command) else ""))
                    else:
                        raise ShellSyntaxError("unfinished heredoc", kind="invalid")
                    body = "".join(lines)
                    expanding = not delimiter.quoted and bool(re.search(r"[$`]", body))
                    result[index] = replace(delimiter, heredoc=body, dynamic=expanding,
                        expansions=frozenset({"heredoc"}) if expanding else frozenset(),
                        substitutions=_heredoc_substitutions(body, _depth) if expanding else ())
                pending.clear()
            continue
        else:
            word += ch
            started = True
            if ch in '*?[':
                dynamic = True
                expansions.add("glob")
            elif ch in '{}~':
                dynamic = True
                expansions.add("other")
        i += 1
    if quote:
        raise ShellSyntaxError("unfinished quote", kind="invalid")
    flush()
    if pending:
        raise ShellSyntaxError("unfinished heredoc", kind="invalid")
    return result


def _substitution_end(command: str, start: int) -> int:
    backtick = command[start] == '`'
    depth, quote = 1, ''
    i = start + (1 if backtick else 2)
    while i < len(command):
        ch = command[i]
        if ch == '\\' and quote != "'":
            i += 2
            continue
        if backtick and ch == '`':
            return i + 1
        if quote:
            if ch == quote:
                quote = ''
        elif ch in "\"'":
            quote = ch
        elif not backtick:
            depth += (ch == '(') - (ch == ')')
            if depth == 0:
                return i + 1
        i += 1
    raise ShellSyntaxError("unfinished substitution", kind="invalid")


def _substitution_sources(expression: str, depth: int) -> tuple[str, ...]:
    if expression.startswith('$(('):
        # 算术正文不是 shell 命令；其中真正的命令替换仍然会执行。
        return tuple(source for token in shell_tokens(expression[3:-2], _depth=depth + 1)
                     for source in token.substitutions)
    return (expression[1:-1] if expression.startswith('`') else expression[2:-1],)


def _heredoc_substitutions(body: str, depth: int) -> tuple[str, ...]:
    # 未引用 here-document 中的引号是数据，只有转义会屏蔽命令替换。
    sources = []
    i = 0
    while i < len(body):
        if body[i] == '\\' and i + 1 < len(body) and body[i + 1] in '$`\\\n':
            i += 2
        elif body.startswith('$(', i) or body[i] == '`':
            end = _substitution_end(body, i)
            sources.extend(_substitution_sources(body[i:end], depth))
            i = end
        else:
            i += 1
    return tuple(sources)


def shell_commands(command: str, *, partial: bool = False) -> tuple[ShellCommand, ...]:
    tokens = shell_tokens(command)
    commands = []
    words, redirects = [], []
    connector = ";"
    opaque_segment = False
    groups: list[bool] = []
    case_phases: list[str] = []
    prefixes = {'if', 'elif', 'while', 'until', 'then', 'else', 'do', '{', '!'}
    i = 0

    def flush():
        nonlocal opaque_segment
        opaque_segment |= False in groups
        opaque_segment |= bool(case_phases and case_phases[-1] != 'body')
        if opaque_segment and not words and not redirects:
            commands.append(ShellCommand((), connector=connector, opaque=True))
        if words or redirects:
            if not words:
                if not partial:
                    raise ShellSyntaxError("redirection-only command")
                opaque_segment = True
            pending = list(words)
            control = prefixes | {'fi', 'for', 'select', 'case', 'esac', 'function', 'done', '}'}
            if pending and not pending[0].quoted and pending[0].value in control:
                if not partial:
                    raise ShellSyntaxError("compound shell control flow")
                # 控制词仅标记分析不完整；可确认的条件/循环体入口仍保留事实。
                commands.append(ShellCommand((), connector=connector, opaque=True))
                while pending and not pending[0].quoted and pending[0].value in prefixes:
                    pending.pop(0)
                if pending and not pending[0].quoted and pending[0].value in control:
                    opaque_segment = True
            if pending or redirects:
                commands.append(ShellCommand(tuple(pending), tuple(redirects), connector, opaque_segment))
            words.clear()
            redirects.clear()
        opaque_segment = False

    while i < len(tokens):
        token = tokens[i]
        if token.operator and token.value in {";", "\n", "&&", "||", "|", "&"}:
            flush()
            connector = token.value
        elif token.operator and token.value in {';;', ';&', ';;&'}:
            if not partial:
                raise ShellSyntaxError("compound shell control flow")
            flush()
            if case_phases:
                case_phases[-1] = 'pattern'
            else:
                opaque_segment = True
            connector = ';'
        elif token.operator:
            if not re.fullmatch(r"\d*(?:>|>>|<|>&|<&|<<|<<-)", token.value):
                if not partial:
                    raise ShellSyntaxError("unsupported shell operator")
                if case_phases and case_phases[-1] in {'pattern', 'opaque_pattern'}:
                    # 模式到右括号为止都是数据；只保留能确定边界的普通 case arm。
                    if token.value == ')' and case_phases[-1] == 'pattern':
                        flush()
                        case_phases[-1] = 'body'
                    elif token.value != '(' or words:
                        case_phases[-1] = 'opaque_pattern'
                        opaque_segment = True
                elif token.value in {'(', '(('}:
                    plain = (token.value == '(' and False not in groups
                             and not redirects and all(not word.quoted and word.value in prefixes for word in words))
                    if plain:
                        flush()
                        commands.append(ShellCommand((token,), connector=connector, opaque=True))
                    else:
                        opaque_segment = True
                    groups.extend([plain] * len(token.value))
                elif token.value == ')' and groups:
                    if groups[-1]:
                        flush()
                        commands.append(ShellCommand((token,), connector=connector, opaque=True))
                    else:
                        opaque_segment = True
                    groups.pop()
                else:
                    # case 模式、函数头和算术名称不能冒充执行入口。
                    opaque_segment = True
                i += 1
                continue
            i += 1
            if i >= len(tokens) or tokens[i].operator:
                raise ShellSyntaxError("missing redirection target", kind="invalid")
            redirects.append((token.value, tokens[i]))
        else:
            if (False not in groups and not token.quoted
                    and all(not word.quoted and word.value in prefixes for word in words)):
                if token.value == 'case' and (not case_phases or case_phases[-1] == 'body'):
                    flush()
                    case_phases.append('header')
                elif token.value == 'esac' and case_phases:
                    flush()
                    case_phases.pop()
            words.append(token)
            if (case_phases and case_phases[-1] == 'header' and not token.quoted
                    and token.value == 'in' and len(words) >= 3):
                flush()
                case_phases[-1] = 'pattern'
        i += 1
    flush()
    if groups:
        raise ShellSyntaxError("unfinished shell grouping", kind="invalid")
    return tuple(commands)


def executable_words(command: ShellCommand) -> tuple[ShellToken, ...]:
    """剥离明确包装以识别真实入口，不给包装授予只读身份。"""
    words = list(command.words)
    while words and re.match(r"^[A-Za-z_]\w*=", words[0].value):
        words.pop(0)
    if words and PurePosixPath(words[0].value).name == "env":
        words.pop(0)
        while words and (re.match(r"^[A-Za-z_]\w*=", words[0].value) or words[0].value in {"-i", "--ignore-environment", "--"}):
            words.pop(0)
    while words and words[0].value in {"command", "exec"}:
        words.pop(0)
        if words and words[0].value == "--":
            words.pop(0)
    return tuple(words)


def substitution_commands(command: ShellCommand) -> tuple[str, ...]:
    """仅词法上确实展开的替换体是代码；单引号与转义内容保持数据。"""
    return tuple(source for token in (*command.words, *(token for _, token in command.redirects))
                 for source in token.substitutions)


def inline_source(command: ShellCommand) -> tuple[str, str] | None:
    words = executable_words(command)
    if not words:
        return None
    name = PurePosixPath(words[0].value).name
    if name in {"node", "nodejs"}:
        invocation = _node_invocation(words)
        if invocation.source is not None:
            return "opaque", invocation.source
        return _stdin_source(command, "opaque") if invocation.stdin else None
    python = bool(re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", name))
    shell = name in {"sh", "bash", "dash", "zsh"}
    opaque = name in {"node", "nodejs", "ruby", "perl"}
    if not python and not shell and not opaque:
        return None
    args = words[1:]
    i = 0
    stdin = True
    while i < len(args):
        arg = args[i]
        value = arg.value
        source_option = value == "-c" or shell and bool(re.fullmatch(r'-[a-zA-Z]*c', value))
        source_option |= opaque and value in {"-e", "--eval", "-p", "--print"}
        if source_option:
            if i + 1 >= len(args) or args[i + 1].dynamic:
                raise ShellSyntaxError("dynamic or missing interpreter source")
            return ("python" if python else "shell" if shell else "opaque"), args[i + 1].value
        if python and value in {"-W", "-X", "--check-hash-based-pycs"}:
            i += 2
            continue
        if value == "-m":
            return None
        if value == "-" or shell and value.startswith("-") and 's' in value[1:]:
            break
        if value == "--" or not value.startswith("-"):
            stdin = False
            break
        if python and (value in {"--version", "--help"} or re.fullmatch(r'-[bBdEhiIOPqRsSuvVx]+', value)
                       or value.startswith(("-W", "-X"))):
            i += 1
            continue
        if shell and re.fullmatch(r'-[aefhkmnptuvxBCEHPT]+', value):
            i += 1
            continue
        raise ShellSyntaxError("unsupported interpreter option")
    body = None
    dynamic_body = False
    descriptor_input = False
    for operator, token in command.redirects:
        # Shell 从左到右应用重定向，最后一次 fd0 输入才是解释器读取的代码。
        if operator in {'<<', '<<-', '0<<', '0<<-'}:
            body = token.heredoc
            dynamic_body = token.dynamic
            descriptor_input = False
        elif operator in {'<', '0<'}:
            body = None
            descriptor_input = False
        elif operator in {'<&', '0<&'}:
            body = None
            descriptor_input = True
    if stdin and descriptor_input:
        raise ShellSyntaxError('interpreter descriptor input is not determined')
    if body is not None and stdin:
        if dynamic_body:
            raise ShellSyntaxError('dynamic interpreter heredoc')
        return ("python" if python else "shell" if shell else "opaque"), body
    return None


@dataclass(frozen=True)
class _NodeInvocation:
    files: tuple[ShellToken, ...]
    stdin: bool
    source: str | None


def _node_invocation(words: tuple[ShellToken, ...]) -> _NodeInvocation:
    """统一解释 Node 选项的值、代码与文件角色，避免三个入口各自猜测。"""
    flags = {"--test", "-c", "--check", "--test-only", "--watch", "--watch-preserve-output",
             "--enable-source-maps", "--no-warnings", "--trace-warnings", "--experimental-test-coverage",
             "--experimental-vm-modules", "--version", "-v", "--help", "-h"}
    data_options = {"--test-name-pattern", "--test-skip-pattern", "--test-concurrency", "--test-timeout",
                    "--conditions", "-C", "--input-type", "--unhandled-rejections", "--inspect-port", "--title"}
    file_options = {"--require", "-r", "--import", "--loader", "--experimental-loader"}
    source_options = {"-e", "--eval", "-p", "--print", "-pe"}
    args = words[1:]
    files = []
    source = None
    stdin = True
    i = 0
    while i < len(args):
        token = args[i]
        value = token.value
        if value == '--' or value == '-' or not value.startswith('-'):
            if value == '--':
                i += 1
            elif value == '-':
                i += 1
                stdin = True
            if i < len(args):
                if value != '-':
                    stdin = False
                files.extend(arg for arg in args[i:] if '://' not in arg.value
                             and not re.match(r'^[A-Za-z_]\w*=', arg.value))
            break
        option, separator, attached = value.partition('=')
        if not separator and value not in source_options and len(value) > 2 and value[:2] in {'-r', '-e', '-p', '-C'}:
            option, separator, attached = value[:2], '=', value[2:]
        if option in flags:
            if separator:
                raise ShellSyntaxError('unexpected Node flag value')
            if option in {'--test', '--version', '-v', '--help', '-h'}:
                stdin = False
        elif option in data_options | file_options | source_options | {'--test-reporter', '--test-reporter-destination'}:
            if not separator:
                i += 1
                if i >= len(args):
                    raise ShellSyntaxError('missing Node option value')
                token, attached = args[i], args[i].value
            if token.dynamic:
                raise ShellSyntaxError('dynamic Node option value')
            if option in source_options:
                source, stdin = attached, False
            elif (option in file_options
                  or option == '--test-reporter' and attached not in {'spec', 'tap', 'dot', 'junit', 'lcov'}
                  or option == '--test-reporter-destination' and attached not in {'stdout', 'stderr'}):
                parsed = urlsplit(attached)
                if parsed.scheme:
                    if parsed.scheme != 'file' or parsed.netloc not in {'', 'localhost'}:
                        raise ShellSyntaxError('unsupported Node code file scheme')
                    attached = unquote(parsed.path)
                files.append(replace(token, value=attached))
        else:
            raise ShellSyntaxError('unsupported Node interpreter option')
        i += 1
    return _NodeInvocation(tuple(files), stdin, source)


def _stdin_source(command: ShellCommand, language: str) -> tuple[str, str] | None:
    body = None
    dynamic_body = False
    descriptor_input = False
    for operator, token in command.redirects:
        if operator in {'<<', '<<-', '0<<', '0<<-'}:
            body, descriptor_input = token.heredoc, False
            dynamic_body = token.dynamic
        elif operator in {'<', '0<'}:
            body, descriptor_input = None, False
        elif operator in {'<&', '0<&'}:
            body, descriptor_input = None, True
    if descriptor_input:
        raise ShellSyntaxError('interpreter descriptor input is not determined')
    if body is not None and dynamic_body:
        raise ShellSyntaxError('dynamic interpreter heredoc')
    return (language, body) if body is not None else None


@dataclass(frozen=True)
class PythonEffects:
    files: tuple[str, ...]
    commands: tuple[str, ...]
    uncertain_cwd: bool = False
    incomplete_reason: str = ""


@dataclass(frozen=True)
class ShellOptions:
    files: tuple[ShellToken, ...] = ()
    commands: tuple[str, ...] = ()
    writes: bool = False
    incomplete_reason: str = ""
    uncertain_command_cwd_indices: tuple[int, ...] = ()


def _find_options(args: tuple[ShellToken, ...]) -> ShellOptions:
    """只提取显式起点、文件和回调；谓词不证明遍历结果安全。"""
    data_options = {
        '-name', '-iname', '-path', '-ipath', '-wholename', '-iwholename', '-regex', '-iregex',
        '-type', '-xtype', '-maxdepth', '-mindepth', '-regextype', '-printf',
        '-size', '-perm', '-user', '-group', '-uid', '-gid', '-links', '-inum',
        '-mtime', '-mmin', '-atime', '-amin', '-ctime', '-cmin', '-used',
    }
    file_options = {'-newer', '-anewer', '-cnewer', '-samefile', '-files0-from',
                    '-fprint', '-fprint0', '-fprintf', '-fls'}
    flags = {'-a', '-and', '-o', '-or', '-not', '!', '(', ')', ',', '-prune', '-print', '-print0',
             '-true', '-false', '-empty', '-readable', '-writable', '-executable', '-nouser', '-nogroup',
             '-depth', '-xdev', '-mount', '-daystart', '-noleaf', '-ignore_readdir_race',
             '-noignore_readdir_race', '-quit', '-ls'}
    files, children = [], []
    expression = False
    writes = False
    incomplete = ''
    uncertain_cwds = []
    i = 0
    while i < len(args):
        token = args[i]
        value = token.value
        if not expression and value in {'-H', '-L', '-P', '--'}:
            i += 1
            continue
        if not expression and not value.startswith('-') and value not in {'!', '('}:
            files.append(token)
            i += 1
            continue
        expression = True
        if value in data_options | file_options:
            i += 1
            if i >= len(args):
                incomplete = 'missing find predicate argument'
                break
            if value in file_options:
                files.append(args[i])
                writes |= value in {'-fprint', '-fprint0', '-fprintf', '-fls'}
            if value == '-files0-from':
                incomplete = 'find starting points loaded from a file are not analyzed'
            if value == '-fprintf':
                i += 1
                if i >= len(args):
                    incomplete = 'missing find output format'
        elif value in {'-exec', '-execdir', '-ok', '-okdir'}:
            uncertain_cwd = value in {'-execdir', '-okdir'}
            start = i + 1
            i = start
            while i < len(args) and args[i].value not in {';', '+'}:
                i += 1
            # 回调边界确定时保留可见执行入口；目录/占位符效果仍未完整分析。
            if i < len(args) and i > start:
                if uncertain_cwd:
                    uncertain_cwds.append(len(children))
                children.append(shlex.join(word.value for word in args[start:i]))
            incomplete = 'find callback execution is not fully analyzed'
        elif value == '-delete':
            writes = True
            incomplete = 'find deletion targets are not analyzed'
        elif value not in flags:
            incomplete = 'unsupported find expression'
        i += 1
    return ShellOptions(tuple(files), tuple(children), writes, incomplete, tuple(uncertain_cwds))


def _query_options(name: str, args: tuple[ShellToken, ...]) -> ShellOptions:
    """按选项的值角色消费参数；未知选项之后不再猜位置参数的用途。"""
    listing = name == 'ls'
    roles = ({'-I': 'data', '--ignore': 'data', '--hide': 'data', '--sort': 'data',
              '--format': 'data', '--time': 'data', '--time-style': 'data', '--quoting-style': 'data',
              '--block-size': 'data', '-w': 'data', '--width': 'data', '-T': 'data', '--tabsize': 'data'}
             if listing else {
                 '-e': 'pattern', '--regexp': 'pattern', '-f': 'file', '--file': 'file',
                 '-A': 'data', '--after-context': 'data', '-B': 'data', '--before-context': 'data',
                 '-C': 'data', '--context': 'data', '-m': 'data', '--max-count': 'data',
                 '--include': 'data', '--exclude': 'data', '--exclude-dir': 'data',
                 '--exclude-from': 'file', '--binary-files': 'data', '--label': 'data',
             })
    if name == 'rg':
        roles.update({'-g': 'data', '--glob': 'data', '--iglob': 'data', '-t': 'data', '--type': 'data',
                      '-T': 'data', '--type-not': 'data', '--type-add': 'data', '--type-clear': 'data',
                      '--max-depth': 'data', '--max-filesize': 'data', '--encoding': 'data',
                      '--replace': 'data', '-r': 'data', '--sort': 'data', '--sortr': 'data',
                      '--threads': 'data', '-j': 'data', '--pre': 'callback', '--hostname-bin': 'callback',
                      '--ignore-file': 'file'})
    short_flags = (set('aAbBcCdDfFgGhHiklLmMnNopqQrRsStuUvVXxZ1') if listing
                   else set('EFGHPVabchilnoqrsuvwxzIRZ'))
    if name == 'rg':
        short_flags |= set('SUL0N')
    long_flags = ({'--all', '--almost-all', '--author', '--classify', '--color', '--dereference',
                   '--directory', '--file-type', '--full-time', '--group-directories-first', '--human-readable',
                   '--inode', '--literal', '--numeric-uid-gid', '--quote-name', '--recursive', '--reverse',
                   '--size', '--version', '--help'} if listing else {
                       '--count', '--extended-regexp', '--fixed-strings', '--perl-regexp', '--ignore-case',
                       '--invert-match', '--line-number', '--no-filename', '--with-filename', '--only-matching',
                       '--quiet', '--silent', '--recursive', '--dereference-recursive', '--word-regexp',
                       '--line-regexp', '--files-with-matches', '--files-without-match', '--text', '--null',
                       '--null-data', '--color', '--colour', '--version', '--help',
                   })
    if name == 'rg':
        long_flags |= {'--files', '--hidden', '--no-ignore', '--no-ignore-vcs', '--no-ignore-global',
                       '--no-ignore-parent', '--no-ignore-dot', '--follow', '--multiline', '--pcre2',
                       '--smart-case', '--json', '--stats', '--no-heading', '--heading', '--no-messages',
                       '--no-line-number', '--no-config', '--crlf'}
    files, children = [], []
    pattern_pending = not listing
    positions_known = True
    options = True
    incomplete = ''
    i = 0
    while i < len(args):
        token = args[i]
        value = token.value
        if options and value == '--':
            options = False
            # 明确终止符恢复后续位置参数的边界，但不抹掉此前未知效果。
            positions_known = True
            i += 1
            continue
        selected = []
        if options and value.startswith('--'):
            option, separator, attached = value.partition('=')
            if option in roles:
                selected.append((roles[option], replace(token, value=attached) if separator else None))
            elif option in long_flags:
                if option == '--files':
                    pattern_pending = False
            else:
                positions_known = False
                incomplete = 'unsupported command option'
        elif options and value.startswith('-') and value != '-':
            if not listing and value[1:].isdigit():
                i += 1
                continue
            for offset, flag in enumerate(value[1:], 1):
                option = '-' + flag
                if option in roles:
                    attached = value[offset + 1:]
                    selected.append((roles[option], replace(token, value=attached) if attached else None))
                    break
                if flag not in short_flags:
                    positions_known = False
                    incomplete = 'unsupported command option'
                    break
        elif positions_known:
            if pattern_pending:
                pattern_pending = False
            else:
                files.append(token)
        for role, operand in selected:
            if operand is None:
                i += 1
                if i >= len(args):
                    incomplete = 'missing command option argument'
                    break
                operand = args[i]
            if role == 'file':
                files.append(operand)
            elif role == 'callback':
                children.append(shlex.quote(operand.value))
            if role in {'pattern', 'file'} and not listing:
                # 仅 -f/--file 定义匹配模式；ignore-file 等不消费位置模式。
                if role == 'pattern' or option in {'-f', '--file'}:
                    pattern_pending = False
        i += 1
    return ShellOptions(tuple(files), tuple(children), incomplete_reason=incomplete)


def command_options(command: ShellCommand) -> ShellOptions:
    """明确选项的文件/回调用途由各消费者共享，不以选项文本授予只读权限。"""
    words = executable_words(command)
    if not words:
        return ShellOptions()
    name = PurePosixPath(words[0].value).name
    if name in {"node", "nodejs"}:
        return ShellOptions(_node_invocation(words).files)
    if name == 'find':
        return _find_options(words[1:])
    if name in {'grep', 'egrep', 'fgrep', 'rg', 'ls'}:
        return _query_options(name, words[1:])
    files, children = [], []
    args = words[1:]
    i = 0
    while i < len(args):
        token = args[i]
        value = token.value
        option, _, attached = value.partition('=')
        file_option = name == 'git' and option == '--output'
        callback = name == 'git' and any(arg.value == 'grep' for arg in args[:1]) and option == '--open-files-in-pager'
        short_pager = name == 'git' and args and args[0].value == 'grep' and value.startswith('-O')
        if short_pager:
            if value != '-O':
                children.append(value[2:])
            else:
                # 无显式pager时由环境选择，不能取得自动只读身份。
                children.append('')
        elif file_option or callback:
            if '=' not in value:
                i += 1
                if i >= len(args):
                    raise ShellSyntaxError('missing command option argument')
                token = args[i]
                attached = token.value
            if file_option:
                files.append(replace(token, value=attached))
            else:
                children.append(attached)
        i += 1
    return ShellOptions(tuple(files), tuple(children), bool(files))


def python_effects(source: str, *, depth: int = 0) -> PythonEffects:
    """仅解释标准库明确的常量位置，不把程序文本当路径或执行代码。"""
    if depth > 8 or len(source) > 2 * 1024 * 1024:
        raise ShellSyntaxError("inline program analysis limit")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ShellSyntaxError("invalid inline Python syntax") from exc
    pending = [(tree, 0)]
    visited = 0
    while pending:
        node, level = pending.pop()
        visited += 1
        if level > 64 or visited > 50000:
            raise ShellSyntaxError("inline Python structure analysis limit")
        pending.extend((child, level + 1) for child in ast.iter_child_nodes(node))
    aliases = {}
    def bind_alias(local, target):
        if local in aliases and aliases[local] != target:
            raise ShellSyntaxError('conflicting inline import aliases')
        aliases[local] = target

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bind_alias(alias.asname or alias.name, alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bind_alias(alias.asname or alias.name, node.module + "." + alias.name)

    def qualified(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return qualified(node.value) + "." + node.attr
        return ""

    io_constructors = {"Path", "PosixPath", "WindowsPath", "pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}
    constructors = io_constructors | {"PurePath", "PurePosixPath", "PureWindowsPath", "pathlib.PurePath",
                                      "pathlib.PurePosixPath", "pathlib.PureWindowsPath"}
    bindings: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name) and node.value:
            bindings.setdefault(node.target.id, []).append(node.value)

    remaining = 10000
    unresolved_literals = 0

    def conditional_values(node):
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            return (node.body if node.test.value else node.orelse,)
        return node.body, node.orelse

    def literals(node, seen=frozenset()):
        nonlocal remaining, unresolved_literals
        remaining -= 1
        if remaining < 0 or len(seen) > 64:
            raise ShellSyntaxError("inline constant analysis limit")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (node.value,)
        if isinstance(node, ast.IfExp):
            values = {value for branch in conditional_values(node) for value in literals(branch, seen)}
            if len(values) > 64:
                raise ShellSyntaxError("inline constant analysis limit")
            return tuple(values)
        if isinstance(node, ast.Name) and node.id not in seen:
            # 保留所有静态赋值候选，后来的重赋值不能抹掉此前的真实访问。
            values = {value for expr in bindings.get(node.id, ())
                      for value in literals(expr, seen | {node.id})}
            if len(values) > 64:
                raise ShellSyntaxError("inline constant analysis limit")
            if not bindings.get(node.id):
                unresolved_literals += 1
            return tuple(values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            left_values, right_values = literals(node.left, seen), literals(node.right, seen)
            if len(left_values) * len(right_values) > 64:
                raise ShellSyntaxError("inline constant analysis limit")
            pairs = product(left_values, right_values)
            values = tuple(left + right if isinstance(node.op, ast.Add) else str(PurePosixPath(left) / right)
                           for left, right in pairs)
            return values
        if isinstance(node, ast.Call) and qualified(node.func) in constructors:
            parts = [literals(arg, seen) for arg in node.args]
            combinations = 1
            for part in parts:
                combinations *= len(part)
            if combinations > 64:
                raise ShellSyntaxError("inline constant analysis limit")
            return tuple(str(PurePosixPath(*path)) for path in product(*parts)) if parts else ('.',)
        if isinstance(node, ast.Call) and qualified(node.func) in {'str', 'os.fspath'} and len(node.args) == 1:
            return literals(node.args[0], seen)
        if isinstance(node, ast.Attribute) and node.attr in {'parent', 'name', 'stem', 'suffix'}:
            return tuple(str(getattr(PurePosixPath(value), node.attr)) for value in literals(node.value, seen))
        unresolved_literals += 1
        return ()

    files, commands = [], []
    uncertain_cwd = False
    incomplete = ''

    def file_argument(node, *, default=None):
        nonlocal incomplete
        unresolved_before = unresolved_literals
        values = (default,) if node is None and default is not None else literals(node)
        if not values or unresolved_literals != unresolved_before:
            incomplete = 'inline Python file operand is not determined'
        files.extend(values)

    path_data_methods = {'as_posix', 'as_uri', 'is_absolute', 'is_relative_to', 'match', 'full_match',
                         'relative_to', 'joinpath', 'with_name', 'with_stem', 'with_suffix', 'with_segments'}
    path_io_methods = {'open', 'read_text', 'read_bytes', 'write_text', 'write_bytes', 'touch', 'mkdir',
                       'unlink', 'rmdir', 'rename', 'replace', 'chmod', 'lchmod', 'stat', 'lstat', 'exists',
                       'is_file', 'is_dir', 'is_symlink', 'is_mount', 'is_socket', 'is_fifo', 'is_block_device',
                       'is_char_device', 'is_junction', 'owner', 'group', 'readlink', 'resolve', 'samefile',
                       'iterdir', 'glob', 'rglob', 'walk', 'symlink_to', 'hardlink_to', 'link_to'}

    def path_receiver(node, seen=frozenset()):
        if len(seen) > 64:
            return False
        if isinstance(node, ast.Name) and node.id not in seen:
            return any(path_receiver(value, seen | {node.id}) for value in bindings.get(node.id, ()))
        if isinstance(node, ast.IfExp):
            return any(path_receiver(value, seen) for value in conditional_values(node))
        if isinstance(node, ast.Call):
            if qualified(node.func) in io_constructors:
                return True
            return (isinstance(node.func, ast.Attribute)
                    and node.func.attr in {'joinpath', 'with_name', 'with_stem', 'with_suffix',
                                           'relative_to', 'absolute', 'resolve', 'expanduser'}
                    and path_receiver(node.func.value, seen))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return path_receiver(node.left, seen) or path_receiver(node.right, seen)
        return isinstance(node, ast.Attribute) and node.attr == 'parent' and path_receiver(node.value, seen)

    def path_methods(function, seen=frozenset()):
        # 仅跟随可见的方法绑定和条件候选；取出方法本身不代表执行文件操作。
        if len(seen) > 64:
            return [], True
        if isinstance(function, ast.Name) and function.id not in seen:
            candidates = bindings.get(function.id, ())
            next_seen = seen | {function.id}
        elif isinstance(function, ast.IfExp):
            candidates = conditional_values(function)
            next_seen = seen
        elif isinstance(function, ast.Attribute):
            receiver = function.value
            unbound = qualified(receiver) in io_constructors
            if unbound or path_receiver(receiver):
                return [(function.attr, receiver, unbound)], False
            return [], True
        else:
            return [], True
        targets, uncertain = [], not candidates
        for candidate in candidates:
            methods, unknown = path_methods(candidate, next_seen)
            targets.extend(methods)
            uncertain |= unknown
        return targets, uncertain

    file_calls = {"open": ("file",), "builtins.open": ("file",), "io.open": ("file",), "os.open": ("path",), "os.chdir": ("path",),
        "os.listdir": ("path",), "os.scandir": ("path",), "os.stat": ("path",), "os.lstat": ("path",),
        "os.remove": ("path",), "os.unlink": ("path",), "os.mkdir": ("path",), "os.makedirs": ("name",),
        "os.rmdir": ("path",), "os.rename": ("src", "dst"), "os.replace": ("src", "dst"),
        "shutil.copy": ("src", "dst"), "shutil.copy2": ("src", "dst"), "shutil.copyfile": ("src", "dst"),
        "shutil.move": ("src", "dst"), "shutil.rmtree": ("path",)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = qualified(node.func)
        kwargs = {item.arg: item.value for item in node.keywords if item.arg}
        if name == 'os.chdir' or name.startswith('subprocess.') and 'cwd' in kwargs:
            uncertain_cwd = True
            files.extend(literals(kwargs.get('cwd')))
        if name in file_calls and any(key in kwargs for key in {'dir_fd', 'src_dir_fd', 'dst_dir_fd'}):
            uncertain_cwd = True
        for index, keyword in enumerate(file_calls.get(name, ())):
            arg = node.args[index] if index < len(node.args) else kwargs.get(keyword)
            file_argument(arg, default='.' if name in {'os.listdir', 'os.scandir'} else None)
        methods, uncertain_methods = path_methods(node.func)
        if methods and uncertain_methods:
            incomplete = 'inline Python path method is not determined'
        for method, receiver, unbound in methods:
            offset = 0
            if unbound:
                receiver = node.args[0] if node.args else kwargs.get('self')
                offset = 1
            if method in path_io_methods:
                # 路径对象仅在实际文件操作处成为访问事实；构造和打印保持数据。
                file_argument(receiver)
                if method in {'rename', 'replace', 'samefile', 'symlink_to', 'hardlink_to', 'link_to'}:
                    file_argument(node.args[offset] if len(node.args) > offset else kwargs.get('target', kwargs.get('other_path')))
            elif method not in path_data_methods | {'absolute', 'expanduser'}:
                incomplete = 'unsupported inline Python path operation'
        if name in {"exec", "eval"} and node.args:
            unresolved_before = unresolved_literals
            sources = literals(node.args[0])
            if not sources or unresolved_literals != unresolved_before:
                incomplete = 'inline Python source is not determined'
            for value in sources:
                effects = python_effects(value, depth=depth + 1)
                files.extend(effects.files)
                commands.extend(effects.commands)
                uncertain_cwd |= effects.uncertain_cwd
                incomplete = incomplete or effects.incomplete_reason
        if name in {"os.system", "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
            unresolved_before = unresolved_literals
            arg = node.args[0] if node.args else kwargs.get("args", kwargs.get("command"))
            if isinstance(arg, (ast.List, ast.Tuple)):
                parts = [literals(part) for part in arg.elts]
                combinations = 1
                for part in parts:
                    combinations *= len(part)
                if combinations > 64:
                    raise ShellSyntaxError("inline command analysis limit")
                commands.extend(shlex.join(values) for values in product(*parts))
            else:
                commands.extend(literals(arg))
            if 'executable' in kwargs:
                commands.extend(shlex.quote(value) for value in literals(kwargs['executable']))
            if unresolved_literals != unresolved_before:
                incomplete = 'inline Python command is not determined'
    return PythonEffects(tuple(dict.fromkeys(files)), tuple(dict.fromkeys(commands)), uncertain_cwd, incomplete)


def file_operands(command: ShellCommand) -> tuple[ShellToken, ...]:
    words = executable_words(command)
    if not words:
        return ()
    name = PurePosixPath(words[0].value).name
    if name in {"node", "nodejs", 'find', 'grep', 'egrep', 'fgrep', 'rg', 'ls'}:
        return command_options(command).files
    if inline_source(command) is not None or name in {"echo", "printf", "true", "false", "exit", "set"}:
        return ()
    result = list(command_options(command).files)
    args = list(words[1:])
    pattern_pending = name in {"sed", "awk"}
    i = 0
    while i < len(args):
        token = args[i]
        value = token.value
        if name in {"sed", "awk"}:
            if value in {"-e", "--regexp", "--expression", "-f", "--file"}:
                i += 1
                if i < len(args) and value in {"-f", "--file"}:
                    result.append(args[i])
                pattern_pending = False
                i += 1
                continue
            if value.startswith(("--file=", "-f")):
                result.append(replace(token, value=value.split("=", 1)[1] if "=" in value else value[2:]))
                pattern_pending = False
                i += 1
                continue
            if value.startswith(("--regexp=", "-e")):
                pattern_pending = False
                i += 1
                continue
        if value.startswith("-"):
            if value in {"--max-count", "--max-depth", "--glob", "-g", "--include", "--exclude"}:
                i += 1
            i += 1
            continue
        if pattern_pending:
            pattern_pending = False
        elif "://" not in value and not re.match(r"^[A-Za-z_]\w*=", value):
            result.append(token)
        i += 1
    return tuple(result)
