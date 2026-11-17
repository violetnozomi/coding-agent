"""有限 shell 参数用途分析；这是准入事实，不是操作系统沙箱。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from nz_coder.foundation.shell_syntax import (
    ShellSyntaxError, ShellToken, command_options, executable_words, file_operands,
    inline_source, python_effects, shell_commands, substitution_commands,
)
from nz_coder.foundation.workspace_paths import (
    WorkspacePathPolicy, WorkspacePathError, _private_path_reason,
)


@dataclass(frozen=True)
class ShellPathAnalysis:
    kind: str
    escaped_path: str | None = None
    reason: str = ""

    @property
    def rejection(self) -> str:
        if self.kind == "outside":
            return f"path outside workspace ({self.escaped_path})"
        if self.kind in {"forbidden", "invalid"}:
            return self.reason
        return ""


def _merge(*outcomes: ShellPathAnalysis | None) -> ShellPathAnalysis:
    priority = {"supported": 0, "legacy": 0, "unsupported": 1, "invalid": 2, "outside": 3, "forbidden": 3}
    return max((item for item in outcomes if item is not None),
               key=lambda item: priority[item.kind], default=ShellPathAnalysis("legacy"))


def _unknown(reason: str) -> ShellPathAnalysis:
    return ShellPathAnalysis("unsupported", reason=reason)


def _file(value: str, root: Path, cwd: Path) -> ShellPathAnalysis | None:
    if value in {"-", "/dev/null", "NUL", "nul"}:
        return None
    if re.match(r'^[A-Za-z]:[\\/]|^\\\\|^%\w+%|^\$env:', value) and os.name != "nt":
        return ShellPathAnalysis("outside", value)
    try:
        WorkspacePathPolicy(root).validate_model_read(cwd / value)
    except WorkspacePathError as exc:
        if "escapes workspace" in str(exc):
            return ShellPathAnalysis("outside", value)
        kind = 'forbidden' if 'Model access blocked' in str(exc) else 'invalid'
        return ShellPathAnalysis(kind, reason=str(exc))
    return None


def _glob_files(value: str, root: Path, cwd: Path) -> ShellPathAnalysis | None:
    if '**' in value:
        return _unknown("recursive glob shell options are not determined")
    pattern = Path(value)
    parts = pattern.parts
    first = next(index for index, part in enumerate(parts) if re.search(r'[*?\[]', part))
    bases = [cwd / Path(*parts[:first])]
    checked = 0
    outcome = None
    for part in parts[first:]:
        following = []
        for base in bases:
            violation = _file(str(base), root, cwd)
            if violation:
                outcome = _merge(outcome, violation)
                continue
            try:
                if re.search(r'[*?\[]', part):
                    if not base.is_dir():
                        continue
                    candidates = base.iterdir()
                else:
                    candidates = iter((base / part,))
                for path in candidates:
                    checked += 1
                    if checked > 4096:
                        return _merge(outcome, _unknown("file glob analysis limit"))
                    if re.search(r'[*?\[]', part) and (
                        path.name.startswith('.') and not part.startswith('.')
                        or not fnmatchcase(path.name, part)
                    ):
                        continue
                    violation = _file(str(path), root, cwd)
                    if violation:
                        outcome = _merge(outcome, violation)
                    elif os.path.lexists(path):
                        following.append(path)
            except (OSError, RuntimeError, ValueError):
                outcome = _merge(outcome, ShellPathAnalysis("invalid", reason="Cannot resolve glob file paths."))
        bases = following
    if not bases:
        return _merge(outcome, _unknown("file glob has no verified matches"))
    return outcome


def _operand(token: ShellToken, root: Path, cwd: Path, *, cwd_known: bool = True) -> ShellPathAnalysis | None:
    if not cwd_known and not Path(token.value).is_absolute():
        private_reason = _private_path_reason(token.value)
        if private_reason:
            return ShellPathAnalysis("forbidden", reason=f"Model access blocked: {private_reason}")
        return _unknown("relative file operand has an undetermined working directory")
    if not token.dynamic:
        return _file(token.value, root, cwd)
    # 未展开的普通文本仍能显式命名 .env 等禁区；不能先 unknown 而丢掉它。
    literal_check = _file(token.value, root, cwd)
    if literal_check and literal_check.kind == "forbidden":
        return literal_check
    if token.expansions == {"glob"}:
        return literal_check or _glob_files(token.value, root, cwd)
    return _unknown("dynamic file operand")


def _url(token: ShellToken, binding: tuple[str, str] | None, root: Path, cwd: Path, *, cwd_known: bool):
    value = token.value
    if token.dynamic:
        if binding is None:
            return _unknown("unbound URL variable")
        name, base = binding
        match = re.fullmatch(r'\$(?:' + name + r'|\{' + name + r'\})(/[^$`\s]*)', value)
        if match is None:
            return _unknown("unsupported URL expansion")
        value = base + match[1]
    if any(ch in value for ch in '{}[]'):
        return _unknown('curl URL glob syntax')
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _unknown("invalid URL")
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return None
    if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
        return _operand(ShellToken(unquote(parsed.path)), root, cwd, cwd_known=cwd_known)
    return _unknown("unsupported URL scheme or operand")


def _curl(tokens: tuple[ShellToken, ...], binding, root: Path, cwd: Path, *, cwd_known: bool):
    options = {
        '-X': 'method', '--request': 'method', '-H': 'header', '--header': 'header',
        '-d': 'data', '--data': 'data', '--data-binary': 'data', '--data-raw': 'literal',
        '-w': 'writeout', '--write-out': 'writeout', '-o': 'output', '--output': 'output',
        '-F': 'form', '--form': 'form', '--form-string': 'literal',
        '-K': 'config', '--config': 'config',
    }
    flags = {'-s', '--silent', '-S', '--show-error', '-sS'}
    urls, i = 0, 0
    outcome = None

    def file_operand(value):
        return _operand(ShellToken(value), root, cwd, cwd_known=cwd_known)

    while i < len(tokens):
        token = tokens[i]
        value = token.value
        if not token.dynamic and value in flags:
            i += 1
            continue
        role = options.get(value)
        operand = None
        if role:
            i += 1
            if i >= len(tokens):
                return _merge(outcome, _unknown('missing curl option argument'))
            operand = tokens[i]
        elif value.startswith('--') and '=' in value:
            option, attached = value.split('=', 1)
            role = options.get(option)
            operand = replace(token, value=attached)
        elif len(value) > 2 and value[:2] in {'-X', '-H', '-d', '-w', '-o', '-F', '-K'}:
            role, operand = options[value[:2]], replace(token, value=value[2:])
        error = None
        if role:
            assert operand is not None
            data = operand.value
            if operand.dynamic:
                error = _unknown('dynamic curl option argument')
                if role in {'output', 'config'}:
                    error = _merge(error, _operand(operand, root, cwd, cwd_known=cwd_known))
                elif role != 'literal' and data.startswith('@'):
                    error = _merge(error, _operand(replace(operand, value=data[1:]), root, cwd, cwd_known=cwd_known))
            elif role == 'method':
                if data not in {'GET', 'POST', 'PATCH', 'DELETE', 'PUT', 'HEAD', 'OPTIONS'}:
                    error = _unknown('unsupported HTTP method')
            elif role == 'config':
                error = _merge(file_operand(data), _unknown('curl config file contents are not analyzed'))
            elif role == 'form':
                if '=' not in data:
                    error = _unknown('unsupported curl form operand')
                else:
                    form_value = data.split('=', 1)[1]
                    if form_value.startswith(('@', '<')):
                        for path in form_value[1:].split(';', 1)[0].split(','):
                            error = _merge(error, file_operand(path))
            elif role == 'output' or role != 'literal' and data.startswith('@'):
                error = file_operand(data if role == 'output' else data[1:])
                if role == 'output' and re.search(r'#\d+', data):
                    error = _merge(error, _unknown('curl output capture template'))
                if role == 'writeout':
                    error = _merge(error, _unknown('file-loaded curl write-out format is not a checked literal'))
            elif role == 'writeout':
                for target in re.findall(r'%output\{([^}]+)\}', data):
                    error = _merge(error, file_operand(target.removeprefix('>>')))
                remaining = re.sub(r'%%|%\{(?:http_code|content_type|size_download)\}', '', data)
                if '%' in remaining:
                    error = _merge(error, _unknown('unsupported curl write-out format'))
        elif value.startswith('-'):
            error = _unknown('unsupported curl option')
        else:
            error = _url(token, binding, root, cwd, cwd_known=cwd_known)
            urls += 1
        outcome = _merge(outcome, error)
        i += 1
    return _merge(outcome, _unknown('missing curl URL') if not urls else None)


def analyze_shell_paths(command: str, workspace_root: Path, *, command_cwd: Path) -> ShellPathAnalysis:
    """按命令段累计可确认事实，分析未知不能抹掉另一段的明确禁止。"""
    try:
        policy = WorkspacePathPolicy(workspace_root)
        cwd = policy.validate_model_execute(command_cwd)
    except WorkspacePathError as exc:
        kind = ('outside' if 'escapes workspace' in str(exc) else
                'forbidden' if 'Model access blocked' in str(exc) else 'invalid')
        return ShellPathAnalysis(kind, str(command_cwd) if kind == 'outside' else None, str(exc))
    try:
        return _ordinary_paths(shell_commands(command, partial=True), policy.root, cwd)
    except ShellSyntaxError as exc:
        return ShellPathAnalysis(exc.kind, reason=str(exc))


def _ordinary_paths(commands, root: Path, cwd: Path, *, depth: int = 0, cwd_known: bool = True):
    if depth > 8:
        return _unknown('shell analysis nesting limit')
    outcome = ShellPathAnalysis('legacy')
    binding = None
    group_contexts = []
    for index, segment in enumerate(commands):
        if segment.opaque and len(segment.words) == 1 and segment.words[0].operator:
            # 子 shell 中 cd 和变量赋值不能改变外层的路径事实。
            if segment.words[0].value == '(':
                group_contexts.append((cwd, cwd_known, binding))
            elif segment.words[0].value == ')' and group_contexts:
                cwd, cwd_known, binding = group_contexts.pop()
            outcome = _merge(outcome, _unknown('subshell structure is not fully analyzed'))
            continue
        words = executable_words(segment)
        name = Path(words[0].value).name if words else ''
        if segment.connector not in {';', '\n', '&&'}:
            binding = None
        for operator, token in segment.redirects:
            if token.heredoc is not None:
                if token.dynamic:
                    outcome = _merge(outcome, _unknown('dynamic heredoc expansion'))
                continue
            if operator.endswith('&') and token.value in {'0', '1', '2', '-'}:
                continue
            outcome = _merge(outcome, _operand(token, root, cwd, cwd_known=cwd_known))
        # 替换先于 curl/赋值等专用参数用途发生，不能被这些分支的 continue 跳过。
        for child in substitution_commands(segment):
            try:
                nested = _ordinary_paths(shell_commands(child, partial=True), root, cwd, depth=depth + 1, cwd_known=cwd_known)
            except ShellSyntaxError as exc:
                nested = ShellPathAnalysis(exc.kind, reason=str(exc))
            outcome = _merge(outcome, nested)
        if segment.opaque:
            outcome = _merge(outcome, _unknown('compound shell structure is not fully analyzed'))
            binding = None
            continue
        if len(segment.words) == 1 and '=' in segment.words[0].value and not words:
            assignment = segment.words[0]
            key, value = assignment.value.split('=', 1)
            if (binding is None and segment.connector in {';', '\n'}
                    and key in {'B', 'BASE_URL', 'API_URL'} and not assignment.dynamic
                    and re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', value)
                    and 1 <= int(value.rsplit(':', 1)[1]) <= 65535):
                binding = key, value
            else:
                binding = None
                outcome = _merge(outcome, _unknown('unsupported URL assignment'))
            continue
        if name == 'curl':
            outcome = _merge(ShellPathAnalysis('supported'), outcome,
                             _curl(words[1:], binding, root, cwd, cwd_known=cwd_known))
            continue
        candidates = []
        children = []
        program_cwd_known = cwd_known
        try:
            candidates.extend(file_operands(segment))
            options = command_options(segment)
            # 相同回调文本也可能来自不同执行目录，按调用位置保留目录事实。
            children.extend((child, index not in options.uncertain_command_cwd_indices)
                            for index, child in enumerate(options.commands))
            if options.incomplete_reason:
                outcome = _merge(outcome, _unknown(options.incomplete_reason))
            program = inline_source(segment)
            if program:
                if program[0] == 'python':
                    effects = python_effects(program[1])
                    candidates.extend(ShellToken(path) for path in effects.files)
                    children.extend((child, True) for child in effects.commands)
                    if effects.incomplete_reason:
                        outcome = _merge(outcome, _unknown(effects.incomplete_reason))
                    if effects.uncertain_cwd:
                        program_cwd_known = False
                        outcome = _merge(outcome, _unknown('inline program working directory is not determined'))
                elif program[0] == 'shell':
                    children.append((program[1], True))
                else:
                    outcome = _merge(outcome, _unknown('inline program effects are not analyzed'))
        except ShellSyntaxError as exc:
            outcome = _merge(outcome, ShellPathAnalysis(exc.kind, reason=str(exc)))
        for word in words:
            if word.dynamic and word not in candidates and not (
                name in {'echo', 'printf'} and word is not words[0] and word.parameter_only
            ):
                outcome = _merge(outcome, _unknown('dynamic shell argument'))
        for token in candidates:
            outcome = _merge(outcome, _operand(token, root, cwd, cwd_known=program_cwd_known))
        for child, child_cwd_known in children:
            try:
                nested = _ordinary_paths(shell_commands(child, partial=True), root, cwd, depth=depth + 1,
                                         cwd_known=program_cwd_known and child_cwd_known)
            except ShellSyntaxError as exc:
                nested = ShellPathAnalysis(exc.kind, reason=str(exc))
            outcome = _merge(outcome, nested)
        if name == 'cd':
            if len(words) != 2 or words[1].dynamic or words[1].value.startswith('-'):
                outcome = _merge(outcome, _unknown('working directory operand is not determined'))
                cwd_known = False
            elif any(following.connector != '&&' for following in commands[index + 1:]):
                outcome = _merge(outcome, _unknown('conditional working directory is not determined'))
                cwd_known = False
            elif cwd_known and not _file(words[1].value, root, cwd):
                cwd = (cwd / words[1].value).resolve()
        if name not in {'echo', 'printf', 'true', 'false', 'set'}:
            binding = None
    return outcome
